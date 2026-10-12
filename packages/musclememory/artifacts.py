"""Conservative artifact and command extraction; nothing here executes a command.

Literal targets support exact lookup and producer/consumer links. Operation
categories come from a fixed vocabulary and never include argument values.
"""

import posixpath
import re
import shlex

from .security import MASK, redact

PATH_KEYS = {"path", "file_path", "filename", "file", "target", "output_path"}
READERS = {"cat", "head", "tail", "sed", "rg", "grep", "ls", "find", "stat", "wc"}
TESTERS = {"pytest", "unittest", "jest", "vitest", "cargo", "go"}


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
        if name in TESTERS or word in {"test", "tests", "check", "unittest", "unittest.discover"}:
            operations.add("verify")
        if name in {"git"}:
            operations.add("version-control")
        if name in {"pip", "pip3", "npm", "pnpm", "yarn", "uv"}:
            operations.add("package")
        if name in {"python", "python3", "node", "ruby"}:
            operations.add("runtime")
    if "patch" in capability or "edit" in capability or "write" in capability:
        operations.add("modify")
    return sorted(operations)


def targets(arguments, effect, capability=""):
    reads, writes = set(), set()
    for key, value in arguments.items():
        if key in PATH_KEYS and (path := normalized_path(value)):
            (reads if effect == "read" else writes).add(path)
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
                if command not in verifiers:
                    verifiers.append(command)
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


def patch_edits(arguments, capability):
    """Bounded, literal changed lines from captured edits; never synthesize a fix."""
    if not any(word in capability for word in ("patch", "edit")):
        return []
    patch = arguments.get("patch", arguments.get("input", arguments.get("command", "")))
    if not isinstance(patch, str) or redact(patch) != patch or MASK in patch:
        return []
    result, path, before, after = [], None, [], []

    def flush():
        if path and (before or after):
            item = {"file": path}
            for name, lines in (("before", before), ("after", after)):
                bounded = []
                for line in lines[:8]:
                    if sum(len(s) + 1 for s in [*bounded, line]) > 600:
                        break
                    bounded.append(line)
                if bounded:
                    item[name] = "\n".join(bounded)
            if len(item) > 1:
                result.append(item)

    for line in patch.splitlines():
        match = re.match(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", line)
        if match or line.startswith(("@@", "*** End Patch")):
            flush()
            before, after = [], []
            if match:
                path = normalized_path(match[1])
        elif line.startswith("-") and not line.startswith("---"):
            before.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            after.append(line[1:])
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
