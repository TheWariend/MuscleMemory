"""Value-free views of an agent's project, used by the capture hooks.

Agents often change code from the shell (scripts, heredocs, ``sed -i``) instead of an
edit tool. Comparing which files differ from version control before and after a shell
command shows whether it changed code and which files, without reading any contents.
Paths inside the project are made relative so a learned skill matches other checkouts.
"""

import os
import re
import subprocess

from .artifacts import test_run

IGNORED_DIRECTORIES = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nyc_output",
    "htmlcov",
    ".next",
}
IGNORED_NAMES = {".coverage", ".DS_Store"}
IGNORED_SUFFIXES = (".pyc", ".pyo", ".log")
WALK_LIMIT = 4000
GIT_ENVIRONMENT = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


def project_root(cwd):
    """The git work tree containing ``cwd``, else ``cwd``; ``(None, False)`` when unusable."""
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        return None, False
    root = current = os.path.realpath(cwd)
    while True:
        if os.path.isfile(os.path.join(current, ".git")) or os.path.isdir(
            os.path.join(current, ".git")
        ):
            return current, True
        parent = os.path.dirname(current)
        if parent == current:
            return root, False
        current = parent


def edit_target(path, cwd):
    """Canonical local target for comparing concurrent edits, or unknown."""
    if not isinstance(path, str) or not path:
        return None
    if not os.path.isabs(path):
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            return None
        path = os.path.join(cwd, path)
    try:
        return os.path.realpath(path)
    except (OSError, ValueError, RuntimeError):
        return None


def overlapping_paths(left, right):
    """Compare aliases conservatively, including claims from older hook versions."""
    if not all(isinstance(path, str) and os.path.isabs(path) for path in (left, right)):
        return True
    # Case folding can conservatively serialize distinct files on Linux, but
    # cannot miss aliases on a case-insensitive filesystem, including new files.
    if left.casefold() == right.casefold():
        return True
    try:
        return os.path.samefile(left, right)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True


def ignored(path):
    parts = path.split("/")
    return (
        any(part in IGNORED_DIRECTORIES for part in parts[:-1])
        or parts[-1] in IGNORED_NAMES
        or parts[-1].endswith(IGNORED_SUFFIXES)
    )


def _stat(root, path):
    try:
        status = os.stat(os.path.join(root, path))
        return [status.st_size, status.st_mtime_ns]
    except OSError:
        return "missing"


def snapshot(cwd):
    """``{"root", "files"}``: size and mtime of files that differ from git (or of every file)."""
    root, is_git = project_root(cwd)
    if not root:
        return None
    files = {}
    if is_git:
        try:
            listed = subprocess.run(
                ["git", "-C", root, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                capture_output=True,
                timeout=5,
                env=GIT_ENVIRONMENT,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if listed.returncode != 0:
            return None
        entries = listed.stdout.decode("utf-8", "surrogateescape").split("\0")
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            status, path = entry[:2], entry[3:]
            if status[0] in "RC":
                index += 1  # porcelain -z puts the original name next
            if not ignored(path):
                files[path] = _stat(root, path)
    else:
        for directory, names, filenames in os.walk(root):
            names[:] = [name for name in names if name not in IGNORED_DIRECTORIES]
            for name in filenames:
                path = os.path.relpath(os.path.join(directory, name), root).replace(os.sep, "/")
                if ignored(path):
                    continue
                files[path] = _stat(root, path)
                if len(files) > WALK_LIMIT:
                    return None
    return {"root": root, "files": files}


def changed_files(before, after):
    """Project-relative paths whose state differs between two snapshots, or None if unknown."""
    if not before or not after or before["root"] != after["root"]:
        return None
    old, new = before["files"], after["files"]
    return sorted(path for path in set(old) | set(new) if old.get(path) != new.get(path))


def roots(cwd, root):
    """Every spelling of the project root an agent may use (e.g. /tmp and /private/tmp)."""
    found = {value for value in (root, cwd) if isinstance(value, str) and value.startswith("/")}
    found |= {os.path.realpath(value) for value in list(found)}
    cleaned = {value.rstrip("/") for value in found if value.rstrip("/")}
    return sorted(cleaned, key=len, reverse=True)


def relative_path(value, spellings):
    if not isinstance(value, str) or not value.startswith("/"):
        return value
    for root in spellings:
        if value == root:
            return "."
        if value.startswith(root + "/"):
            return value[len(root) + 1 :]
    return value


def relative_command(command, spellings):
    """Drop a leading ``cd <project> &&`` and write project paths relative to it."""
    if not isinstance(command, str):
        return command
    for root in spellings:
        for quoted in (root, f'"{root}"', f"'{root}'"):
            for joiner in (" && ", "; "):
                prefix = f"cd {quoted}{joiner}"
                if command.startswith(prefix):
                    command = command[len(prefix) :]
        command = command.replace(root + "/", "")
    return command


PIPED = re.compile(r"(?<!\|)\|(?!\|)")
FAILED = re.compile(
    r"^FAILED \(|^FAIL[: \t]|\b\d+ failed\b|\b\d+ errors?\b"
    r"|test result: FAILED|^Tests:.*\bfailed\b",
    re.MULTILINE,
)
PASSED = re.compile(
    r"^OK(?: \(.*\))?\s*$|\b\d+ passed\b|test result: ok\b|^ok\s|^Tests:\s+\d+ passed",
    re.MULTILINE,
)


def piped_test(words, command):
    """A test runner whose output is piped on, so the exit status belongs to another program."""
    for index, word in enumerate(words):
        if test_run(words[index:]):
            return any(PIPED.search(later) for later in words[index + 1 :]) or bool(
                PIPED.search(command.split(word, 1)[-1])
            )
    return False


def output_text(response):
    """Read known terminal output fields without escaping their newlines as JSON."""
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return ""
    parts = [
        response[key]
        for key in ("stdout", "stderr", "output", "aggregated_output", "text")
        if isinstance(response.get(key), str)
    ]
    content = response.get("content")
    if isinstance(content, list):
        parts.extend(
            item["text"]
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        )
    return "\n".join(parts)


def test_output_verdict(text):
    """True/False from the runner's own summary line, None when it cannot be told."""
    if not isinstance(text, str) or not text:
        return None
    if FAILED.search(text):
        return False
    if PASSED.search(text):
        return True
    return None
