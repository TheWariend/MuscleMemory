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
# Programs that only run tests, and tools that run tests with one of these subcommands.
TEST_RUNNERS = {
    "pytest", "py.test", "jest", "vitest", "mocha", "ava", "tox", "nox", "rspec", "phpunit",
    "runtests.py", "ctest",
}  # fmt: skip
TEST_SUBCOMMANDS = {
    "cargo": {"test", "nextest"}, "go": {"test"}, "npm": {"test", "t"}, "pnpm": {"test", "t"},
    "yarn": {"test"}, "bun": {"test"}, "deno": {"test"}, "make": {"test", "tests", "check"},
    "mvn": {"test", "verify"}, "gradle": {"test"}, "gradlew": {"test"}, "dotnet": {"test"},
    "mix": {"test"}, "swift": {"test"}, "rake": {"test", "spec"}, "flutter": {"test"},
    "dart": {"test"},
}  # fmt: skip
PYTHON_TEST_MODULES = {"pytest", "unittest", "tox", "nox"}
# Prefixes that run the next program: `uv run pytest`, `npx jest`, `timeout 60 go test`.
LAUNCHERS = {
    "uv": {"run"}, "poetry": {"run"}, "pipenv": {"run"}, "pdm": {"run"}, "hatch": {"run"},
    "bundle": {"exec"}, "pnpm": {"exec", "dlx"}, "yarn": {"exec", "dlx"}, "npx": None,
    "bunx": None, "env": None, "time": None, "timeout": None, "nice": None, "xvfb-run": None,
}  # fmt: skip


def runner_words(words):
    """A command without its launchers and environment: `uv run pytest -q` -> pytest -q."""
    words = list(words)
    while words:
        name = posixpath.basename(words[0])
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):  # FOO=1 pytest
            words = words[1:]
        elif name in LAUNCHERS and LAUNCHERS[name] is None:
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
            if name == "timeout" and words:
                words = words[1:]  # the duration
        elif name in LAUNCHERS and words[1:2] and words[1] in LAUNCHERS[name]:
            words = words[2:]
        else:
            break
    return words


def test_run(words):
    """True when one command runs a test suite, not when it only mentions tests."""
    words = runner_words(words)
    if not words:
        return False
    name, rest = posixpath.basename(words[0]), words[1:]
    if name in TEST_RUNNERS:
        return True
    if re.fullmatch(r"python[\d.]*", name):
        while rest and rest[0].startswith("-") and rest[0] != "-m":
            rest = rest[1:]
        if rest[:1] == ["-m"]:
            return len(rest) > 1 and rest[1] in PYTHON_TEST_MODULES
        script = posixpath.basename(rest[0]) if rest else ""
        return script in TEST_RUNNERS or (script == "manage.py" and rest[1:2] == ["test"])
    if not rest or name not in TEST_SUBCOMMANDS:
        return False
    if rest[0] in TEST_SUBCOMMANDS[name]:
        return True
    # `npm run test`, `pnpm run test:unit`
    return (
        name in {"npm", "pnpm", "yarn", "bun"}
        and rest[0] == "run"
        and len(rest) > 1
        and (rest[1] == "test" or rest[1].startswith("test:"))
    )


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
    if not isinstance(command, str):
        return False
    # Discarding or merging output writes nothing; any other redirect may.
    command = re.sub(r"\d*>\s*/dev/null|\d*>&\d+", " ", command)
    if any(char in command for char in ">`$<"):
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
        if not test_run(words):
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


# Flags that only change how results are reported, never which tests run.
QUIET = {
    "pytest": {"-v", "-vv", "-vvv", "-q", "-qq", "-x", "-s", "-l", "-rA", "-ra", "--verbose",
               "--quiet", "--exitfirst", "--no-header", "--color=yes", "--color=no",
               "--disable-warnings", "-p no:cacheprovider"},
    "unittest": {"-v", "-q", "-f", "-b", "-c", "--locals", "--verbose", "--quiet", "--failfast",
                 "--buffer", "--catch"},
}  # fmt: skip
VALUED = {"pytest": {"--tb", "--maxfail", "--durations", "-n", "--timeout", "--rootdir", "-W"},
          "unittest": {"-t", "--top-level-directory"}}  # fmt: skip


def test_selection(command):
    """(runner, tests run) for a pytest or unittest command, or None for anything else.

    Tests run are paths; an empty set is the whole suite. Options that narrow the run in
    ways we can't compare (-k, -m, -p, a config file) make the selection unknown."""
    tested = test_command(command)
    if not tested:
        return None
    words = runner_words(command_parts(tested))
    if not words:
        return None
    name, rest = posixpath.basename(words[0]), words[1:]
    if re.fullmatch(r"python[\d.]*", name):
        while rest and rest[0].startswith("-") and rest[0] != "-m":
            rest = rest[1:]
        if rest[:1] != ["-m"] or len(rest) < 2:
            return None
        runner, rest = rest[1], rest[2:]
    elif name in {"pytest", "py.test"}:
        runner = "pytest"
    else:
        return None
    if runner not in QUIET:
        return None
    if runner == "unittest" and rest[:1] == ["discover"]:
        rest = rest[1:]
        start, skip = ".", False
        for index, word in enumerate(rest):
            if skip:
                skip = False
            elif word in {"-s", "--start-directory"}:
                start, skip = rest[index + 1] if index + 1 < len(rest) else ".", True
            elif word in {"-t", "--top-level-directory"}:
                skip = True
            elif word in QUIET["unittest"]:
                continue
            else:
                return None  # a pattern or anything else narrows the run
        start = posixpath.normpath(start)
        return runner, frozenset() if start == "." else frozenset({start})
    tests, skip = set(), False
    for word in rest:
        if skip:
            skip = False
        elif word in QUIET[runner] or any(word.startswith(flag + "=") for flag in VALUED[runner]):
            continue
        elif word in VALUED[runner]:
            skip = True
        elif word.startswith("-"):
            return None
        else:
            path = word
            if runner == "unittest" and not word.endswith(".py") and "/" not in word:
                # tests.test_contracts.TestX.test_y -> tests/test_contracts.py::TestX::test_y
                parts = word.split(".")
                module = next((i for i in range(len(parts), 0, -1)
                               if parts[i - 1].startswith("test")), len(parts))  # fmt: skip
                path = "/".join(parts[:module]) + ".py"
                if parts[module:]:
                    path += "::" + "::".join(parts[module:])
            tests.add(
                posixpath.normpath(path.split("::")[0])
                + ("::" + path.split("::", 1)[1] if "::" in path else "")
            )
    return runner, frozenset(tests)


def test_covers(check, skill):
    """A check proves the skill's test if it ran the same tests or more with the same runner."""
    if not check or not skill:
        return False
    if test_command(check) == test_command(skill):
        return True
    ran, needed = test_selection(check), test_selection(skill)
    if not ran or not needed or ran[0] != needed[0]:
        return False
    if not ran[1]:
        return True  # the whole suite
    if not needed[1]:
        return False

    def within(test, scope):
        return (
            test == scope
            or test.startswith(scope.rstrip("/") + "/")
            or test.startswith(scope + "::")
        )

    return all(any(within(test, scope) for scope in ran[1]) for test in needed[1])


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
        if name in {"git"}:
            operations.add("version-control")
        if name in {"pip", "pip3", "npm", "pnpm", "yarn", "uv"}:
            operations.add("package")
        if name in {"python", "python3", "node", "ruby"}:
            operations.add("runtime")
    if test_command(arguments.get("command")):
        operations.add("verify")
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


IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def added_words(arguments, capability):
    """Identifiers an edit or write added, or None when its content wasn't captured."""
    content = arguments.get("content")
    if "write" in capability and isinstance(content, str):
        return set(IDENTIFIER.findall(content))
    edits = patch_edits(arguments, capability)
    if not edits:
        return None
    return {
        word
        for item in edits
        for word in set(IDENTIFIER.findall(item.get("after", "")))
        - set(IDENTIFIER.findall(item.get("before", "")))
    }


def line_changes(arguments, capability):
    """Lines an edit or write added or removed, as "file<TAB>+line" / "file<TAB>-line"
    (stripped); None when the content wasn't captured."""
    path = normalized_path(arguments.get("file_path") or arguments.get("path")) or ""
    content = arguments.get("content")
    if "write" in capability and isinstance(content, str):
        return {f"{path}\t+{line.strip()}" for line in content.splitlines() if line.strip()}
    old, new = arguments.get("old_string"), arguments.get("new_string")
    if "edit" in capability and isinstance(old, str) and isinstance(new, str):
        before = {line.strip() for line in old.splitlines() if line.strip()}
        after = {line.strip() for line in new.splitlines() if line.strip()}
        return {f"{path}\t+{line}" for line in after - before} | {
            f"{path}\t-{line}" for line in before - after
        }
    patch = arguments.get("patch", arguments.get("input"))
    if "patch" in capability and isinstance(patch, str):
        changes = set()
        for line in patch.splitlines():
            header = re.match(r"^(?:\*\*\* (?:Update|Add|Delete) File: |\+\+\+ (?:b/)?)(.+)$", line)
            if header:
                path = normalized_path(header[1]) or ""
            elif line[:1] in "+-" and not line.startswith("---") and line[1:].strip():
                changes.add(f"{path}\t{line[0]}{line[1:].strip()}")
        return changes
    return None


def proven_edits(edits, unproven):
    """Edits without the lines no independent check reproduced; says how many were withheld."""
    if not unproven:
        return edits
    result = []
    for item in edits:
        item, withheld = dict(item), 0
        for name, sign in (("after", "+"), ("before", "-")):
            lines = item.get(name, "").splitlines()
            kept = [
                line for line in lines if f"{item['file']}\t{sign}{line.strip()}" not in unproven
            ]
            if len(kept) < len(lines):
                withheld += len(lines) - len(kept)
                if name == "before":
                    # An unproven removal: don't show it as something to take out.
                    kept = []
                if kept:
                    item[name] = "\n".join(kept)
                else:
                    item.pop(name, None)
        if withheld:
            if not ({"before", "after"} & set(item)):
                continue
            item["unproven_lines_withheld"] = withheld
        result.append(item)
    return result


def withhold_unproven(definition, unproven):
    """A copy of a skill whose recorded changes lack the lines no check reproduced.

    A shell command that changed files can't be compared line by line, so its text is
    withheld too; the files it changed stay visible."""
    steps = []
    for step in definition["steps"]:
        arguments = dict(step["arguments"])
        if arguments.get(WORKSPACE_CHANGES) and isinstance(arguments.get("command"), str):
            arguments["command"] = "[withheld: a command that changed files]"
            steps.append({**step, "arguments": arguments})
            continue
        if not unproven:
            steps.append(step)
            continue
        path = normalized_path(arguments.get("file_path") or arguments.get("path")) or ""

        def keep(text, sign, path=path):
            lines = text.splitlines()
            kept = [line for line in lines if f"{path}\t{sign}{line.strip()}" not in unproven]
            return "\n".join(kept), len(lines) - len(kept)

        withheld = 0
        if isinstance(arguments.get("content"), str) and "write" in step["capability"]:
            arguments["content"], withheld = keep(arguments["content"], "+")
        elif isinstance(arguments.get("new_string"), str) and "edit" in step["capability"]:
            arguments["new_string"], withheld = keep(arguments["new_string"], "+")
        elif "patch" in step["capability"]:
            key = "patch" if isinstance(arguments.get("patch"), str) else "input"
            if isinstance(arguments.get(key), str):
                lines, current = [], ""
                for line in arguments[key].splitlines():
                    header = re.match(
                        r"^(?:\*\*\* (?:Update|Add|Delete) File: |\+\+\+ (?:b/)?)(.+)$", line
                    )
                    if header:
                        current = normalized_path(header[1]) or ""
                    elif line[:1] in "+-" and not line.startswith("---"):
                        if f"{current}\t{line[0]}{line[1:].strip()}" in unproven:
                            withheld += 1
                            if line[0] == "+":
                                continue
                            line = " " + line[1:]
                    lines.append(line)
                arguments[key] = "\n".join(lines)
        step = {**step, "arguments": arguments}
        if withheld:
            step["unproven_lines_withheld"] = withheld
        steps.append(step)
    return {**definition, "steps": steps}


def first_sentence(text):
    """The opening sentence of a request: enough to name a skill, too little to carry an
    instruction appended later in the same request."""
    text = text.strip().split("\n", 1)[0]
    match = re.match(r"(.+?[.!?])(?:\s|$)", text)
    return (match[1] if match else text)[:200]


def search_document(definition):
    experience = definition.get("experience") or extract_experience(definition["steps"])
    browser_labels = []
    for step in definition["steps"]:
        if step.get("capability") != "browser.act":
            continue
        context = step.get("arguments", {}).get("mm_browser")
        if not isinstance(context, dict) or context.get("version") != 1:
            continue
        target = context.get("target")
        if not isinstance(target, dict):
            continue
        names = [target.get("name")]
        landmarks = target.get("landmarks", [])
        if isinstance(landmarks, list) and len(landmarks) <= 12:
            names.extend(landmarks)
        browser_labels.extend(
            name for name in names if isinstance(name, str) and 0 < len(name) <= 300
        )
    return "\n".join(
        [
            definition["intent_signature"],
            definition["title"],
            *experience["reads"],
            *experience["writes"],
            *experience["verification_commands"],
            *dict.fromkeys(browser_labels),
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
        # A path, or a name with a file extension; "0.9" or "v1.2" is a number, not a file.
        if re.search(r"(?:/|\.[a-zA-Z0-9]*[a-zA-Z][a-zA-Z0-9]*$)", word) and (
            path := normalized_path(word)
        ):
            keys.add(("artifact", path))
    return keys


TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec)\.\w+$"
)


def needs_test_proof(definition):
    """A skill run against a test that already existed must show that test catching the bug.

    Skills that write or change their own test can't fail it first; a person reviews
    that test instead.
    """
    experience = extract_experience(definition["steps"])
    return bool(experience["verification_commands"]) and not any(
        TEST_PATH.search(path) for path in experience["writes"]
    )
