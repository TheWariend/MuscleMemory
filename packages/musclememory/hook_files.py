"""Bounded local file checks before hook delivery; contents never leave this module."""

import os
import re
import stat
from pathlib import Path

from . import workspace
from .artifacts import (
    PATH_KEYS,
    WORKSPACE_CHANGES,
    extract_experience,
    normalized_path,
    operation_categories,
)

MAX_FILES = 32
MAX_FILE_BYTES = 1024 * 1024


def read_file(root, path, *, contents):
    """Open beneath the root without following any symlink."""
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return "unsafe_file", None
            if info.st_size > MAX_FILE_BYTES:
                return "file_too_large", None
            if not contents:
                return None, None
            data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                return "file_too_large", None
            return None, data.decode("utf-8")
    finally:
        os.close(parent)


def check_files(definition, cwd):
    """Return a skip reason and mismatched files, or no reason when safe to deliver."""
    try:
        experience = extract_experience(definition["steps"])
        edits, writes = experience["edits"], experience["writes"]
        for step in definition["steps"]:
            arguments = step["arguments"]
            if step["effect"] == "read" and "modify" not in operation_categories(
                arguments, step["capability"]
            ):
                continue
            names = [value for key, value in arguments.items() if key in PATH_KEYS]
            names.extend(arguments.get(WORKSPACE_CHANGES, []))
            patch = arguments.get("patch", arguments.get("input", arguments.get("command", "")))
            if isinstance(patch, str):
                names.extend(re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch, re.M))
            for name in names:
                if not (path := normalized_path(name)):
                    return "unsafe_path", []
                writes.append(path)
        if not edits and not writes:
            return None, []
        root, _ = workspace.project_root(cwd)
        if not root:
            return "missing_project_root", []
        root = Path(root)
        paths = {}
        for name in dict.fromkeys([*writes, *(edit["file"] for edit in edits)]):
            normalized = normalized_path(name)
            if not normalized:
                return "unsafe_path", []
            path = Path(normalized)
            if path.is_absolute():
                try:
                    path = path.relative_to(root)
                except ValueError:
                    return "unsafe_path", []
            paths[name] = path
        if len(paths) > MAX_FILES:
            return "too_many_files", []
        if not edits:
            return "no_edit_evidence", []
        edited = {edit["file"] for edit in edits}
        files = {}
        for name, path in paths.items():
            try:
                reason, text = read_file(root, path, contents=name in edited)
            except FileNotFoundError:
                return "missing_changed_file", []
            except UnicodeError:
                return "unreadable_file", []
            except OSError:
                return "unsafe_or_unreadable_file", []
            if reason:
                return reason, []
            files[name] = text
        matched, mismatched = 0, set()
        for edit in edits:
            if any(
                isinstance(text := edit.get(key), str) and text and text in files[edit["file"]]
                for key in ("before", "after")
            ):
                matched += 1
            else:
                mismatched.add(edit["file"])
        if not matched:
            return "edited_code_missing", []
        return None, sorted(mismatched)
    except Exception:
        return "file_check_error", []
