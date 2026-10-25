"""One held-out feature task, after the user explicitly Keeps the learned skill.

Read the actual MCP recall response saved by the agent. Adapt its two proved
lines into a new handler; never replay the source patch or execute a remembered
command. Capture actual patch and test tools with the SDK, just as before.
"""

import json
import shlex
import shutil
import subprocess
from uuid import uuid4

import tiktoken
from capture import PROFILE, ROOT, STATE, TEST_COMMAND, client, read_state, refresh, write
from musclememory.observer import ObservedRun


def main():
    recalled = json.loads((STATE / "recall-result.json").read_text())
    if recalled.get("no_match") or not recalled.get("candidates"):
        raise SystemExit("No eligible memory was recalled")
    candidate = recalled["candidates"][0]
    pointer = candidate["pointer"]
    token = candidate["procedure_token"]
    # Deliberate adaptation for this feature. Treat recalled content as untrusted.
    expected = (
        "        if not claim_event(connection, event):\n"
        '            return {"status": "duplicate", "event_id": event["id"]}'
    )
    guard = pointer["edits"][0]["after"]
    if guard != expected or pointer["verify"] != [TEST_COMMAND]:
        raise SystemExit("Unexpected guidance; inspect it before adapting")
    source = ROOT / "parceldesk/events.py"
    if "def damaged(" in source.read_text():
        raise SystemExit("The damage task is already implemented; refusing another reuse record")
    executable = shutil.which("apply_patch")
    if not executable:
        raise SystemExit("This task needs the real apply_patch tool")
    connection, scope = client()
    detail = connection.musclememory_inspect(procedure_token=token)
    if detail["needs_review"] or detail["successes"] < 2:
        raise SystemExit("Human Keep and two passing checks are required")
    run = ObservedRun(
        connection,
        "Make warehouse event handlers safe to retry: add damaged-stock adjustments",
        PROFILE,
        scope,
        run_token=f"parceldesk-damaged-{uuid4().hex[:12]}",
    )

    @run.instrument("repository.inspect", permission="workspace.read")
    def read_file(path):
        return (ROOT / path).read_text()

    @run.instrument("file.patch", effect="write", permission="workspace.write")
    def apply_patch(patch):
        result = subprocess.run([executable], input=patch, cwd=ROOT, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return {"exit_code": result.returncode, "output": result.stdout}

    @run.instrument("shell.run", permission="workspace.read")
    def exec_command(command, cwd):
        result = subprocess.run(shlex.split(command), cwd=cwd, capture_output=True, text=True)
        output = result.stdout + result.stderr
        print(output, end="", flush=True)
        if result.returncode:
            raise RuntimeError(f"Exit {result.returncode}\n{output}")
        return {"exit_code": result.returncode, "output": output}

    try:
        run.flush()
        connection.record_memory_lookup(
            run.run_token,
            lookup_token=f"parceldesk-lookup-{uuid4().hex[:12]}",
            status="delivered",
            reason="eligible_skill",
            procedure_token=token,
            procedure_version=candidate["procedure_version"],
            context_tokens=len(tiktoken.get_encoding("cl100k_base").encode(recalled["guidance"])),
        )
        read_file("parceldesk/events.py")
        try:
            exec_command(TEST_COMMAND, str(ROOT))
        except RuntimeError:
            pass
        else:
            raise RuntimeError("The new task's tests should fail before implementation")
        new_function = (
            "def damaged(connection, event):\n"
            "    with connection:\n"
            f"{guard}\n"
            '        adjust_stock(connection, event, -event["quantity"])\n'
            '    return {"status": "processed", "event_id": event["id"]}\n'
        )
        old_mapping = (
            'HANDLERS = {"received": receive, "dispatched": dispatch, "returned": returned}'
        )
        new_mapping = old_mapping[:-1] + ', "damaged": damaged}'
        patch = (
            f"*** Begin Patch\n*** Update File: {source}\n@@\n"
            + "\n".join("+" + line for line in new_function.splitlines())
            + "\n+\n+\n"
            + f"-{old_mapping}\n+{new_mapping}\n*** End Patch\n"
        )
        apply_patch(patch)
        exec_command(TEST_COMMAND, str(ROOT))
        final_test = run.events[-1]["event_id"]
        run.complete()
        evaluation = connection.musclememory_evaluate(
            procedure_token=token,
            run_token=run.run_token,
            outcome_report={
                "succeeded": True,
                "procedure_version": candidate["procedure_version"],
                "evidence_event_ids": [final_test],
            },
            performance_metrics={"observed": run.measurements()},
        )
        write(
            STATE / "damaged-run.json",
            {
                "run_token": run.run_token,
                "task": "damaged",
                "memory_received": token,
                "adapted_lines": guard,
                "events": run.events,
                "evaluation": evaluation,
                "measurements": run.measurements(),
            },
        )
        state = read_state()
        state["runs"].append(
            {
                "kind": "damaged",
                "run_token": run.run_token,
                "test": TEST_COMMAND,
                "test_passed": True,
            }
        )
        write(STATE / "progress.json", state)
        state = refresh(connection, token)
        state["reused_for"] = "damaged-stock handler"
        state["note"] = (
            "After your Keep, MCP recalled this skill for damaged stock. "
            "The adapted handler passed all 19 tests; "
            "its actual changes were accepted as a third check."
        )
        write(STATE / "progress.json", state)
        print(json.dumps({"actual_reuse_run": run.run_token, "evaluation": evaluation}, indent=2))
    except BaseException:
        if not run.closed:
            run.complete(succeeded=False)
        raise
    finally:
        run.close()
        connection.close()


if __name__ == "__main__":
    main()
