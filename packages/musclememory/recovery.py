"""Recover independently test-proven coding traces from native Desktop receipts.

Original hook events are immutable. A recovered trace is a deterministic, linked
copy, containing actual tool results, never a replay or an invented attestation.
"""

import hashlib
import json
import os
import re
import shlex
import stat
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from . import workspace
from .artifacts import (
    WORKSPACE_CHANGES,
    command_parts,
    documentation_only,
    inspection_only,
    operation_categories,
    targets,
)
from .core import ExecutionEvent
from .native_receipts import local_directory
from .security import redact

RECOVERY_VERSION = "native-recovery-v1"
MAX_TRANSCRIPT_BYTES = 128 * 1024 * 1024


class RecoverySkipped(ValueError):
    pass


def read_native_turns(path, wanted=None):
    """Stream native envelopes, binding every item to the transcript's session.

    Unlike live tail lookup, migration must see the beginning of long turns.
    Duplicate conflicting items, incomplete turns and oversized files fail closed.
    """
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TRANSCRIPT_BYTES:
            raise RecoverySkipped("transcript_size_or_type")
        session, cwd, turns = None, None, {}
        for line in stream:
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(record, dict):
                continue
            event = record.get("payload")
            if not isinstance(event, dict):
                continue
            if record.get("type") == "session_meta":
                session = event.get("id", event.get("session_id"))
                cwd = local_directory(event.get("cwd"))
            if record.get("type") != "event_msg" or not session:
                continue
            turn = event.get("turn_id")
            if not turn or (wanted is not None and turn not in wanted):
                continue
            state = turns.setdefault(
                turn, {"session": session, "turn": turn, "cwd": cwd, "items": {}, "errors": []}
            )
            kind = event.get("type")
            if kind == "task_started":
                state["started"] = record.get("timestamp")
            elif kind == "task_complete":
                state["completed"] = record.get("timestamp")
            elif kind in {"turn_aborted", "task_failed"}:
                state["errors"].append("interrupted_turn")
            elif kind == "item_completed" and event.get("thread_id") == session:
                item = event.get("item")
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                entry = {
                    "item": item,
                    "start": event.get("started_at_ms"),
                    "end": event.get("completed_at_ms"),
                }
                previous = state["items"].get(item["id"])
                if previous is not None and previous != entry:
                    state["errors"].append("conflicting_native_item")
                state["items"][item["id"]] = entry
    return turns


def _command(item):
    argv = item.get("command")
    if not isinstance(argv, list) or not argv or not all(isinstance(w, str) for w in argv):
        raise RecoverySkipped("missing_command_arguments")
    if len(argv) >= 3 and argv[-2] in {"-c", "-lc"}:
        return argv[-1]
    return shlex.join(argv)


def _patch(item, original, root):
    changes = item.get("changes")
    if not isinstance(changes, dict) or not changes:
        if original:
            return original["arguments"]
        raise RecoverySkipped("missing_patch_arguments")
    lines = ["*** Begin Patch"]
    for name, change in changes.items():
        if not isinstance(change, dict) or change.get("move_path"):
            raise RecoverySkipped("unsupported_patch_change")
        operation = {"update": "Update", "add": "Add", "delete": "Delete"}.get(change.get("type"))
        diff = change.get("unified_diff")
        if operation in {"Add", "Delete"} and isinstance(change.get("content"), str):
            prefix = "+" if operation == "Add" else "-"
            diff = "\n".join(prefix + line for line in change["content"].splitlines())
        if operation is None or not isinstance(diff, str):
            raise RecoverySkipped("missing_patch_arguments")
        lines += [f"*** {operation} File: {workspace.relative_path(name, [root])}", diff]
    return {"input": "\n".join([*lines, "*** End Patch"])}


def recover_native_run(row, captured, native, context_tools):
    """Build a trace only when native timing proves tests followed all changes.

    Parallel work is retained as independent actions without invented dependency
    edges. A test overlapping any possible mutation cannot prove the final state.
    External and browser actions inside the coding segment prevent its recovery.
    Actions outside it are omitted; they are never asserted to have succeeded.
    """
    expected_session = hashlib.sha256(
        json.dumps(["codex", native["session"]], sort_keys=True).encode()
    ).hexdigest()
    if row["session"] != expected_session or row["turn"] != native["turn"]:
        raise RecoverySkipped("session_or_turn_mismatch")
    if not row["closed"] or not row["ended"] or not native.get("completed"):
        raise RecoverySkipped("unfinished_turn")
    if not native.get("started") or native["errors"]:
        raise RecoverySkipped("incomplete_native_turn")
    originals = {source: event for source, event in captured if event["kind"] == "tool"}
    missing = set(originals) - set(native["items"])
    if any(
        not inspection_only(
            originals[source].get("arguments", {}), originals[source].get("capability", "")
        )
        for source in missing
    ):
        raise RecoverySkipped("missing_native_tool_result")
    # Old gap names are harmless only if every corresponding tool now has a
    # native result or an explicit context-only mapping.
    native_tools = {
        f"mcp__{entry['item'].get('server')}__{entry['item'].get('tool')}".replace(".", "__")
        for entry in native["items"].values()
        if entry["item"].get("type") == "McpToolCall"
    }
    for gap in json.loads(row["gaps"]):
        if gap.startswith("unmapped_tool:"):
            name = gap.partition(":")[2]
            if (
                name not in context_tools
                and "musclememory_" not in name
                and name not in native_tools
            ):
                raise RecoverySkipped("unsupported_tool:" + name)
        elif gap not in {
            "unverified_exit_status",
            "parallel_tool_dependencies_unknown",
            "unfinished_tool",
            "tool_after_stop",
            "configuration_changed",
        }:
            raise RecoverySkipped(gap)
    profile = json.loads(row["profile"])
    profile["bindings"].update({"shell.execute": "Bash", "filesystem.patch": "apply_patch"})
    intent = next(event for _, event in captured if event["kind"] == "intent")
    events = [
        ExecutionEvent(
            event_id="intent",
            sequence=0,
            kind="intent",
            intent_text=intent["intent_text"],
            occurred_at=datetime.fromtimestamp(row["created"], timezone.utc),
        )
    ]
    actions, barriers = [], []
    for source, entry in native["items"].items():
        item, start, end = entry["item"], entry["start"], entry["end"]
        kind = item.get("type")
        if kind in {"Reasoning", "AgentMessage", "UserMessage", "ImageView", "ContextCompaction"}:
            continue
        if kind == "Extension" and item.get("kind") in {"web.search", "web.run", "web.open"}:
            continue
        if kind == "McpToolCall":
            name = f"mcp__{item.get('server')}__{item.get('tool')}".replace(".", "__")
            if name in context_tools or "musclememory_" in name:
                continue
            if type(start) is not int or type(end) is not int or start > end:
                raise RecoverySkipped("unsupported_tool:" + name)
            barriers.append((start, end, name))
            continue
        if kind not in {"CommandExecution", "FileChange"}:
            raise RecoverySkipped("unsupported_native_item:" + str(kind))
        if type(start) is not int or type(end) is not int or start > end:
            raise RecoverySkipped("missing_native_timing")
        if item.get("status") not in {"completed", "failed"}:
            raise RecoverySkipped("unfinished_native_tool")
        original = originals.get(source)
        directory = local_directory(item.get("cwd")) if kind == "CommandExecution" else None
        if kind == "FileChange":
            paths = list(item.get("changes", {}))
            directory = (
                workspace.project_root(os.path.dirname(paths[0]))[0] if paths else native["cwd"]
            )
        if not directory:
            raise RecoverySkipped("missing_execution_directory")
        root = workspace.project_root(directory)[0] or directory
        response = {
            "capture_source": RECOVERY_VERSION,
            "native_call_id": source,
            "started_at_ms": start,
            "completed_at_ms": end,
            "cwd": directory,
        }
        failed = item["status"] == "failed"
        if kind == "CommandExecution":
            if type(item.get("exit_code")) is not int:
                raise RecoverySkipped("missing_native_exit_code")
            command = _command(item)
            args = {
                "command": workspace.relative_command(command, workspace.roots(directory, root))
            }
            # Old workspace snapshots are valid only for the same project. Native
            # execution cwd, rather than the chat cwd, owns this command.
            if (
                original
                and native["cwd"]
                and workspace.project_root(native["cwd"])[0] == root
                and not metadata_only(command)
                and not workspace_inspection(item, args)
            ):
                changes = original["arguments"].get(WORKSPACE_CHANGES)
                if changes:
                    args[WORKSPACE_CHANGES] = changes
            writes = targets(args, "destructive", "shell.execute")[1]
            if writes:
                args[WORKSPACE_CHANGES] = writes
            response.update(
                exit_code=item["exit_code"], output=str(item.get("aggregated_output", ""))[-12000:]
            )
            failed |= item["exit_code"] != 0
            tool, capability, effect = "Bash", "shell.execute", "destructive"
            inspect = workspace_inspection(item, args)
        else:
            args = _patch(item, original, root)
            response["output"] = str(item.get("stdout", ""))[-4000:]
            tool, capability, effect, inspect = "apply_patch", "filesystem.patch", "write", False
        args = redact(args)
        if len(json.dumps(args).encode()) > 48000:
            raise RecoverySkipped("oversized_arguments")
        event = ExecutionEvent(
            event_id=f"tool-{len(actions)}",
            sequence=0,
            kind="tool",
            tool_name=tool,
            capability=capability,
            arguments=args,
            response=redact(response),
            status="failure" if failed else "success",
            exploratory=inspect,
            effect=effect,
            duration_ms=end - start,
        )
        actions.append((start, end, root, inspect, event))
    actions.sort(key=lambda action: (action[1], action[0], action[4].event_id))
    changes = [
        a
        for a in actions
        if "modify" in operation_categories(a[4].arguments, a[4].capability)
        and not documentation_only(a[4].arguments, a[4].effect, a[4].capability)
    ]
    if not changes:
        raise RecoverySkipped("no_recorded_code_change")
    # Keep project scope explicit. Tests in another checkout cannot validate edits.
    roots = {a[2] for a in changes}
    if len(roots) != 1:
        raise RecoverySkipped("changes_in_multiple_projects")
    root = roots.pop()
    checks = [
        a
        for a in actions
        if a[2] == root and "verify" in operation_categories(a[4].arguments, a[4].capability)
    ]
    if not checks:
        raise RecoverySkipped("no_test_command")
    final = checks[-1]
    if final[4].status != "success":
        raise RecoverySkipped("final_test_failed")
    if any(a[1] > final[0] for a in changes if a is not final):
        raise RecoverySkipped("test_did_not_follow_changes")
    first_change = min(a[0] for a in changes)
    if any(start <= final[1] and end >= first_change for start, end, _ in barriers):
        raise RecoverySkipped("external_action_inside_coding_segment")
    segment_start = max((end for _, end, _ in barriers if end < first_change), default=0)
    # Even a shell without a known target may mutate files. Require it to finish
    # before the verifier starts, or begin after the verifier ends.
    if any(a is not final and not a[3] and a[0] < final[1] and a[1] > final[0] for a in actions):
        raise RecoverySkipped("test_overlapped_possible_mutation")
    command = final[4].arguments.get("command", "")
    if (
        workspace.piped_test(command_parts(command), command)
        and workspace.test_output_verdict(final[4].response["output"]) is not True
    ):
        raise RecoverySkipped("unverified_piped_test")
    for start, end, project, _, event in actions:
        if start < segment_start or end > final[1] or project != root:
            continue
        event.sequence = len(events)
        events.append(event)
    evidence = final[4].event_id
    fingerprint = hashlib.sha256(
        json.dumps(
            [RECOVERY_VERSION, row["token"], [e.model_dump(mode="json") for e in events]],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    derived = str(uuid5(NAMESPACE_URL, RECOVERY_VERSION + ":" + row["token"]))
    events.append(
        ExecutionEvent(
            event_id="recovered-completion",
            sequence=len(events),
            kind="completion",
            response={
                "source_run": row["token"],
                "recovery_version": RECOVERY_VERSION,
                "evidence_sha256": fingerprint,
                "verifier_event": evidence,
                "scope": "coding_segment",
                "verification": "Native tests passed after all recorded code changes",
            },
        )
    )
    if len(events) > 1024:
        raise RecoverySkipped("trace_size")
    return {
        "run_token": derived,
        "source_run": row["token"],
        "runtime_profile": profile,
        "visibility_scope": row["scope"],
        "events": [e.model_dump(mode="json") for e in events],
    }


def workspace_inspection(item, args):
    parsed = item.get("parsed_cmd")
    return inspection_only(args, "shell.execute") or (
        not args.get(WORKSPACE_CHANGES)
        and isinstance(parsed, list)
        and bool(parsed)
        and all(p.get("type") in {"read", "search", "list_files"} for p in parsed)
    )


def metadata_only(command):
    """Index/ref bookkeeping is not evidence of a working-file modification."""
    parts = re.split(r"&&|;|\n", command)
    return bool(parts) and all(
        len(words := command_parts(part)) > 1
        and words[0] == "git"
        and words[1] in {"add", "commit", "status", "diff", "log", "show", "push", "fetch"}
        for part in parts
        if part.strip()
    )
