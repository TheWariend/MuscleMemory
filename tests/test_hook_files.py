import json
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest
from musclememory.client_hooks import DEFAULT_TOOLS, HookQueue, run_hook
from musclememory.core import RuntimeProfile
from musclememory.delivery import render_pointer, token_count
from musclememory.hook_files import MAX_FILE_BYTES, MAX_FILES, check_files
from musclememory.workspace import project_root


def definition(*paths):
    return {
        "title": "Fix login",
        "required_capabilities": ["filesystem.edit"],
        "required_permissions": [],
        "steps": [
            {
                "step_token": f"edit-{index}",
                "capability": "filesystem.edit",
                "effect": "write",
                "arguments": {
                    "file_path": path,
                    "old_string": "old code",
                    "new_string": "new code",
                },
            }
            for index, path in enumerate(paths)
        ],
    }


@pytest.fixture
def hook(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    runtime = RuntimeProfile(
        bindings={cap: name for name, (cap, _) in DEFAULT_TOOLS["claude"].items()}
    )
    queue = HookQueue(tmp_path / "state", "a" * 64)
    token, _ = queue.handle(
        "claude",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s",
            "cwd": str(repo),
            "prompt": "Fix login",
        },
        DEFAULT_TOOLS["claude"],
        runtime,
        "team",
    )
    result = {
        "candidates": [
            {"procedure_token": "p", "procedure_version": 1, "definition": definition("login.py")}
        ],
        "plan": {"steps": [{}, {}], "gaps": []},
    }
    requests = []

    def recall(**request):
        requests.append({**request, "runtime_profile": request["runtime_profile"].model_dump()})
        return result

    def inject(budget=350):
        return queue.recall_once(
            SimpleNamespace(musclememory_recall=recall),
            token,
            "Fix login",
            runtime,
            "team",
            budget,
            str(repo),
        )

    try:
        yield repo, queue, result, requests, inject
    finally:
        queue.close()


@pytest.mark.parametrize("code", ["old code", "new code"])
def test_hook_delivers_when_before_or_after_matches(hook, code):
    repo, queue, _, requests, inject = hook
    (repo / "login.py").write_text(code)
    pointer = json.loads(inject())
    assert pointer["procedure"] == "p" and "warning" not in pointer
    assert "plan" not in pointer
    assert inject() == ""
    assert queue.db.execute("SELECT count(*) FROM injections").fetchone()[0] == 1
    assert not queue.db.execute("SELECT * FROM skips").fetchall()
    assert all("old code" not in json.dumps(request) for request in requests)


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (None, "missing_changed_file"),
        ("replacement code", "edited_code_missing"),
        ("x" * (MAX_FILE_BYTES + 1), "file_too_large"),
    ],
)
def test_hook_skips_missing_changed_or_huge_files_and_records_reason(hook, code, reason):
    repo, queue, _, _, inject = hook
    if code is not None:
        (repo / "login.py").write_text(code)
    assert inject() == ""
    assert not queue.db.execute("SELECT * FROM injections").fetchall()
    [skip] = queue.db.execute("SELECT * FROM skips").fetchall()
    assert skip["reason"] == reason
    stored = json.dumps(dict(skip))
    assert "replacement code" not in stored and "old code" not in stored


def test_deleted_changed_file_without_an_edit_also_blocks_delivery(hook):
    repo, queue, result, _, inject = hook
    (repo / "login.py").write_text("old code")
    result["candidates"][0]["definition"]["steps"].append(
        {
            "step_token": "write",
            "capability": "filesystem.write",
            "effect": "write",
            "arguments": {"file_path": "deleted.py"},
        }
    )
    assert inject() == ""
    assert queue.db.execute("SELECT reason FROM skips").fetchone()[0] == "missing_changed_file"


def test_partial_match_warning_survives_pointer_trimming(hook):
    repo, _, result, _, inject = hook
    (repo / "login.py").write_text("old code")
    (repo / "other.py").write_text("different code")
    result["candidates"][0]["definition"] = definition("login.py", "other.py")
    result["candidates"][0]["definition"]["title"] = "long title " * 20
    pointer = json.loads(inject(160))
    warning = "files changed since this skill was learned: other.py"
    assert pointer["warning"] == warning
    result["plan"] = None
    result["file_warning"] = warning
    for budget in range(80, 351, 20):
        rendered = render_pointer(result, budget)
        assert not rendered or json.loads(rendered)["warning"] == warning
        assert token_count(rendered) <= budget


@pytest.mark.parametrize("kind", ["traversal", "absolute", "file_symlink", "directory_symlink"])
def test_paths_cannot_read_outside_the_project(hook, tmp_path, kind):
    repo, queue, result, _, inject = hook
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "login.py").write_text("old code")
    if kind == "traversal":
        path = "../outside/login.py"
    elif kind == "absolute":
        path = str(outside / "login.py")
    elif kind == "file_symlink":
        (repo / "login.py").symlink_to(outside / "login.py")
        path = "login.py"
    else:
        (repo / "linked").symlink_to(outside, target_is_directory=True)
        path = "linked/login.py"
    result["candidates"][0]["definition"] = definition(path)
    assert inject() == ""
    assert queue.db.execute("SELECT reason FROM skips").fetchone()[0] in {
        "unsafe_path",
        "unsafe_or_unreadable_file",
    }


def test_file_limit_abstains_before_opening_files(hook, monkeypatch):
    repo, _, result, _, inject = hook
    result["candidates"][0]["definition"] = definition(
        *(f"file-{index}.py" for index in range(MAX_FILES + 1))
    )
    monkeypatch.setattr(
        "musclememory.hook_files.read_file",
        lambda *args, **kwargs: pytest.fail("file limit must be checked before reading"),
    )
    assert inject() == ""
    assert check_files(result["candidates"][0]["definition"], str(repo)) == ("too_many_files", [])


def test_root_resolution_and_absolute_paths_need_no_commands(tmp_path, monkeypatch):
    repo = tmp_path / "project"
    nested = repo / "app"
    nested.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /unused/worktree")
    path = repo / "login.py"
    path.write_text("new code")
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: pytest.fail("file check must not run commands")
    )
    assert project_root(str(nested)) == (str(repo.resolve()), True)
    assert check_files(definition(str(path.resolve())), str(nested)) == (None, [])


def test_unknown_files_and_bad_definitions_fail_quietly(tmp_path):
    assert check_files(definition("login.py"), None) == ("missing_project_root", [])
    assert check_files({}, str(tmp_path)) == ("file_check_error", [])
    skill = definition("login.py")
    skill["steps"][0]["arguments"] = {"file_path": "login.py"}
    assert check_files(skill, str(tmp_path)) == ("no_edit_evidence", [])


@pytest.mark.parametrize("argument", ["patch", "input", "command"])
def test_unsafe_patch_paths_are_rejected_before_extraction_drops_them(tmp_path, argument):
    skill = definition("login.py")
    skill["steps"][0].update(
        capability="filesystem.patch",
        effect="read",
        arguments={argument: "*** Update File: ../login.py\n-old code\n+new code"},
    )
    assert check_files(skill, str(tmp_path)) == ("unsafe_path", [])


def test_existing_queue_gets_persistent_skip_storage(hook):
    _, queue, _, _, inject = hook
    queue.db.execute("DROP TABLE skips")
    path, identity = queue.path, queue.identity
    upgraded = HookQueue(queue.directory, identity)
    upgraded.close()
    assert inject() == ""
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT reason FROM skips").fetchone()[0] == "missing_changed_file"
        )


@pytest.mark.parametrize("client_name", ["codex", "cursor"])
def test_prompt_and_cursor_hooks_pass_the_project_directory(
    hook, monkeypatch, tmp_path, client_name
):
    repo, _, result, _, _ = hook
    (repo / "login.py").write_text("old code")
    runtime = RuntimeProfile(
        bindings={cap: name for name, (cap, _) in DEFAULT_TOOLS[client_name].items()}
    )
    config = {"namespace": "team", "state_dir": str(tmp_path / "client-state")}
    client = SimpleNamespace(
        capture_identity="b" * 64,
        close=lambda: None,
        musclememory_recall=lambda **request: result,
        musclememory_observe=lambda **request: {
            "accepted_event_count": len(request["event_batch"]),
            "duplicate_event_count": 0,
            "rejected_event_count": 0,
        },
    )
    monkeypatch.setattr(
        "musclememory.client_hooks.load_config",
        lambda *args: (config, DEFAULT_TOOLS[client_name], runtime, client),
    )

    def call(event, **fields):
        return run_hook(
            "unused",
            client_name,
            {"hook_event_name": event, "session_id": "live", "cwd": str(repo), **fields},
        )

    output = call(
        "beforeSubmitPrompt" if client_name == "cursor" else "UserPromptSubmit", prompt="Fix login"
    )
    if client_name == "cursor":
        call("preToolUse", tool_name="Read", tool_use_id="read")
        output = call(
            "postToolUse",
            tool_name="Read",
            tool_use_id="read",
            tool_input={"file_path": "login.py"},
            tool_response="old code",
        )
        context = output["additional_context"]
    else:
        context = output["hookSpecificOutput"]["additionalContext"]
    assert json.loads(context)["procedure"] == "p"
