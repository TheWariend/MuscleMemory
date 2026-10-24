import json

import pytest
from musclememory.native_receipts import command_receipt, completed_tools


def record(source="exec-1", turn="turn-1", **fields):
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "turn_id": turn,
            "thread_id": "session-1",
            "item": {
                "type": "CommandExecution",
                "id": source,
                "command": ["/bin/zsh", "-lc", "pytest -q"],
                "cwd": "/tmp/project",
                "status": "completed",
                "exit_code": 0,
                **fields,
            },
        },
    }


def test_native_receipts_match_turn_id_command_and_directory(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [record(turn="old"), record()]))
    item = completed_tools(str(path), "turn-1")["exec-1"]
    assert command_receipt(item, {"command": "pytest -q"}, "/tmp/project")
    assert command_receipt(item, {"command": "/bin/zsh -lc 'pytest -q'"}, "/tmp/project")
    assert command_receipt(item, {"command": "pytest -x"}, "/tmp/project") is None
    assert command_receipt(item, {"command": "pytest -q"}, "/tmp/elsewhere") is None
    assert completed_tools(str(path), "missing") == {}
    assert completed_tools(str(path), "turn-1", "other-session") == {}
    assert "exec-1" in completed_tools(str(path), "turn-1", "session-1")


def test_desktop_file_urls_and_explicit_execution_directory():
    item = record(cwd="file:///tmp/other%20project")["payload"]["item"]
    assert command_receipt(
        item, {"command": "pytest -q", "workdir": "/tmp/other project"}, "/tmp/chat"
    )
    assert command_receipt(item, {"command": "pytest -q"}, "/tmp/other project")
    assert command_receipt(item, {"command": "pytest -q"}, "/tmp/chat") is None
    item["cwd"] = "file://remote-host/tmp/other%20project"
    assert command_receipt(item, {"command": "pytest -q"}, "/tmp/other project") is None


@pytest.mark.parametrize(
    "fields",
    [{"exit_code": None}, {"exit_code": True}, {"status": "in_progress"}, {"type": "McpToolCall"}],
)
def test_incomplete_or_non_command_receipts_cannot_verify_a_shell(fields):
    item = record(**fields)["payload"]["item"]
    assert command_receipt(item, {"command": "pytest -q"}, "/tmp/project") is None


def test_printed_receipts_and_conflicting_ids_are_not_native_evidence(tmp_path):
    path = tmp_path / "transcript.jsonl"
    printed = {
        "type": "response_item",
        "payload": {"type": "function_call_output", "output": json.dumps(record())},
    }
    path.write_text("\n".join(json.dumps(r) for r in [printed, record(), record(exit_code=1)]))
    assert completed_tools(str(path), "turn-1") == {}


def test_native_receipts_read_a_bounded_tail_and_skip_invalid_lines(tmp_path, monkeypatch):
    from musclememory import native_receipts

    monkeypatch.setattr(native_receipts, "MAX_RECEIPT_BYTES", 1024)
    path = tmp_path / "transcript.jsonl"
    path.write_bytes(b"x" * 2048 + b"\nnot-json\n[]\n" + json.dumps(record()).encode() + b"\n")
    assert "exec-1" in completed_tools(str(path), "turn-1")
    assert completed_tools(str(tmp_path / "missing"), "turn-1") == {}
    assert completed_tools(str(path), "") == {}
    link = tmp_path / "link.jsonl"
    link.symlink_to(path)
    assert completed_tools(str(link), "turn-1") == {}
