"""Conservative artifact and command extraction; nothing here executes a command.

Literal targets support exact lookup and producer/consumer links. Operation
categories come from a fixed vocabulary and never include argument values.
"""

import posixpath
import re
import shlex

from .security import MASK, redact

# Added by the capture hooks to a shell step: project files the command changed.
WORKSPACE_CHANGES = "$mm_workspace_changes"
PATH_KEYS = {"path", "file_path", "filename", "file", "target", "output_path"}
READERS = {"cat", "head", "tail", "sed", "rg", "grep", "ls", "find", "stat", "wc"}
TESTERS = {"pytest", "unittest", "jest", "vitest", "cargo", "go", "runtests.py", "tox", "nox"}
TEST_WORDS = {"test", "tests", "check", "unittest", "unittest.discover"}


def normalized_path(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    if redact(value) != value or MASK in value or "$mm_" in value:
        return None
    if any(c in value for c in "\n\r\0$`*?{}"):  # expansions are not literal targets
        return None
    value = posixpath.normpath(value.replace("\\", "/"))
    return None if value in {".", "/", ".."} or value.startswith("../") else value


def command_parts(command):
    if not isinstance(command, str) or len(command) > 8192 or redact(command) != command:
        return []
    try:
        words = shlex.split(command)
        if len(words) >= 3 and posixpath.basename(words[0]) in {"sh", "bash", "zsh"}:
            if words[-2] in {"-c", "-lc"}:
                words = shlex.split(words[-1])
        return words
    except ValueError:
        return []


LOOKING = {"status", "diff", "log", "show", "ls-files"}


def inspection_only(arguments, capability=""):
    """A shell command that only looks around: reading, listing, git status/diff/log."""
    command = arguments.get("command")
    if not str(capability).startswith("shell.") or arguments.get(WORKSPACE_CHANGES):
        return False
    if not isinstance(command, str) or any(char in command for char in ">`$<"):
        return False
    pieces = [piece for piece in re.split(r"&&|\|\||;|\||\n", command) if piece.strip()]
    for piece in pieces:
        words = command_parts(piece)
        if not words:
            return False
        name = posixpath.basename(words[0])
        if name == "sed" and any(word.startswith(("-i", "--in-place")) for word in words):
            return False
        if name == "find" and {"-delete", "-exec", "-execdir"} & set(words):
            return False
        looking = name == "git" and len(words) > 1 and words[1] in LOOKING
        if not (name in READERS or name == "pwd" or looking):
            return False
    return True


REDIRECT = re.compile(r"^\d*(?:>>?|<)(?:&\d+)?$|^&>$|^\d*>&\d+$")


def test_command(command):
    """Just the test run inside a longer shell command: no pipes, redirects or scripts."""
    if not isinstance(command, str) or redact(command) != command:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) >= 3 and posixpath.basename(words[0]) in {"sh", "bash", "zsh"}:
        if words[-2] in {"-c", "-lc"}:
            command = words[-1]
    for piece in reversed(re.split(r"&&|\|\||;|\||\n", command)):
        words = command_parts(piece)
        if not any(posixpath.basename(word) in TESTERS or word in TEST_WORDS for word in words):
            continue
        kept, skip = [], False
        for word in words:
            if skip:
                skip = False
            elif REDIRECT.match(word):
                skip = not re.search(r"&\d+$", word)  # `> file` names a target to drop too
            else:
                kept.append(word)
        return shlex.join(kept) if kept else None
    return None


DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
DOC_NAMES = {"changelog", "changes", "authors", "news", "history", "contributors"}


def documentation_only(arguments, effect, capability=""):
    """A change that touches only documentation, which tests never run."""
    writes = targets(arguments, effect, capability)[1]
    if not writes:
        return False
    for path in writes:
        name = posixpath.basename(path).lower()
        parts = path.lower().split("/")
        if not (
            parts[0] in {"docs", "doc"}
            or name.endswith(DOC_SUFFIXES)
            or name.split(".")[0] in DOC_NAMES
        ):
            return False
    return True


def normalized_command(command):
    words = command_parts(command)
    return shlex.join(words) if words else None


def operation_categories(arguments, capability=""):
    """Allowlisted semantics, excluding identifiers, paths, values and hashes."""
    words = command_parts(arguments.get("command"))
    operations = set()
    for word in words:
        name = posixpath.basename(word)
        if name in READERS:
            operations.add("inspect")
        if name in TESTERS or word in TEST_WORDS:
            operations.add("verify")
        if name in {"git"}:
            operations.add("version-control")
        if name in {"pip", "pip3", "npm", "pnpm", "yarn", "uv"}:
            operations.add("package")
        if name in {"python", "python3", "node", "ruby"}:
            operations.add("runtime")
    if "patch" in capability or "edit" in capability or "write" in capability:
        operations.add("modify")
    if arguments.get(WORKSPACE_CHANGES):  # a shell command that changed project files
        operations.add("modify")
    return sorted(operations)


def targets(arguments, effect, capability=""):
    reads, writes = set(), set()
    for key, value in arguments.items():
        if key in PATH_KEYS and (path := normalized_path(value)):
            (reads if effect == "read" else writes).add(path)
    changed = arguments.get(WORKSPACE_CHANGES)
    if isinstance(changed, list):
        for name in changed:
            if path := normalized_path(name):
                writes.add(path)
    patch = arguments.get("patch", arguments.get("input", arguments.get("command", "")))
    if isinstance(patch, str):
        for name in re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch, re.M):
            if path := normalized_path(name):
                writes.add(path)
    words = command_parts(arguments.get("command"))
    reader = any(posixpath.basename(word) in READERS for word in words)
    verifier = "verify" in operation_categories(arguments, capability)
    for index, word in enumerate(words):
        # Shell redirects provide explicit producers. Do not infer output from
        # arbitrary script bodies or unknown tools classified as destructive.
        if index and words[index - 1] in {">", ">>", "1>", "2>"}:
            if path := normalized_path(word):
                writes.add(path)
        elif (
            (reader or verifier)
            and not word.startswith("-")
            and re.search(r"(?:/|\.[a-zA-Z0-9]{1,8}$)", word)
        ):
            if path := normalized_path(word):
                reads.add(path)
    return sorted(reads)[:64], sorted(writes)[:64]


def extract_experience(steps):
    reads, writes, commands, verifiers, actions = set(), set(), set(), [], []
    inputs, produced, edits = set(), set(), []
    for step in steps:
        data = step.model_dump() if hasattr(step, "model_dump") else step
        before, after = targets(data["arguments"], data["effect"], data["capability"])
        reads.update(before)
        writes.update(after)
        inputs.update(set(before) - produced)
        produced.update(after)
        edits.extend(patch_edits(data["arguments"], data["capability"]))
        command = normalized_command(data["arguments"].get("command"))
        if command:
            commands.add(command)
            if "verify" in operation_categories(data["arguments"], data["capability"]):
                tested = test_command(data["arguments"].get("command")) or command
                if tested not in verifiers:
                    verifiers.append(tested)
        if before or after:
            actions.append({"step_token": data["step_token"], "reads": before, "writes": after})
    return {
        "reads": sorted(reads)[:128],
        "writes": sorted(writes)[:128],
        "commands": sorted(commands)[:128],
        "verification_commands": verifiers[-3:],
        "actions": actions[:128],
        "inputs": sorted(inputs)[:128],
        "edits": edits[:16],
    }


def edit_item(path, before, after):
    """One change: lines that stayed the same at either end are not part of it."""
    while before and after and before[0] == after[0]:
        before, after = before[1:], after[1:]
    while before and after and before[-1] == after[-1]:
        before, after = before[:-1], after[:-1]
    item = {"file": path}
    for name, lines in (("before", before), ("after", after)):
        bounded = []
        for line in lines[:8]:
            if sum(len(s) + 1 for s in [*bounded, line]) > 600:
                break
            bounded.append(line)
        if bounded:
            item[name] = "\n".join(bounded)
    return item if len(item) > 1 else None


def patch_edits(arguments, capability):
    """Bounded, literal changed lines from captured edits; never synthesize a fix."""
    if not any(word in capability for word in ("patch", "edit")):
        return []
    old, new = arguments.get("old_string"), arguments.get("new_string")
    path = normalized_path(arguments.get("file_path") or arguments.get("path"))
    if path and isinstance(old, str) and isinstance(new, str):
        # Claude Code's Edit tool: the exact text replaced, not a patch.
        if MASK in old + new or redact(old + new) != old + new:
            return []
        item = edit_item(path, old.splitlines(), new.splitlines())
        return [item] if item else []
    patch = arguments.get("patch", arguments.get("input", arguments.get("command", "")))
    if not isinstance(patch, str) or redact(patch) != patch or MASK in patch:
        return []
    result, path, before, after = [], None, [], []

    def flush():
        if path and (before or after) and (item := edit_item(path, before, after)):
            result.append(item)

    for line in patch.splitlines():
        match = re.match(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", line)
        if line.startswith("-") and not line.startswith("---"):
            before.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            after.append(line[1:])
        else:
            # A header or an unchanged line ends one change; the next is a separate edit.
            flush()
            before, after = [], []
            if match:
                path = normalized_path(match[1])
    flush()
    # Identical hunks can appear twice in a recorded retry; preserve one reference.
    return [dict(item) for item in {repr(item): item for item in result}.values()][:16]


def search_document(definition):
    experience = definition.get("experience") or extract_experience(definition["steps"])
    return "\n".join(
        [
            definition["intent_signature"],
            definition["title"],
            *experience["reads"],
            *experience["writes"],
            *experience["verification_commands"],
        ]
    )[:16000]


def semantic_document(definition):
    """Embed the task meaning separately from the exact artifact/command index."""
    return "\n".join(dict.fromkeys([definition["intent_signature"], definition["title"]]))[:8000]


def query_keys(query):
    keys = {("command", command)} if (command := normalized_command(query)) else set()
    try:
        words = shlex.split(query)
    except ValueError:
        words = []
    for word in words:
        word = word.strip("'\"`,:;()[]")
        if re.search(r"(?:/|\.[a-zA-Z0-9]{1,8}$)", word) and (path := normalized_path(word)):
            keys.add(("artifact", path))
    return keys
