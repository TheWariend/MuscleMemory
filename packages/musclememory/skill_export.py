"""Write a proven MuscleMemory skill into an agent's skills folder as SKILL.md.

Codex and Claude Code read the same skill format: a folder holding a ``SKILL.md`` with a
``name`` and ``description`` front matter. Exporting a Ready skill this way lets an agent
use it even without MuscleMemory connected.
"""

import hashlib
import os
import re
import shutil
import stat
from pathlib import Path

from .core import DomainError
from .security import MASK, redact

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_FILES = 2000
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_SKILL_MD_BYTES = 256 * 1024
TEXT_SCAN_BYTES = 1024 * 1024
AGENTS = {"codex": "Codex", "claude": "Claude Code"}


def agent_folder(agent):
    """Where an agent keeps user-level skills (CODEX_HOME / CLAUDE_CONFIG_DIR are honoured)."""
    if agent == "codex":
        base = os.getenv("CODEX_HOME") or str(Path.home() / ".codex")
    elif agent == "claude":
        base = os.getenv("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    else:
        raise DomainError("unknown_agent", "Skills can be exported to Codex or Claude Code", 422)
    return Path(base).expanduser() / "skills"


def valid_name(name):
    if not isinstance(name, str) or not NAME.match(name) or ".." in name:
        raise DomainError(
            "invalid_skill_name", "Use lowercase letters, digits, '-', '_' or '.'", 422
        )
    return name


def scan(folder):
    """A skill folder's files, sizes and SKILL.md. Refuses links, huge folders and secrets.

    Any file type is allowed (scripts, sounds, images); text files are checked for
    secrets before anything is copied.
    """
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        raise DomainError("skill_not_found", "That skill folder was not found", 404)
    files, total = [], 0
    for directory, names, filenames in os.walk(folder, followlinks=False):
        for name in names:
            if (Path(directory) / name).is_symlink():
                relative = (Path(directory) / name).relative_to(folder).as_posix()
                raise DomainError(
                    "unsupported_skill", f"{relative} is a link; copy it in as a folder", 422
                )
        names[:] = sorted(name for name in names if not name.startswith("."))
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            path = Path(directory) / filename
            relative = path.relative_to(folder).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise DomainError(
                    "unsupported_skill", f"{relative} is a link; copy it in as a file", 422
                )
            size = path.stat().st_size
            total += size
            if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                raise DomainError(
                    "skill_too_large", "Skills are limited to 2,000 files and 200 MB", 422
                )
            if size <= TEXT_SCAN_BYTES:
                try:
                    text = path.read_text("utf-8")
                except UnicodeDecodeError:
                    text = None  # a binary asset: nothing to scan
                if text is not None and (MASK in text or redact(text) != text):
                    raise DomainError(
                        "skill_has_secret", f"{relative} looks like it contains a secret", 422
                    )
            files.append({"path": relative, "size": size})
    skill = folder / "SKILL.md"
    if not skill.is_file() or skill.is_symlink():
        raise DomainError("unsupported_skill", "A skill folder needs a SKILL.md", 422)
    if skill.stat().st_size > MAX_SKILL_MD_BYTES:
        raise DomainError("skill_too_large", "SKILL.md is larger than 256 KB", 422)
    try:
        text = skill.read_text("utf-8")
    except UnicodeDecodeError:
        raise DomainError("unsupported_skill", "SKILL.md must be text", 422) from None
    return {"files": files, "bytes": total, "skill_md": text}


def folder_hash(folder, files):
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda f: f["path"]):
        digest.update(item["path"].encode() + b"\0")
        with open(Path(folder) / item["path"], "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def _build(target, write):
    """Build ``target`` in a hidden staging folder beside it, then move it into place."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.musclememory-tmp"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.mkdir()
        write(staging)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def copy_folder(source, files, target):
    """Copy the scanned files of ``source`` to a new folder ``target``."""

    def write(staging):
        for item in files:
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise DomainError("unsupported_skill", "Skill files must stay inside the folder")
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(source) / relative, destination, follow_symlinks=False)

    _build(target, write)


def install(agent, name, source):
    """Copy a skill folder into an agent's skills folder. Never replaces a different skill."""
    valid_name(name)
    scanned = scan(source)
    target = agent_folder(agent) / name
    if target.is_symlink():
        raise DomainError("skill_exists", f"{AGENTS[agent]} has a link named {name}", 409)
    if target.exists():
        try:
            existing = scan(target)
            if folder_hash(target, existing["files"]) == folder_hash(source, scanned["files"]):
                return {"agent": agent, "name": name, "installed": False, "unchanged": True}
        except DomainError:
            pass
        raise DomainError(
            "skill_exists", f"{AGENTS[agent]} already has a different skill named {name}", 409
        )
    copy_folder(source, scanned["files"], target)
    return {"agent": agent, "name": name, "installed": True, "unchanged": False}


def write_skill(folder, text):
    """Replace ``folder`` with a one-file skill holding ``text`` as SKILL.md."""
    folder = Path(folder)
    shutil.rmtree(folder, ignore_errors=True)
    _build(folder, lambda staging: (staging / "SKILL.md").write_text(text, "utf-8"))


def slug(text):
    words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return ("musclememory-" + words)[:64].rstrip("-")


def render_proven(title, summary, successes, avoid, recover):
    """A proven MuscleMemory skill as SKILL.md: when to use it, where, what, how to check."""
    first = re.split(r"(?<=[.!?])\s", title.strip())[0].rstrip(".")
    files = ", ".join(f"`{path}`" for path in summary["changes"]) or "the files named below"
    description = (
        f"{first}. A proven procedure from MuscleMemory: it changed {files} and passed "
        f"{successes} separate test {'check' if successes == 1 else 'checks'}. Use when asked to "
        f"{first[0].lower() + first[1:]} or a similar task in this project."
    ).replace("\n", " ")
    lines = [
        "---",
        f"name: {slug(first)}",
        f"description: {description[:1000]}",
        "---",
        "",
        f"# {first}",
        "",
        "Learned by MuscleMemory from a session whose tests passed after the last change,",
        f"then checked by {successes} more passing {'session' if successes == 1 else 'sessions'}.",
        "Treat it as a proven starting point: read the current files first, because the",
        "code may have changed since.",
        "",
        "## Original request",
        "",
        f"> {title.strip()}",
        "",
    ]
    if summary["reads"]:
        lines += ["## Look at first", "", *(f"- `{path}`" for path in summary["reads"]), ""]
    if summary["changes"]:
        lines += ["## Change", ""]
        edited = {edit["file"] for edit in summary["edits"]}
        lines += [f"- `{path}`" for path in summary["changes"] if path not in edited]
        for edit in summary["edits"]:
            lines += [f"In `{edit['file']}`:", "", "```diff"]
            lines += [f"- {line}" for line in edit.get("before", "").splitlines()]
            lines += [f"+ {line}" for line in edit.get("after", "").splitlines()]
            lines += ["```", ""]
    if summary["test"]:
        lines += ["## Check with", "", "```sh", summary["test"], "```", ""]
    if avoid or recover:
        lines += ["## Watch out", "", *(f"- {note}" for note in [*avoid, *recover]), ""]
    return "\n".join(lines).rstrip() + "\n", slug(first)
