"""Codex on real Django issues (SWE-bench Verified), with and without MuscleMemory.

A small, credible run: one group of real issues that touch the same part of Django.

1. train  - Codex solves the group's oldest issues with MuscleMemory connected, exactly
            as a user would set it up (`musclememory connect codex`). Passing sessions
            are learned, or count as checks on an earlier skill.
2. approve - press Keep on the learned skills (what a person does in the dashboard).
3. score  - the newer issues, each run with memory and without, alternating order.
            Learning is switched off first, so scoring can't teach itself.

Every session is judged by SWE-bench's hidden tests (see swebench_django.py). Codex
runs from a fresh CODEX_HOME, so it has no memories or skills of its own.

    python benchmarks/evaluate_django.py --dir RUN setup --group migration-operations
    python benchmarks/evaluate_django.py --dir RUN train
    python benchmarks/evaluate_django.py --dir RUN approve
    python benchmarks/evaluate_django.py --dir RUN score --repeats 2
    python benchmarks/evaluate_django.py --dir RUN report
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages"))
from musclememory.receipts import codex_tool_counts, codex_usage  # noqa: E402
from swebench_django import GROUPS, HOME, fetch_tasks, judge, prepare  # noqa: E402

CLI = str(Path(sys.executable).with_name("musclememory"))
# The issue first, as a person would paste it; then one plain instruction.
PROMPT = (
    "{issue}\n\nFix this issue in the Django repository. Run the relevant tests with "
    "`python tests/runtests.py <module>` to check your change before you finish."
)


def campaign(run):
    return json.loads((run / "campaign.json").read_text())


def mm(run, *arguments, cwd=None):
    return subprocess.run(
        [CLI, *arguments],
        cwd=cwd,
        env={**os.environ, "MM_HOME": str(run / "home")},
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def setup(run, group, model, port, train_count):
    run.mkdir(parents=True, exist_ok=True)
    ids = GROUPS[group]
    validation = HOME / "runs/validation-1/validation.jsonl"
    usable = {}
    for line in validation.read_text().splitlines():
        result = json.loads(line)
        usable[result["instance_id"]] = result
    ids = [i for i in ids if usable.get(i, {}).get("usable")]
    if len(ids) < train_count + 1:
        raise SystemExit(f"Only {len(ids)} usable issues in {group}")
    tasks = fetch_tasks(ids, HOME / "runs/validation-1/tasks.json")
    tasks.sort(key=lambda task: task["created_at"])
    (run / "campaign.json").write_text(
        json.dumps(
            {
                "group": group,
                "model": model,
                "train": [t["instance_id"] for t in tasks[:train_count]],
                "score": [t["instance_id"] for t in tasks[train_count:]],
                "usable_pass_to_pass": {i: usable[i].get("usable_pass_to_pass") for i in ids},
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
        )
    )
    print(mm(run, "init", "--port", str(port)))
    print(mm(run, "start"))
    home = run / "codex-home"
    home.mkdir(mode=0o700, exist_ok=True)
    auth = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    shutil.copyfile(auth, home / "auth.json")
    (home / "auth.json").chmod(0o600)
    print(json.dumps(campaign(run), indent=2))


def task_by_id(instance_id):
    return fetch_tasks([instance_id], HOME / "runs/validation-1/tasks.json")[0]


def hooks_config(run):
    return run / "home/hooks/codex.json"


def set_learning(run, enabled):
    path = hooks_config(run)
    config = json.loads(path.read_text())
    config["auto_learn"] = enabled
    path.write_text(json.dumps(config, indent=2) + "\n")


def session(run, label, instance_id, arm, timeout, learn):
    info = campaign(run)
    task = task_by_id(instance_id)
    work = run / "sessions" / label
    if (work / "record.json").exists():
        return json.loads((work / "record.json").read_text())
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    repo = prepare(task, work / "repo")
    if arm == "musclememory":
        mm(run, "connect", "codex", cwd=repo)
        set_learning(run, learn)  # connect rewrites the hooks config
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=bench",
                "-c",
                "user.email=b@localhost",
                "commit",
                "-qm",
                "MuscleMemory hooks",
            ],
            check=True,
            capture_output=True,
        )
    home = run / "codex-home"
    (home / "config.toml").write_text(
        f'[projects.{json.dumps(str(repo))}]\ntrust_level="trusted"\n'
    )
    environment = {k: v for k, v in os.environ.items() if not k.startswith("MM_")}
    environment.update(
        {
            "CODEX_HOME": str(home),
            "PATH": f"{HOME / 'py311/bin'}:{environment.get('PATH', '/usr/bin:/bin')}",
            "PYTHONPATH": str(repo),
            "PYTHONDONTWRITEBYTECODE": "1",
            "MM_HOME": str(run / "home"),
        }
    )
    argv = [
        os.getenv("MM_CODEX_BIN", shutil.which("codex")),
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "--dangerously-bypass-hook-trust",
        "--json",
        "--model",
        info["model"],
        "-c",
        'model_reasoning_effort="medium"',
        "-C",
        str(repo),
        PROMPT.format(issue=task["problem_statement"].strip()),
    ]
    (work / "prompt.txt").write_text(argv[-1])
    before = hook_state(run)
    started = time.monotonic()
    with (work / "codex.jsonl").open("w") as out, (work / "codex.stderr").open("w") as err:
        process = subprocess.Popen(
            argv, cwd=repo, env=environment, stdout=out, stderr=err, start_new_session=True
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, 15)
            process.wait(timeout=30)
            returncode = -124
    seconds = time.monotonic() - started
    if arm == "musclememory":
        subprocess.run(
            [CLI, "hooks", "flush", "--client", "codex", "--config", str(hooks_config(run))],
            env=environment,
            capture_output=True,
        )
    after = hook_state(run)
    messages = [
        json.loads(line)
        for line in (work / "codex.jsonl").read_text().splitlines()
        if line.startswith("{")
    ]
    usage = codex_usage(messages)
    tools = codex_tool_counts(messages)
    verdict = judge(repo, task, info["usable_pass_to_pass"].get(instance_id))
    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", "HEAD", "--", ".", ":!.codex"],
        capture_output=True,
        text=True,
    ).stdout
    (work / "patch.diff").write_text(diff)
    record = {
        "label": label,
        "instance_id": instance_id,
        "arm": arm,
        "model": info["model"],
        "returncode": returncode,
        "seconds": round(seconds, 1),
        "input_tokens": usage["input_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "uncached_input_tokens": None
        if usage["input_tokens"] is None
        else usage["input_tokens"] - (usage["cached_input_tokens"] or 0),
        "output_tokens": usage["output_tokens"],
        "tool_calls": tools.get("tool_calls") if isinstance(tools, dict) else None,
        "failed_tool_calls": tools.get("failed_tool_calls") if isinstance(tools, dict) else None,
        "resolved": verdict["resolved"],
        "fail_to_pass": verdict.get("fail_to_pass"),
        "pass_to_pass": verdict.get("pass_to_pass"),
        "hook_runs": [r for r in after["runs"] if r not in before["runs"]],
        "injected": after["injections"][len(before["injections"]) :],
    }
    (work / "record.json").write_text(json.dumps(record, indent=2))
    with (run / "results.jsonl").open("a") as results:
        results.write(json.dumps(record) + "\n")
    shutil.rmtree(Path(str(repo) + "-base"), ignore_errors=True)
    print(
        json.dumps(
            {
                k: record[k]
                for k in (
                    "label",
                    "arm",
                    "resolved",
                    "input_tokens",
                    "tool_calls",
                    "seconds",
                    "injected",
                )
            },
            default=str,
        ),
        flush=True,
    )
    return record


def hook_state(run):
    database = run / "home/hooks/codex-state/hooks.sqlite"
    if not database.exists():
        return {"runs": [], "injections": []}
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    runs = [
        {"token": r["token"], "closed": r["closed"], "gaps": json.loads(r["gaps"])}
        for r in connection.execute("SELECT * FROM runs ORDER BY created")
    ]
    injections = [dict(r) for r in connection.execute("SELECT * FROM injections")]
    connection.close()
    return {"runs": runs, "injections": injections}


def skills(run):
    connection = sqlite3.connect(f"file:{run / 'home/memory.db'}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT p.procedure_token, p.lifecycle, p.reviewed_at IS NOT NULL, r.successes, "
        "r.validation_status, json_extract(r.definition, '$.title') FROM procedures p JOIN "
        "revisions r ON r.procedure_token = p.procedure_token AND r.version = p.current_version"
    ).fetchall()
    connection.close()
    return [
        {
            "procedure": r[0],
            "lifecycle": r[1],
            "approved": bool(r[2]),
            "successes": r[3],
            "validation": r[4],
            "title": (r[5] or "")[:80],
        }
        for r in rows
    ]


def approve(run):
    config = json.loads((run / "home/config.json").read_text())
    key = (run / "home/keys/codex.env").read_text().split("=", 1)[1].strip()
    for skill in skills(run):
        if skill["approved"]:
            continue
        request = urllib.request.Request(
            f"{config['url']}/v1/procedures/{skill['procedure']}/approve",
            method="POST",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            print("kept", skill["title"], response.status)
    print(json.dumps(skills(run), indent=2))


def report(run):
    rows = [json.loads(line) for line in (run / "results.jsonl").read_text().splitlines()]
    latest = {row["label"]: row for row in rows}
    train = [row for label, row in latest.items() if label.startswith("train-")]
    scored = [row for label, row in latest.items() if label.startswith("score-")]
    totals = {}
    for arm in ("off", "musclememory"):
        side = [row for row in scored if row["arm"] == arm]
        totals[arm] = {
            "sessions": len(side),
            "resolved": sum(row["resolved"] for row in side),
            **{
                key: sum(row[key] or 0 for row in side)
                for key in ("input_tokens", "uncached_input_tokens", "output_tokens", "tool_calls")
            },
            "seconds": round(sum(row["seconds"] for row in side), 1),
            "skill_handed_over": sum(bool(row["injected"]) for row in side),
        }
    pairs = []
    for row in sorted(scored, key=lambda r: r["label"]):
        if row["arm"] != "off":
            continue
        twin = latest.get(row["label"].replace("-off", "-musclememory"))
        if twin:
            pairs.append(
                {
                    "pair": row["label"].removesuffix("-off"),
                    "input_tokens": [row["input_tokens"], twin["input_tokens"]],
                    "tool_calls": [row["tool_calls"], twin["tool_calls"]],
                    "seconds": [row["seconds"], twin["seconds"]],
                    "resolved": [row["resolved"], twin["resolved"]],
                }
            )
    summary = {
        "campaign": campaign(run),
        "training": [
            {
                k: r[k]
                for k in (
                    "label",
                    "instance_id",
                    "resolved",
                    "input_tokens",
                    "tool_calls",
                    "seconds",
                )
            }
            | {"learned_or_checked": any(h["closed"] for h in r["hook_runs"])}
            for r in sorted(train, key=lambda r: r["label"])
        ],
        "skills": skills(run),
        "totals": totals,
        "pairs": pairs,
    }
    (run / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({"totals": totals, "pairs": pairs, "skills": summary["skills"]}, indent=2))


def export(run, out):
    """Copy the campaign without local paths: summary, and per session the prompt,
    Codex's event stream, the patch and the record."""
    import hashlib
    import re

    def clean(text):
        text = text.replace(str(run), "$RUN").replace(str(HOME), "$BENCH")
        return re.sub(r"/Users/[^/\s\"']+", "$HOME", text)

    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(clean((run / "summary.json").read_text()))
    for session_dir in sorted((run / "sessions").iterdir()):
        target = out / "sessions" / session_dir.name
        target.mkdir(parents=True, exist_ok=True)
        for name in ("prompt.txt", "codex.jsonl", "patch.diff", "record.json"):
            if (session_dir / name).exists():
                (target / name).write_text(clean((session_dir / name).read_text()))
    fingerprints = {
        str(path.relative_to(out)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(out.rglob("*"))
        if path.is_file() and path.name != "SHA256.json"
    }
    (out / "SHA256.json").write_text(json.dumps(fingerprints, indent=2) + "\n")
    print(f"exported {len(fingerprints)} files to {out}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("setup")
    start.add_argument("--group", choices=sorted(GROUPS), required=True)
    start.add_argument("--model", default="gpt-6-luna")
    start.add_argument("--port", type=int, default=8097)
    start.add_argument("--train", type=int, default=3)
    sub.add_parser("train")
    sub.add_parser("approve")
    score = sub.add_parser("score")
    score.add_argument("--repeats", type=int, default=2)
    sub.add_parser("state")
    sub.add_parser("report")
    exporter = sub.add_parser("export")
    exporter.add_argument("--out", type=Path, required=True)
    for command in ("train", "score"):
        sub.choices[command].add_argument("--timeout", type=int, default=1200)
    arguments = parser.parse_args()
    run = arguments.dir.resolve()
    if arguments.command == "setup":
        setup(run, arguments.group, arguments.model, arguments.port, arguments.train)
    elif arguments.command == "train":
        for index, instance_id in enumerate(campaign(run)["train"]):
            session(run, f"train-{index}", instance_id, "musclememory", arguments.timeout, True)
        print(json.dumps(skills(run), indent=2))
    elif arguments.command == "approve":
        approve(run)
    elif arguments.command == "score":
        order = 0  # memory is frozen while scoring: learning is off in every session
        for repeat in range(arguments.repeats):
            for instance_id in campaign(run)["score"]:
                arms = ["off", "musclememory"] if order % 2 == 0 else ["musclememory", "off"]
                order += 1
                for arm in arms:
                    session(
                        run,
                        f"score-{instance_id.split('-')[-1]}-r{repeat}-{arm}",
                        instance_id,
                        arm,
                        arguments.timeout,
                        False,
                    )
    elif arguments.command == "report":
        report(run)
    elif arguments.command == "export":
        export(run, arguments.out.resolve())
    else:
        print(json.dumps({"skills": skills(run), "hooks": hook_state(run)}, indent=2))


if __name__ == "__main__":
    main()
