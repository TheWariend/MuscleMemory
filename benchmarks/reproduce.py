"""Check MuscleMemory's published benchmark numbers yourself.

    python benchmarks/reproduce.py check     # offline: files untouched, numbers recomputed
    python benchmarks/reproduce.py rejudge   # offline: rerun SWE-bench's hidden tests on
                                             # every published Django patch (~15 minutes)
    python benchmarks/reproduce.py run       # rerun the Django benchmark with your own
                                             # Codex login (spends your Codex quota)

`check` needs only Python. `rejudge` and `run` set up a Django clone and a Python 3.11
environment under ~/.musclememory-bench (MM_BENCH_HOME) the first time. `run` needs the
Codex CLI signed in (`codex login`).
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks/results"
PUBLISHED = {
    "codex/luna-03": "Codex, three practice tasks",
    "claude/sonnet-01": "Claude Code with Sonnet 5.5",
    "codex/django-forms-01": "Codex on real Django issues (SWE-bench Verified)",
}


def change(off, on):
    return f"{(on - off) / off * 100:+.1f}%" if off else "n/a"


def fingerprints(folder):
    listed = json.loads((folder / "SHA256.json").read_text())
    bad = [
        name
        for name, digest in listed.items()
        if not (folder / name).exists()
        or hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest
    ]
    return len(listed), bad


def totals_luna(folder):
    data = json.loads((folder / "results.json").read_text())
    scored = [r for r in data["results"] if r["label"].startswith("scored")]
    computed = {}
    for arm in ("off", "musclememory"):
        side = [r for r in scored if r["arm"] == arm]
        computed[arm] = {
            "passed": sum(r["passed"] for r in side),
            "sessions": len(side),
            "input_tokens": sum(r["usage"]["input_tokens"] for r in side),
            "tool_calls": sum(r["tool_calls"] for r in side),
            "seconds": round(sum(r["duration_ms"] for r in side) / 1000, 1),
        }
    published = data["totals"]
    agrees = all(
        computed[arm]["input_tokens"] == published[arm]["input_tokens"]
        and computed[arm]["tool_calls"] == published[arm]["tool_calls"]
        and computed[arm]["passed"] == published[arm]["passed"]
        for arm in computed
    )
    return computed, agrees


def totals_sessions(rows, passed_key, seconds_key):
    computed = {}
    for arm in ("off", "musclememory"):
        side = [r for r in rows if r["arm"] == arm]
        computed[arm] = {
            "passed": sum(bool(r[passed_key]) for r in side),
            "sessions": len(side),
            "input_tokens": sum(r["input_tokens"] or 0 for r in side),
            "tool_calls": sum(r["tool_calls"] or 0 for r in side),
            "seconds": round(sum(r[seconds_key] for r in side), 1),
        }
    return computed


def totals_sonnet(folder):
    data = json.loads((folder / "results.json").read_text())
    computed = totals_sessions(data["results"], "passed", "wall_seconds")
    published = data["totals"]
    agrees = all(
        computed[arm]["input_tokens"] == published[arm]["input_tokens"]
        and computed[arm]["tool_calls"] == published[arm]["tool_calls"]
        for arm in computed
    )
    return computed, agrees


def django_records(folder):
    return [
        json.loads(path.read_text()) for path in sorted((folder / "sessions").glob("*/record.json"))
    ]


def totals_django(folder):
    scored = [r for r in django_records(folder) if r["label"].startswith("score-")]
    computed = totals_sessions(scored, "resolved", "seconds")
    published = json.loads((folder / "summary.json").read_text())["totals"]
    agrees = all(
        computed[arm]["input_tokens"] == published[arm]["input_tokens"]
        and computed[arm]["tool_calls"] == published[arm]["tool_calls"]
        and computed[arm]["passed"] == published[arm]["resolved"]
        for arm in computed
    )
    return computed, agrees


def check():
    ok = True
    computers = {
        "codex/luna-03": totals_luna,
        "claude/sonnet-01": totals_sonnet,
        "codex/django-forms-01": totals_django,
    }
    for name, title in PUBLISHED.items():
        folder = RESULTS / name
        count, bad = fingerprints(folder)
        computed, agrees = computers[name](folder)
        off, on = computed["off"], computed["musclememory"]
        print(f"\n{title}  ({name})")
        print(f"  files: {count - len(bad)} of {count} match their published SHA-256")
        for file in bad:
            print(f"    changed or missing: {file}")
        print(
            f"  recomputed from the raw session records ({'matches' if agrees else 'DIFFERS FROM'}"
            " the published totals):"
        )
        print(
            f"    passed       {off['passed']}/{off['sessions']} off   "
            f"{on['passed']}/{on['sessions']} with memory"
        )
        print(
            f"    input tokens {off['input_tokens']:>10,} → {on['input_tokens']:>10,}  "
            f"{change(off['input_tokens'], on['input_tokens'])}"
        )
        print(
            f"    tool calls   {off['tool_calls']:>10} → {on['tool_calls']:>10}  "
            f"{change(off['tool_calls'], on['tool_calls'])}"
        )
        print(
            f"    time         {off['seconds']:>9}s → {on['seconds']:>9}s  "
            f"{change(off['seconds'], on['seconds'])}"
        )
        ok &= not bad and agrees
    print(
        "\nAll published files and totals check out." if ok else "\nSomething differs; see above."
    )
    return 0 if ok else 1


def word(resolved):
    return "resolved" if resolved else "not resolved"


def rejudge():
    sys.path.insert(0, str(ROOT / "benchmarks"))
    import swebench_django as bench

    bench.prepare_environment()
    folder = RESULTS / "codex/django-forms-01"
    records = django_records(folder)
    tasks = {
        t["instance_id"]: t
        for t in bench.fetch_tasks(
            sorted({r["instance_id"] for r in records}), bench.HOME / "tasks-verified.json"
        )
    }
    scratch = bench.HOME / "rejudge"
    differences = 0
    for record in records:
        task = tasks[record["instance_id"]]
        repo = bench.prepare(task, scratch / record["label"])
        patch = (folder / "sessions" / record["label"] / "patch.diff").read_text()
        applied = True
        if patch.strip():
            applied, error = bench.apply(repo, patch)
        verdict = bench.judge(repo, task) if applied else {"resolved": False}
        same = verdict["resolved"] == record["resolved"]
        differences += not same
        print(
            f"{record['label']:<32} published {word(record['resolved']):<13}"
            f" rejudged {word(verdict['resolved']):<13}"
            f"{'' if same else '  <-- differs'}",
            flush=True,
        )
        shutil.rmtree(scratch / record["label"], ignore_errors=True)
        shutil.rmtree(Path(str(scratch / record["label"]) + "-base"), ignore_errors=True)
    print(
        "\nEvery published verdict reproduces."
        if not differences
        else f"\n{differences} verdict(s) differ."
    )
    return 0 if not differences else 1


def run(model, repeats, assume_yes):
    sys.path.insert(0, str(ROOT / "benchmarks"))
    import swebench_django as bench

    if not shutil.which("codex"):
        raise SystemExit("Install the Codex CLI and run `codex login` first.")
    sessions = 4 + 2 * repeats
    print(
        f"This runs about {sessions} Codex sessions with {model} on your account "
        "(roughly 2-4 million input tokens) and takes 20-40 minutes."
    )
    if not assume_yes and input("Continue? [y/N] ").strip().lower() != "y":
        return 1
    bench.prepare_environment()
    python = sys.executable
    out = bench.HOME / "runs"
    steps = [
        [
            python,
            str(ROOT / "benchmarks/swebench_django.py"),
            "validate",
            "--group",
            "model-forms",
            "--out",
            str(out / "validation-1"),
        ],
        [
            python,
            str(ROOT / "benchmarks/evaluate_django.py"),
            "--dir",
            str(out / "repro"),
            "setup",
            "--group",
            "model-forms",
            "--model",
            model,
            "--train",
            "4",
        ],
    ]
    for command in ("train", "approve"):
        steps.append(
            [
                python,
                str(ROOT / "benchmarks/evaluate_django.py"),
                "--dir",
                str(out / "repro"),
                command,
            ]
        )
    steps.append(
        [
            python,
            str(ROOT / "benchmarks/evaluate_django.py"),
            "--dir",
            str(out / "repro"),
            "score",
            "--repeats",
            str(repeats),
        ]
    )
    steps.append(
        [python, str(ROOT / "benchmarks/evaluate_django.py"), "--dir", str(out / "repro"), "report"]
    )
    for step in steps:
        print("\n$", " ".join(step[1:]), flush=True)
        subprocess.run(step, check=True)
    print(
        f"\nYour run is in {out / 'repro'}; compare summary.json with "
        "benchmarks/results/codex/django-forms-01/summary.json."
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Offline: verify files and recompute totals")
    sub.add_parser("rejudge", help="Offline: rerun the hidden tests on published patches")
    live = sub.add_parser("run", help="Rerun the Django benchmark with your Codex")
    live.add_argument("--model", default="gpt-6-luna")
    live.add_argument("--repeats", type=int, default=3)
    live.add_argument("--yes", action="store_true", help="Don't ask before spending quota")
    arguments = parser.parse_args()
    if arguments.command == "check":
        return check()
    if arguments.command == "rejudge":
        return rejudge()
    return run(arguments.model, arguments.repeats, arguments.yes)


if __name__ == "__main__":
    sys.exit(main())
