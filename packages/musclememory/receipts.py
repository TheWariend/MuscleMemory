"""Reconcile native capture with authoritative completed CLI tool receipts.

Original observations are immutable. Corrections become a separately linked run.
Unknown tools, missing calls, incomplete patches and unknown exit codes fail closed.
"""

import shlex
from copy import deepcopy

from .artifacts import normalized_path, targets

MEMORY_TOOLS = {
    "musclememory_recall",
    "musclememory_inspect",
    "musclememory_observe",
    "musclememory_learn",
    "musclememory_evaluate",
}


def memory_receipt(item):
    return (
        item.get("type") == "mcp_tool_call"
        and item.get("server") == "musclememory"
        and item.get("tool") in MEMORY_TOOLS
    )


def audit_codex_tools(trace, messages):
    if not trace:
        raise ValueError("No native trace to reconcile")
    tools = [event for event in trace["events"] if event["kind"] == "tool"]
    completed = [
        message["item"]
        for message in messages
        if message.get("type") == "item.completed"
        and message.get("item", {}).get("type") in {"command_execution", "file_change"}
    ]
    # A process-creation failure can occur before native hooks fire. It is an
    # attempted command with a complete CLI receipt, not a silently lost action.
    if len(completed) > len(tools):
        expanded, native_index = [], 0
        bindings = trace.get("runtime_profile", {}).get("bindings", {})
        capability = next((cap for cap, tool in bindings.items() if tool == "Bash"), None)
        for receipt in completed:
            if (
                receipt["type"] == "command_execution"
                and receipt.get("status") == "failed"
                and receipt.get("exit_code") == -1
                and receipt.get("aggregated_output", "").startswith(
                    "Failed to create unified exec process:"
                )
                and capability
            ):
                words = shlex.split(receipt["command"])
                expanded.append(
                    {
                        "event_id": "startup-" + receipt["id"],
                        "sequence": 0,
                        "kind": "tool",
                        "tool_name": "Bash",
                        "capability": capability,
                        "arguments": {"command": words[-1]},
                        "response": receipt["aggregated_output"],
                        "status": "failure",
                        "effect": "destructive",
                        "duration_ms": 0,
                        "outcome_verified": True,
                    }
                )
            elif native_index < len(tools):
                expanded.append(tools[native_index])
                native_index += 1
            else:
                raise ValueError("CLI tool count disagrees with native capture")
        if native_index != len(tools):
            raise ValueError("CLI tool count disagrees with native capture")
        trace = deepcopy(trace)
        trace["events"] = (
            [event for event in trace["events"] if event["kind"] == "intent"]
            + expanded
            + [event for event in trace["events"] if event["kind"] == "completion"]
        )
        for sequence, event in enumerate(trace["events"]):
            event["sequence"] = sequence
        tools = expanded
    if len(tools) != len(completed):
        raise ValueError("CLI tool count disagrees with native capture")
    corrected, discrepancies = deepcopy(trace), []
    audited = [event for event in corrected["events"] if event["kind"] == "tool"]
    for event, receipt in zip(audited, completed, strict=True):
        if receipt["type"] == "command_execution":
            command = shlex.split(receipt["command"])
            original = event["arguments"].get("command")
            if not command or original not in {receipt["command"], command[-1]}:
                raise ValueError("CLI command disagrees with native capture")
            if type(receipt.get("exit_code")) is not int:
                raise ValueError("Completed command has no exit-code evidence")
            status = "success" if receipt["exit_code"] == 0 else "failure"
        else:
            if event["tool_name"] != "apply_patch":
                raise ValueError("File-change receipt disagrees with native capture")
            if not event["arguments"] or "$mm_capture_omitted" in str(event["arguments"]):
                raise ValueError("Patch arguments are incomplete")
            _, expected = targets(event["arguments"], "write", "filesystem.patch")
            actual = [normalized_path(change.get("path")) for change in receipt.get("changes", [])]
            if not expected or not actual or any(path is None for path in actual):
                raise ValueError("File-change receipt has incomplete artifact evidence")
            if len(expected) != len(actual) or any(
                not any(path == target or path.endswith("/" + target) for path in actual)
                for target in expected
            ):
                raise ValueError("File-change targets disagree with native capture")
            if receipt.get("status") not in {"completed", "failed"}:
                raise ValueError("File-change receipt has no completed outcome")
            status = "success" if receipt["status"] == "completed" else "failure"
        if status != event["status"]:
            discrepancies.append(
                {
                    "event_id": event["event_id"],
                    "item_id": receipt["id"],
                    "hook_status": event["status"],
                    "cli_status": status,
                }
            )
        event["status"] = status
        event["outcome_verified"] = True
    # Started-but-never-completed CLI items and unsupported tools cannot produce
    # a complete reusable trace even if the model eventually returns an answer.
    started = {
        m["item"]["id"]
        for m in messages
        if m.get("type") == "item.started"
        and m.get("item", {}).get("type") in {"command_execution", "file_change", "mcp_tool_call"}
    }
    finished = {
        m["item"]["id"]
        for m in messages
        if m.get("type") == "item.completed"
        and m.get("item", {}).get("type") in {"command_execution", "file_change", "mcp_tool_call"}
    }
    if started - finished:
        raise ValueError("CLI execution has unfinished tool items")
    unsupported = {
        m.get("item", {}).get("type")
        for m in messages
        if m.get("type") == "item.completed" and not memory_receipt(m.get("item", {}))
    } - {"agent_message", "reasoning", "command_execution", "file_change", "todo_list", "error"}
    if unsupported:
        raise ValueError("CLI execution contains unsupported tool evidence")
    return corrected, discrepancies


def codex_usage(messages):
    receipts = [m.get("usage", {}) for m in messages if m.get("type") == "turn.completed"]
    if not receipts:
        return {"input_tokens": None, "cached_input_tokens": None, "output_tokens": None}
    result = {
        key: sum(receipt[key] for receipt in receipts)
        if all(key in receipt for receipt in receipts)
        else None
        for key in ("input_tokens", "cached_input_tokens", "output_tokens")
    }
    if any(type(value) is not int or value < 0 for value in result.values() if value is not None):
        raise ValueError("Invalid Codex token measurement")
    if result["cached_input_tokens"] is not None and (
        result["input_tokens"] is None or result["cached_input_tokens"] > result["input_tokens"]
    ):
        raise ValueError("Invalid Codex cached-token measurement")
    return result


def codex_tool_counts(messages):
    tools = [
        m["item"]
        for m in messages
        if m.get("type") == "item.completed"
        and m.get("item", {}).get("type")
        in {"command_execution", "file_change", "mcp_tool_call", "web_search", "collab_tool_call"}
    ]

    def failed(item):
        if item["type"] == "command_execution":
            return item["exit_code"] != 0 if type(item.get("exit_code")) is int else None
        if item.get("status") == "failed" or item.get("error") is not None:
            return True
        if item.get("status") != "completed":
            return None
        if item["type"] == "mcp_tool_call":
            result = item.get("result")
            return result.get("isError") is True if isinstance(result, dict) else None
        return False

    statuses = [failed(item) for item in tools]
    return {
        "tool_calls": len(tools),
        "failed_tool_calls": sum(statuses)
        if all(value is not None for value in statuses)
        else None,
    }
