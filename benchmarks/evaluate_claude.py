"""Claude Code sessions with and without MuscleMemory, on the shared repair fixtures.

Each session runs `claude -p` headless in a fresh fixture repository. The memory arm
has MuscleMemory's Claude Code hooks installed by `musclememory connect claude`; the
off arm has no hooks. MuscleMemory runs from an isolated home and port, so a local
installation is never touched. The independent verifier from codex_fixtures checks
every session.

    python benchmarks/evaluate_claude.py --dir RUN_DIR setup
    python benchmarks/evaluate_claude.py --dir RUN_DIR session LABEL --family configuration \
        --variant 1 --prompt 0 --arm musclememory
    python benchmarks/evaluate_claude.py --dir RUN_DIR approve
    python benchmarks/evaluate_claude.py --dir RUN_DIR state
    python benchmarks/evaluate_claude.py --dir RUN_DIR teardown
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_fixtures import PROMPTS, fixture_files, prepare, verify  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CLI = str(Path(sys.executable).with_name("musclememory"))
TOOLS = [
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "Bash(python3:*)",
    "Bash(python:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(grep:*)",
    "Bash(find:*)",
    "Bash(sed -n:*)",
    "Bash(wc:*)",
    "Bash(git diff:*)",
    "Bash(git status:*)",
]


def environment(run_dir):
    return {**os.environ, "MM_HOME": str(run_dir / "home")}


def target(run_dir, hooks_config=None, database=None):
    """Where sessions are recorded: the isolated home, or an existing server's hook config."""
    if hooks_config:
        config = json.loads(Path(hooks_config).read_text())
        return {
            "hooks_config": str(hooks_config),
            "state": Path(config["state_dir"]) / "hooks.sqlite",
            "key_file": Path(config["env_file"]),
            "url": config["url"],
            "database": Path(database),
        }
    home = run_dir / "home"
    url = None
    if (home / "config.json").exists():
        url = json.loads((home / "config.json").read_text())["url"]
    return {
        "hooks_config": str(home / "hooks" / "claude.json"),
        "state": home / "hooks" / "claude-state" / "hooks.sqlite",
        "key_file": home / "keys" / "claude.env",
        "url": url,
        "database": home / "memory.db",
    }


def mm(run_dir, *arguments, cwd=None):
    return subprocess.run(
        [CLI, *arguments],
        cwd=cwd,
        env=environment(run_dir),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def setup(run_dir, port):
    run_dir.mkdir(parents=True, exist_ok=True)
    print(mm(run_dir, "init", "--port", str(port)))
    print(mm(run_dir, "start"))


def teardown(run_dir):
    print(mm(run_dir, "stop"))


def run_claude(repo, prompt, model, transcript, timeout):
    command = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        *TOOLS,
        "--setting-sources",
        "project,local",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    started = time.monotonic()
    with transcript.open("w") as output:
        process = subprocess.run(
            command,
            cwd=repo,
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    elapsed = time.monotonic() - started
    tool_calls = parallel_messages = denied = 0
    result = {}
    for line in transcript.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            uses = [
                block
                for block in event.get("message", {}).get("content", [])
                if block.get("type") == "tool_use"
            ]
            tool_calls += len(uses)
            parallel_messages += len(uses) > 1
        elif event.get("type") == "user":
            for block in event.get("message", {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("is_error"):
                    if "permission" in json.dumps(block.get("content", "")).lower():
                        denied += 1
        elif event.get("type") == "result":
            result = event
    usage = result.get("usage", {})
    input_tokens = (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )
    return {
        "returncode": process.returncode,
        "stderr": process.stderr[-2000:],
        "wall_seconds": round(elapsed, 1),
        "duration_ms": result.get("duration_ms"),
        "num_turns": result.get("num_turns"),
        "is_error": result.get("is_error"),
        "cost_usd": result.get("total_cost_usd"),
        "input_tokens": input_tokens,
        "uncached_input_tokens": usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "tool_calls": tool_calls,
        "messages_with_parallel_tools": parallel_messages,
        "permission_denials": denied,
        "final_text": (result.get("result") or "")[-600:],
    }


def hook_state(where):
    database = where["state"]
    if not database.exists():
        return {"runs": [], "injections": []}
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    runs = [
        {key: row[key] for key in ("token", "session", "ended", "closed", "gaps", "created")}
        for row in connection.execute("SELECT * FROM runs ORDER BY created")
    ]
    injections = [dict(row) for row in connection.execute("SELECT * FROM injections")]
    connection.close()
    return {"runs": runs, "injections": injections}


def memory_state(where):
    database = where["database"]
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    procedures = []
    for row in connection.execute(
        "SELECT p.procedure_token, p.lifecycle, p.reviewed_at, p.current_version, "
        "r.successes, r.validation_status, r.definition FROM procedures p JOIN revisions r "
        "ON r.procedure_token = p.procedure_token AND r.version = p.current_version"
    ):
        definition = json.loads(row["definition"])
        procedures.append(
            {
                "procedure": row["procedure_token"],
                "title": definition.get("title", "")[:90],
                "lifecycle": row["lifecycle"],
                "approved": row["reviewed_at"] is not None,
                "successes": row["successes"],
                "validation": row["validation_status"],
            }
        )
    runs = [
        dict(row) for row in connection.execute("SELECT run_token, status FROM runs ORDER BY rowid")
    ]
    connection.close()
    return {"procedures": procedures, "runs": runs}


def session(run_dir, where, label, family, variant, prompt_index, arm, model, timeout):
    work = run_dir / "sessions" / label
    if work.exists():
        shutil.rmtree(work)
    repo = work / "repo"
    original = prepare(repo, family, variant)
    # Claude Code reads CLAUDE.md; give it the same project rules Codex read in AGENTS.md.
    (repo / "CLAUDE.md").write_text(fixture_files(family, variant)["AGENTS.md"])
    subprocess.run(["git", "-C", str(repo), "add", "CLAUDE.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Evaluation Harness",
            "-c",
            "user.email=evaluation@localhost",
            "commit",
            "-qm",
            "Claude rules",
        ],
        check=True,
    )
    original["CLAUDE.md"] = hashlib.sha256((repo / "CLAUDE.md").read_bytes()).hexdigest()
    if arm == "musclememory":
        if (run_dir / "home").exists():
            mm(run_dir, "connect", "claude", cwd=repo)
        else:
            mm(
                run_dir,
                "hooks",
                "install",
                "--client",
                "claude",
                "--config",
                where["hooks_config"],
                "--project",
                str(repo),
            )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Evaluation Harness",
                "-c",
                "user.email=evaluation@localhost",
                "commit",
                "-qam",
                "hooks",
                "--allow-empty",
            ],
            check=False,
            capture_output=True,
        )
    before = hook_state(where)
    prompt = PROMPTS[family][prompt_index]
    measured = run_claude(repo, prompt, model, work / "transcript.jsonl", timeout)
    time.sleep(2)  # let the Stop hook finish delivering
    if arm == "musclememory":
        subprocess.run(
            [
                CLI,
                "hooks",
                "flush",
                "--client",
                "claude",
                "--config",
                where["hooks_config"],
            ],
            env=environment(run_dir),
            capture_output=True,
        )
    after = hook_state(where)
    new_runs = [run for run in after["runs"] if run not in before["runs"]]
    new_injections = after["injections"][len(before["injections"]) :]
    checked = verify(repo, original, 500000 + variant)
    record = {
        "label": label,
        "family": family,
        "variant": variant,
        "prompt_index": prompt_index,
        "arm": arm,
        "model": model,
        **measured,
        "passed": checked["passed"],
        "behavioral_checks": checked["behavioral_checks"],
        "protected_files_unchanged": checked["protected_files_unchanged"],
        "hook_runs": new_runs,
        "injections": new_injections,
    }
    with (run_dir / "results.jsonl").open("a") as results:
        results.write(json.dumps(record) + "\n")
    (work / "record.json").write_text(json.dumps(record, indent=2))
    summary = {
        k: record[k]
        for k in (
            "label",
            "arm",
            "passed",
            "behavioral_checks",
            "input_tokens",
            "uncached_input_tokens",
            "output_tokens",
            "tool_calls",
            "messages_with_parallel_tools",
            "permission_denials",
            "wall_seconds",
            "cost_usd",
            "num_turns",
        )
    }
    summary["hook_runs"] = [
        {"closed": run["closed"], "gaps": json.loads(run["gaps"])} for run in new_runs
    ]
    summary["injected"] = len(new_injections)
    print(json.dumps(summary, indent=2))


def approve(where):
    key = where["key_file"].read_text().split("=", 1)[1].strip()
    url = where["url"]
    for procedure in memory_state(where)["procedures"]:
        if procedure["approved"]:
            continue
        request = urllib.request.Request(
            f"{url}/v1/procedures/{procedure['procedure']}/approve",
            method="POST",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            print(procedure["title"], response.status, response.read().decode())


INIT_KEYS = ("type", "subtype", "model", "permissionMode", "claude_code_version", "cwd")


def sanitize(text, run_dir):
    """Published files carry no local paths; `$RUN` and `$HOME` stand in for them."""
    text = text.replace(str(run_dir), "$RUN").replace(str(Path.home()), "$HOME")
    # Agents sometimes retype the run folder with wildcards; those spellings go too.
    pattern = r"/private/tmp/[^\s\"']*?/" + re.escape(run_dir.name) + r"(?=/|\b)"
    return re.sub(r"/private/tmp/claude-\d+", "$TMP", re.sub(pattern, "$RUN", text))


def export(run_dir, labels, out, protocol_file):
    """Copy scored sessions, without local paths or the local Claude setup, plus totals."""
    out.mkdir(parents=True, exist_ok=True)
    records = {}
    for line in (run_dir / "results.jsonl").read_text().splitlines():
        record = json.loads(line)
        records[record["label"]] = record  # the last run of a label wins
    chosen = [records[label] for label in labels]
    cases = out / "cases"
    cases.mkdir(exist_ok=True)
    for record in chosen:
        lines = []
        for line in (
            (run_dir / "sessions" / record["label"] / "transcript.jsonl").read_text().splitlines()
        ):
            event = json.loads(line)
            if event.get("type") == "system" and event.get("subtype") == "init":
                event = {key: event[key] for key in INIT_KEYS if key in event}
            lines.append(sanitize(json.dumps(event), run_dir))
        (cases / f"{record['label']}.jsonl").write_text("\n".join(lines) + "\n")
    totals = {}
    for arm in ("off", "musclememory"):
        rows = [record for record in chosen if record["arm"] == arm]
        totals[arm] = {
            "sessions": len(rows),
            "passed": sum(record["passed"] for record in rows),
            **{
                key: round(sum(record[key] or 0 for record in rows), 4)
                for key in (
                    "input_tokens",
                    "uncached_input_tokens",
                    "cache_read_input_tokens",
                    "output_tokens",
                    "tool_calls",
                    "wall_seconds",
                    "cost_usd",
                )
            },
            "skill_injected": sum(bool(record["injections"]) for record in rows),
        }
    result = {
        "protocol": json.loads(Path(protocol_file).read_text()),
        "totals": totals,
        "results": [json.loads(sanitize(json.dumps(record), run_dir)) for record in chosen],
    }
    (out / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    fingerprints = {
        str(path.relative_to(out)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(out.rglob("*"))
        if path.is_file() and path.name != "SHA256.json"
    }
    (out / "SHA256.json").write_text(json.dumps(fingerprints, indent=2) + "\n")
    print(json.dumps(totals, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--hooks-config", help="record into an existing server instead")
    parser.add_argument("--database", help="that server's SQLite file, for reporting")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("setup")
    start.add_argument("--port", type=int, default=8099)
    commands.add_parser("teardown")
    commands.add_parser("approve")
    commands.add_parser("state")
    publish = commands.add_parser("export")
    publish.add_argument("--labels", required=True, help="comma-separated session labels")
    publish.add_argument("--out", type=Path, required=True)
    publish.add_argument("--protocol", type=Path, required=True, help="JSON protocol notes")
    one = commands.add_parser("session")
    one.add_argument("label")
    one.add_argument("--family", choices=sorted(PROMPTS), required=True)
    one.add_argument("--variant", type=int, required=True)
    one.add_argument("--prompt", type=int, default=0)
    one.add_argument("--arm", choices=["off", "musclememory"], required=True)
    one.add_argument("--timeout", type=int, default=600)
    arguments = parser.parse_args()
    run_dir = arguments.dir.resolve()
    where = target(run_dir, arguments.hooks_config, arguments.database)
    if arguments.command == "setup":
        setup(run_dir, arguments.port)
    elif arguments.command == "teardown":
        teardown(run_dir)
    elif arguments.command == "approve":
        approve(where)
    elif arguments.command == "export":
        export(run_dir, arguments.labels.split(","), arguments.out.resolve(), arguments.protocol)
    elif arguments.command == "state":
        print(json.dumps({"memory": memory_state(where), "hooks": hook_state(where)}, indent=2))
    else:
        run_dir.mkdir(parents=True, exist_ok=True)
        session(
            run_dir,
            where,
            arguments.label,
            arguments.family,
            arguments.variant,
            arguments.prompt,
            arguments.arm,
            arguments.model,
            arguments.timeout,
        )


if __name__ == "__main__":
    main()
