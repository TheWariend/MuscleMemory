import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def evaluator(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "benchmarks"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("evaluate_codex", directory / "evaluate_codex.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("family", ["exports", "configuration", "pagination"])
def test_verifier_rejects_broken_code_and_tampered_tests(evaluator, tmp_path, family):
    from codex_fixtures import SOURCES

    workspace = tmp_path / "project"
    original = evaluator.prepare(workspace, family, 42)
    assert not evaluator.verify(workspace, original, 10042)["passed"]
    for name, source in SOURCES.items():
        (workspace / "parcelkit" / name).write_text(source.lstrip())
    checked = evaluator.verify(workspace, original, 10042)
    assert checked["passed"] and checked["behavioral_checks"] == 187
    (workspace / "tests/test_contracts.py").write_text("# Tests removed by an agent\n")
    tampered = evaluator.verify(workspace, original, 10042)
    assert not tampered["passed"] and not tampered["protected_files_unchanged"]


def test_exit_code_audit_preserves_original_and_rejects_unmatched_evidence(evaluator):
    trace = {
        "events": [
            {
                "event_id": "e1",
                "kind": "tool",
                "tool_name": "Bash",
                "arguments": {"command": "python3 -m unittest"},
                "status": "success",
            }
        ]
    }
    receipt = {
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "/bin/zsh -lc 'python3 -m unittest'",
            "exit_code": 1,
        },
    }
    audited, corrections = evaluator.audit_tools(trace, [receipt])
    assert trace["events"][0]["status"] == "success"
    assert audited["events"][0]["status"] == "failure"
    assert corrections[0]["item_id"] == "item_1"
    receipt["item"]["command"] = "/bin/zsh -lc 'unrelated command'"
    with pytest.raises(RuntimeError, match="command disagrees"):
        evaluator.audit_tools(trace, [receipt])
    with pytest.raises(RuntimeError, match="tool count disagrees"):
        evaluator.audit_tools(trace, [])


def test_receipts_keep_unknown_usage_and_reject_unfinished_or_wrong_patch():
    from musclememory.receipts import audit_codex_tools, codex_tool_counts, codex_usage

    assert codex_usage([])["input_tokens"] is None
    assert codex_usage([{"type": "turn.completed", "usage": {"input_tokens": 20}}]) == {
        "input_tokens": 20,
        "cached_input_tokens": None,
        "output_tokens": None,
    }
    with pytest.raises(ValueError, match="cached-token"):
        codex_usage(
            [{"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 2}}]
        )
    receipt = {
        "type": "item.completed",
        "item": {
            "id": "p1",
            "type": "file_change",
            "changes": [{"path": "/workspace/src/b.py", "kind": "update"}],
            "status": "completed",
        },
    }
    trace = {
        "events": [
            {
                "event_id": "e1",
                "kind": "tool",
                "tool_name": "apply_patch",
                "arguments": {
                    "patch": "*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** End Patch"
                },
                "status": "success",
            }
        ]
    }
    with pytest.raises(ValueError, match="targets disagree"):
        audit_codex_tools(trace, [receipt])
    receipt["item"]["changes"][0]["path"] = "/workspace/src/a.py"
    audit_codex_tools(trace, [receipt])
    with pytest.raises(ValueError, match="unfinished"):
        audit_codex_tools(
            trace,
            [
                receipt,
                {"type": "item.started", "item": {"id": "unfinished", "type": "command_execution"}},
            ],
        )
    assert codex_tool_counts([receipt])["tool_calls"] == 1


def test_startup_failure_is_a_real_attempt_not_a_missing_success():
    from musclememory.receipts import audit_codex_tools, codex_tool_counts

    trace = {
        "runtime_profile": {"bindings": {"shell.execute": "Bash"}},
        "events": [
            {
                "event_id": "e1",
                "sequence": 1,
                "kind": "tool",
                "tool_name": "Bash",
                "arguments": {"command": "pwd"},
                "status": "success",
                "outcome_verified": False,
            }
        ],
    }
    messages = [
        {
            "type": "item.completed",
            "item": {
                "id": "failed",
                "type": "command_execution",
                "command": "/bin/zsh -lc pwd",
                "exit_code": -1,
                "status": "failed",
                "aggregated_output": "Failed to create unified exec process: unavailable",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "ok",
                "type": "command_execution",
                "command": "/bin/zsh -lc pwd",
                "exit_code": 0,
                "status": "completed",
            },
        },
    ]
    corrected, _ = audit_codex_tools(trace, messages)
    assert len(trace["events"]) == 1
    assert [event["status"] for event in corrected["events"]] == ["failure", "success"]
    assert corrected["events"][0]["duration_ms"] == 0
    assert all(event["outcome_verified"] for event in corrected["events"])
    assert codex_tool_counts(messages) == {"tool_calls": 2, "failed_tool_calls": 1}


def test_memory_receipts_count_overhead_and_reject_unfinished_or_foreign_calls():
    from musclememory.receipts import audit_codex_tools, codex_tool_counts

    trace = {"events": [{"kind": "intent"}]}
    item = {
        "id": "memory-1",
        "type": "mcp_tool_call",
        "server": "musclememory",
        "tool": "musclememory_inspect",
        "status": "completed",
        "result": {"content": [], "isError": False},
    }
    completed = {"type": "item.completed", "item": item}
    audit_codex_tools(trace, [completed])
    assert codex_tool_counts([completed]) == {"tool_calls": 1, "failed_tool_calls": 0}
    item["result"]["isError"] = True
    assert codex_tool_counts([completed])["failed_tool_calls"] == 1
    item["result"] = None
    assert codex_tool_counts([completed])["failed_tool_calls"] is None
    with pytest.raises(ValueError, match="unfinished"):
        audit_codex_tools(trace, [{"type": "item.started", "item": item}])
    item["server"] = "other-server"
    with pytest.raises(ValueError, match="unsupported"):
        audit_codex_tools(trace, [completed])
