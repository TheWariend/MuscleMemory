"""Capture real development tools through the installed MuscleMemory Python SDK.

This controlled demonstration uses distinct recorded runs by the same developer.
It is not a blind multi-agent benchmark and does not use native Codex hooks.
No code here fabricates observations, sets success counters, or approves skills.
"""

import argparse
import json
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from musclememory.client_hooks import load_config
from musclememory.observer import ObservedRun

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".demo"
TEST_COMMAND = "python3 -m unittest discover -s tests -v"
PROFILE = {
    "agent_name": "parceldesk-codex-sdk-demo",
    "bindings": {
        "repository.inspect": "read_file",
        "file.patch": "apply_patch",
        "shell.run": "exec_command",
    },
    "environment": {"project": "parceldesk-demo", "language": "python"},
    "permissions": ["workspace.read", "workspace.write"],
}
INTENTS = {
    "received": (
        "Make warehouse event handlers safe to retry: "
        "protect supplier receipts from duplicate stock updates"
    ),
    "dispatched": (
        "Make warehouse event handlers safe to retry: "
        "protect courier dispatches from duplicate stock deductions"
    ),
    "returned": (
        "Make warehouse event handlers safe to retry: "
        "protect customer returns from duplicate stock additions"
    ),
}


def client():
    config, _, _, connection = load_config(Path.home() / ".musclememory/hooks/codex.json", "codex")
    return connection, config.get("namespace", "default")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def read_state():
    path = STATE / "progress.json"
    return json.loads(path.read_text()) if path.exists() else {"runs": []}


def refresh(connection, token):
    detail = connection.musclememory_inspect(procedure_token=token)
    state = read_state()
    state.update(
        procedure_token=token,
        procedure_version=detail["procedure_version"],
        lifecycle_status=detail["lifecycle_status"],
        successes=detail["successes"],
        failures=detail["failures"],
        needs_review=detail["needs_review"],
        unproven_lines=len(detail.get("unproven_lines", [])),
        note=(
            "Recorded edits and real tests passed. Waiting for your Keep; recall remains blocked."
            if detail["needs_review"]
            else "Kept by you. Recall still requires two passing checks and the proof filter."
        ),
    )
    write(STATE / "progress.json", state)
    write(STATE / "skill-inspection.json", detail)
    return state


def capture(kind, connection, scope):
    handler = {"received": "receive", "dispatched": "dispatch", "returned": "returned"}[kind]
    source = ROOT / "parceldesk/events.py"
    text = source.read_text()
    segment = text.split(f"def {handler}(connection, event):\n", 1)[1].split("\n\ndef ", 1)[0]
    if "if not claim_event" in segment:
        raise SystemExit("This task is already implemented; it cannot create another check")
    executable = shutil.which("apply_patch")
    if not executable:
        raise SystemExit("Run capture from an agent terminal providing the apply_patch tool")
    patch = (
        f"*** Begin Patch\n*** Update File: {source}\n@@\n"
        f" def {handler}(connection, event):\n"
        "     with connection:\n"
        "+        if not claim_event(connection, event):\n"
        '+            return {"status": "duplicate", "event_id": event["id"]}\n'
        "*** End Patch\n"
    )
    run = ObservedRun(
        connection,
        INTENTS[kind],
        PROFILE,
        scope,
        run_token=f"parceldesk-{kind}-{uuid4().hex[:12]}",
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
        read_file("parceldesk/events.py")
        try:
            exec_command(TEST_COMMAND, str(ROOT))
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Task tests already pass; refusing a meaningless validation run")
        apply_patch(patch)
        exec_command(TEST_COMMAND, str(ROOT))
        final_test = run.events[-1]["event_id"]
        run.complete()
        report = {
            "run_token": run.run_token,
            "task": kind,
            "intent": INTENTS[kind],
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "completion_proof": {
                "succeeded": True,
                "evidence_event_ids": [final_test],
                "summary": (
                    f"Real {kind} retry regressions failed before the edit and passed after it"
                ),
            },
            "performance_metrics": {"observed": run.measurements()},
            "events": run.events,
        }
        write(STATE / f"{kind}-run.json", report)
        state = read_state()
        state["runs"].append(
            {"kind": kind, "run_token": run.run_token, "test": TEST_COMMAND, "test_passed": True}
        )
        write(STATE / "progress.json", state)
        return {
            key: report[key] for key in ("run_token", "completion_proof", "performance_metrics")
        }
    except BaseException:
        if not run.closed:
            run.complete(succeeded=False)
        raise
    finally:
        run.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "check", "status"])
    parser.add_argument("kind", nargs="?", choices=INTENTS)
    parser.add_argument("--procedure")
    args = parser.parse_args()
    connection, scope = client()
    try:
        if args.action == "capture":
            if not args.kind:
                parser.error("capture needs an event kind")
            result = capture(args.kind, connection, scope)
        elif args.action == "check":
            if not (args.kind and args.procedure):
                parser.error("check needs an event kind and --procedure")
            record = json.loads((STATE / f"{args.kind}-run.json").read_text())
            result = connection.auto_learn(record["run_token"], hint=args.procedure)
            write(STATE / f"{args.kind}-check.json", result)
            refresh(connection, args.procedure)
        else:
            token = args.procedure or read_state().get("procedure_token")
            if not token:
                parser.error("status needs --procedure until a skill has been learned")
            result = refresh(connection, token)
        print(json.dumps(result, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
