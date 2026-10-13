"""Value-free views of an agent's project, used by the capture hooks.

Agents often change code from the shell (scripts, heredocs, ``sed -i``) instead of an
edit tool. Comparing which files differ from version control before and after a shell
command shows whether it changed code and which files, without reading any contents.
Paths inside the project are made relative so a learned skill matches other checkouts.
"""

import os
import re
import subprocess

from .artifacts import TEST_WORDS, TESTERS

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
    try:
        found = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
            env=GIT_ENVIRONMENT,
        )
        if found.returncode == 0 and found.stdout.strip():
            return os.path.realpath(found.stdout.strip()), True
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.realpath(cwd), False


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
        name = os.path.basename(word)
        if name in TESTERS or word in TEST_WORDS:
            return any(PIPED.search(later) for later in words[index + 1 :]) or bool(
                PIPED.search(command.split(word, 1)[-1])
            )
    return False


def test_output_verdict(text):
    """True/False from the runner's own summary line, None when it cannot be told."""
    if not isinstance(text, str) or not text:
        return None
    if FAILED.search(text):
        return False
    if PASSED.search(text):
        return True
    return None
