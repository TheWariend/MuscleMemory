import copy
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from musclememory.client_hooks import CODEX_CONTEXT_TOOLS, DEFAULT_TOOLS, HookQueue, digest
from musclememory.core import ObserveInput
from musclememory.recovery import RecoverySkipped, read_native_turns, recover_native_run
from musclememory.sdk import MuscleMemoryClient


def recording(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    session, turn = str(uuid4()), str(uuid4())
    profile = {
        "agent_name": "codex",
        "bindings": {capability: name for name, (capability, _) in DEFAULT_TOOLS["codex"].items()},
        "environment": {},
        "permissions": [],
    }
    row = {
        "token": str(uuid4()),
        "session": digest(["codex", session]),
        "turn": turn,
        "created": datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp(),
        "scope": "team",
        "profile": json.dumps(profile),
        "closed": 1,
        "ended": 1,
        "gaps": json.dumps(["parallel_tool_dependencies_unknown", "unverified_exit_status"]),
    }
    captured = [
        (
            "intent",
            {
                "event_id": "e0",
                "sequence": 0,
                "kind": "intent",
                "intent_text": "Fix integer rounding",
                "status": "success",
            },
        )
    ]
    native = {
        "session": session,
        "turn": turn,
        "cwd": str(root),
        "errors": [],
        "started": "2026-10-20T00:00:00Z",
        "completed": "2026-10-20T00:00:10Z",
        "items": {
            "edit": {
                "start": 1000,
                "end": 2000,
                "item": {
                    "type": "FileChange",
                    "id": "edit",
                    "status": "completed",
                    "changes": {
                        str(root / "rounding.py"): {
                            "type": "update",
                            "unified_diff": "@@\n-int(x)\n+round(x)",
                        }
                    },
                },
            },
            "test": {
                "start": 3000,
                "end": 4000,
                "item": {
                    "type": "CommandExecution",
                    "id": "test",
                    "command": ["/bin/zsh", "-lc", "pytest -q"],
                    "cwd": root.as_uri(),
                    "exit_code": 0,
                    "status": "completed",
                    "aggregated_output": "10 passed",
                },
            },
        },
    }
    return row, captured, native


def test_native_recovery_proves_parallel_capture_with_file_url_and_preserves_source(tmp_path):
    row, captured, native = recording(tmp_path)
    before = copy.deepcopy((row, captured, native))
    result = recover_native_run(row, captured, native, CODEX_CONTEXT_TOOLS)
    assert result["run_token"] != row["token"]
    assert result == recover_native_run(row, captured, native, CODEX_CONTEXT_TOOLS)
    assert (row, captured, native) == before
    assert result["events"][-1]["response"]["source_run"] == row["token"]
    assert result["events"][-1]["response"]["scope"] == "coding_segment"
    assert "*** Update File: rounding.py" in result["events"][1]["arguments"]["input"]
    assert all(not event["dependencies"] for event in result["events"])


@pytest.mark.parametrize("when", [0, 5000])
def test_external_actions_outside_proven_coding_segment_are_not_taught(tmp_path, when):
    row, captured, native = recording(tmp_path)
    native["items"]["browser"] = {
        "start": when,
        "end": when + 1,
        "item": {"type": "McpToolCall", "server": "cua_repl", "tool": "js"},
    }
    recovered = recover_native_run(row, captured, native, CODEX_CONTEXT_TOOLS)
    assert len(recovered["events"]) == 4
    assert {event.get("tool_name") for event in recovered["events"]} == {
        None,
        "apply_patch",
        "Bash",
    }


def test_commit_does_not_invalidate_tests_or_enter_recovered_guidance(tmp_path):
    row, captured, native = recording(tmp_path)
    native["items"]["commit"] = {
        "start": 5000,
        "end": 6000,
        "item": {
            "type": "CommandExecution",
            "id": "commit",
            "status": "completed",
            "exit_code": 0,
            "cwd": native["cwd"],
            "command": ["/bin/sh", "-c", "git add rounding.py && git commit -m fix"],
        },
    }
    captured.append(
        ("commit", {"kind": "tool", "arguments": {"$mm_workspace_changes": ["rounding.py"]}})
    )
    recovered = recover_native_run(row, captured, native, CODEX_CONTEXT_TOOLS)
    assert len(recovered["events"]) == 4


def test_workspace_snapshot_preserves_cleaned_paths_after_commit(tmp_path):
    import subprocess

    from musclememory.workspace import changed_files, snapshot

    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init")
    (root / "code.py").write_text("print('tested')\n")
    before = snapshot(str(root))
    git("add", "code.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "test")
    after = snapshot(str(root), before["files"])
    assert changed_files(before, after) == []
    (root / "code.py").write_text("print('changed after tests')\n")
    assert changed_files(after, snapshot(str(root), after["files"])) == ["code.py"]


@pytest.mark.parametrize(
    "case,reason",
    [
        ("session", "session_or_turn_mismatch"),
        ("open", "unfinished_turn"),
        ("failed", "final_test_failed"),
        ("overlap", "test_did_not_follow_changes"),
        ("later_edit", "test_did_not_follow_changes"),
        ("browser", "external_action_inside_coding_segment"),
        ("missing_result", "missing_native_tool_result"),
        ("no_timing", "missing_native_timing"),
        ("pipe", "unverified_piped_test"),
    ],
)
def test_recovery_does_not_invent_outcomes(tmp_path, case, reason):
    row, captured, native = recording(tmp_path)
    if case == "session":
        native["session"] = "other"
    elif case == "open":
        row["closed"] = 0
    elif case == "failed":
        native["items"]["test"]["item"]["exit_code"] = 1
    elif case == "overlap":
        native["items"]["edit"]["end"] = 3500
    elif case == "later_edit":
        native["items"]["edit"].update(start=5000, end=6000)
    elif case == "browser":
        native["items"]["browser"] = {
            "start": 1500,
            "end": 1600,
            "item": {"type": "McpToolCall", "server": "cua_repl", "tool": "js"},
        }
    elif case == "missing_result":
        captured.append(("missing", {"kind": "tool"}))
    elif case == "no_timing":
        native["items"]["edit"]["start"] = None
    elif case == "pipe":
        native["items"]["test"]["item"].update(
            command=["/bin/sh", "-c", "pytest -q | tail -1"], aggregated_output="done"
        )
    with pytest.raises(RecoverySkipped, match=reason):
        recover_native_run(row, captured, native, CODEX_CONTEXT_TOOLS)


def write_transcript(path, native):
    records = [{"type": "session_meta", "payload": {"id": native["session"], "cwd": native["cwd"]}}]
    for kind, timestamp in [
        ("task_started", native["started"]),
        ("task_complete", native["completed"]),
    ]:
        records.append(
            {
                "type": "event_msg",
                "timestamp": timestamp,
                "payload": {"type": kind, "turn_id": native["turn"]},
            }
        )
    for entry in native["items"].values():
        records.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": native["session"],
                    "turn_id": native["turn"],
                    "item": entry["item"],
                    "started_at_ms": entry["start"],
                    "completed_at_ms": entry["end"],
                },
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_native_reader_rejects_embedded_receipts_wrong_sessions_and_symlinks(tmp_path):
    _, _, native = recording(tmp_path)
    path = tmp_path / "native.jsonl"
    write_transcript(path, native)
    assert read_native_turns(path)[native["turn"]] == native
    with path.open("a") as output:
        output.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {"type": "function_call_output", "output": json.dumps(native)},
                }
            )
            + "\n"
        )
        output.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "thread_id": "wrong",
                        "turn_id": native["turn"],
                        "item": {"id": "injected", "type": "CommandExecution"},
                    },
                }
            )
            + "\n"
        )
    assert "injected" not in read_native_turns(path)[native["turn"]]["items"]
    link = tmp_path / "alias"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_native_turns(link)


def test_live_native_recovery_waits_for_completion_and_is_idempotent(
    service, principal, credential, tmp_path
):
    row, captured, native = recording(tmp_path)
    queue = HookQueue(tmp_path / "queue", "fixture")
    with queue.transaction():
        queue.db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row["token"],
                "fixture",
                row["session"],
                row["turn"],
                row["scope"],
                row["profile"],
                row["ended"],
                row["closed"],
                row["gaps"],
                row["created"],
            ),
        )
        queue.db.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?)",
            (row["token"], 0, "intent", digest(captured[0][1]), json.dumps(captured[0][1]), 1),
        )

    class Sink:
        def musclememory_observe(self, **args):
            return service.observe(principal, ObserveInput(**args))

        def auto_learn(self, token):
            return service.auto_learn(principal, token)

    transcript = tmp_path / "late.jsonl"
    write_transcript(transcript, native)
    lines = transcript.read_text().splitlines()
    transcript.write_text("\n".join(line for line in lines if '"task_complete"' not in line) + "\n")
    assert queue.recover_native(Sink(), row["token"], str(transcript)) is None
    assert service.listing(principal, "team", "procedures")["items"] == []
    write_transcript(transcript, native)
    assert queue.recover_native(Sink(), row["token"], str(transcript))["action"] == "learned"
    assert queue.recover_native(Sink(), row["token"], str(transcript)) is None
    assert len(service.listing(principal, "team", "procedures")["items"]) == 1
    queue.close()


def test_migration_backs_up_learns_once_and_keeps_original(
    service, principal, credential, tmp_path, monkeypatch, fixture_learner
):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "recover_recorded", Path(__file__).parents[1] / "scripts/recover_recorded_skills.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from musclememory.service import MemoryService

    monkeypatch.setattr(
        module, "MemoryService", lambda settings: MemoryService(settings, learner=fixture_learner)
    )
    row, captured, native = recording(tmp_path)
    folder = tmp_path / "mm-home"
    folder.mkdir(mode=0o700)
    (folder / "hooks").mkdir()
    monkeypatch.setenv("MM_HOME", str(folder))
    monkeypatch.setenv("MM_DATABASE_URL", service.settings.database_url)
    monkeypatch.setenv("MM_API_KEY", credential["api_key"])
    (folder / "config.json").write_text(
        json.dumps({"database_url": service.settings.database_url, "url": "http://127.0.0.1:8080"})
    )
    (folder / "hooks/codex.json").write_text(json.dumps({"state_dir": str(folder / "state")}))
    client = MuscleMemoryClient("http://127.0.0.1:8080", credential["api_key"])
    queue = HookQueue(folder / "state", client.capture_identity)
    with queue.transaction():
        queue.db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row["token"],
                client.capture_identity,
                row["session"],
                row["turn"],
                row["scope"],
                row["profile"],
                row["ended"],
                row["closed"],
                row["gaps"],
                row["created"],
            ),
        )
        queue.db.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?)",
            (row["token"], 0, "intent", digest(captured[0][1]), json.dumps(captured[0][1]), 1),
        )
    source_events = [
        captured[0][1],
        {
            "event_id": "old-completion",
            "sequence": 1,
            "kind": "completion",
            "status": "failure",
            "outcome_verified": False,
        },
    ]
    service.observe(
        principal,
        ObserveInput(
            run_token=row["token"],
            runtime_profile=json.loads(row["profile"]),
            visibility_scope="team",
            event_batch=source_events,
        ),
    )
    original = service.get_run(principal, row["token"])
    codex = tmp_path / "codex"
    (codex / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex))
    write_transcript(codex / "sessions" / f"rollout-{native['session']}.jsonl", native)
    assert module.migrate_recordings()["candidate_count"] == 1
    assert service.listing(principal, "team", "procedures")["items"] == []
    result = module.migrate_recordings(True)
    assert result["actions"] == {"learned": 1}
    assert Path(result["backup"]).is_file()
    assert Path(result["backup"]).stat().st_mode & 0o777 == 0o600
    assert module.migrate_recordings(True)["actions"] == {"already_learned": 1}
    assert len(service.listing(principal, "team", "procedures")["items"]) == 1
    assert service.get_run(principal, row["token"]) == original
    queue.close()
    client.close()
