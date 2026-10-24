"""Compare the committed local baseline with this checkout, without LLM calls.

Uses the same synthetic scale fixtures in both arms, separate temporary SQLite
stores, the cached local MiniLM model, and alternating arm order. Trust metadata
on the synthetic bank is fixture setup, not evidence of real-world skill quality.
This measures service overhead and retrieval decisions, not agent task savings.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import random
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def worker(root, size, queries, destination):
    # The parent selects the package path before Python imports either arm.
    from musclememory.cli import migrate
    from musclememory.config import DEFAULT_EMBEDDING_DIM, DEFAULT_EMBEDDING_MODEL, Settings
    from musclememory.core import RecallInput
    from musclememory.delivery import render_pointer, token_count
    from musclememory.service import MemoryService
    from musclememory.storage import Procedure, utcnow
    from sqlalchemy import update

    spec = importlib.util.spec_from_file_location("scale_fixture", root / "benchmarks/scale.py")
    scale = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scale)
    with tempfile.TemporaryDirectory(prefix="mm-compare-store-") as directory:
        database = Path(directory) / "memory.sqlite"
        url = "sqlite:///" + str(database)
        migrate(url)
        service = MemoryService(
            Settings(
                database_url=url,
                embedding_provider="fastembed",
                embedding_model=DEFAULT_EMBEDDING_MODEL,
                embedding_dim=DEFAULT_EMBEDDING_DIM,
                preferences_path=None,
                requests_per_minute=1_000_000,
            )
        )
        actor = service.authenticate(
            service.issue_key("comparison", {"bench": ["read", "write", "delete", "admin"]})[
                "api_key"
            ],
            False,
        )
        service.embeddings.embed("Warm the cached local model")
        learning_ms = []
        for index in range(5):
            intent = f"Fix configuration override precedence in calibration-{index}.py"
            events = scale.trace_events(intent)
            # Distinct files prevent later calibration episodes becoming reuse checks.
            events = json.loads(json.dumps(events).replace("zzmodule", f"calibration{index}"))
            run = f"calibration-{index}"
            service.observe(
                actor,
                scale.ObserveInput(
                    run_token=run,
                    event_batch=events,
                    runtime_profile=scale.PROFILE,
                    visibility_scope="bench",
                ),
            )
            begin = time.perf_counter()
            learned = service.auto_learn(actor, run)
            learning_ms.append(1000 * (time.perf_counter() - begin))
            assert learned["action"] == "learned", learned
            service.delete_memory(actor, learned["procedure_token"])
        template = scale.learn_template(service, actor, "bench")
        items = scale.combos()
        stored = items[:size]
        scale.build_store(service, template, stored, actor.tenant, "bench")
        # Explicit simulated approvals, identical in both synthetic retrieval arms.
        with service.db.transaction() as session:
            session.execute(update(Procedure).values(reviewed_at=utcnow()))
            session.commit()
        targets = random.Random(42).sample(stored, queries)
        cases = (
            [("paraphrase", item, True) for item in targets]
            + [("with_path", item, True) for item in targets[: queries // 2]]
            + [("not_stored", item, False) for item in items[-queries // 2 :]]
            + [("unrelated", text, False) for text in scale.UNRELATED]
        )
        rows = []
        for index, (kind, item, expected) in enumerate(cases):
            query = (
                item
                if kind == "unrelated"
                else scale.request_text(*item, with_path=kind == "with_path")
            )
            if index == 0:
                service.recall(
                    actor,
                    RecallInput(
                        intent_text=query, runtime_profile=scale.PROFILE, visibility_scope="bench"
                    ),
                )
            begin = time.perf_counter()
            result = service.recall(
                actor,
                RecallInput(
                    intent_text=query, runtime_profile=scale.PROFILE, visibility_scope="bench"
                ),
            )
            latency = 1000 * (time.perf_counter() - begin)
            top = result["candidates"][0]["definition"]["title"] if result["candidates"] else None
            # Native hooks deliberately deliver one checked candidate, not a plan.
            # Record both surfaces so an empty API plan cannot masquerade as savings.
            native_pointer = render_pointer({**result, "plan": None})
            rows.append(
                {
                    "kind": kind,
                    "query": query,
                    "expected": scale.task_text(*item) if expected else None,
                    "selected": top,
                    "latency_ms": latency,
                    "api_pointer_tokens": token_count(result["guidance"]),
                    "pointer_tokens": token_count(native_pointer),
                    "api_plan_gaps": (result.get("plan") or {}).get("gaps", []),
                }
            )
        service.db.engine.dispose()
        # Include WAL, rather than undercounting SQLite's working storage.
        storage = sum(p.stat().st_size for p in database.parent.glob("memory.sqlite*"))
        report = {
            "size": size,
            "local_learning_ms": learning_ms,
            "local_learning_model_calls": 0,
            "storage_bytes": storage,
            "decisions": rows,
        }
        destination.write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="HEAD")
    parser.add_argument("--size", type=int, default=1000)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 10 <= args.queries <= args.size <= 10000 or not 1 <= args.repeats <= 5:
        parser.error("Need 10 <= queries <= size <= 10000, and 1-5 repeats")
    if args.worker_root:
        worker(args.worker_root, args.size, args.queries, args.output)
        return
    baseline = subprocess.check_output(
        ["git", "rev-parse", args.baseline], cwd=ROOT, text=True
    ).strip()
    fingerprint = hashlib.sha256()
    for path in sorted((ROOT / "packages/musclememory").rglob("*.py")):
        fingerprint.update(str(path.relative_to(ROOT)).encode() + path.read_bytes())
    runs = []
    with tempfile.TemporaryDirectory(prefix="mm-compare-baseline-") as directory:
        temporary = Path(directory)
        archive = temporary / "baseline.tar"
        subprocess.run(
            [
                "git",
                "archive",
                "--output",
                str(archive),
                baseline,
                "packages/musclememory",
                "benchmarks/scale.py",
            ],
            cwd=ROOT,
            check=True,
        )
        before = temporary / "before"
        before.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(before, filter="data")
        for repeat in range(args.repeats):
            for arm in ["before", "after"] if repeat % 2 == 0 else ["after", "before"]:
                root = before if arm == "before" else ROOT
                output = temporary / f"{arm}-{repeat}.json"
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker-root",
                        str(root),
                        "--size",
                        str(args.size),
                        "--queries",
                        str(args.queries),
                        "--output",
                        str(output),
                    ],
                    cwd=root,
                    env={
                        **os.environ,
                        "PYTHONPATH": str(root / "packages"),
                        "HF_HUB_OFFLINE": "1",
                        "HF_HUB_DISABLE_TELEMETRY": "1",
                        "MM_LEARNING_AGENT": "",
                    },
                    check=True,
                )
                runs.append({"arm": arm, "repeat": repeat, **json.loads(output.read_text())})
                print(json.dumps({"arm": arm, "repeat": repeat, "complete": True}), flush=True)
    summaries = {}
    for arm in ("before", "after"):
        selected = [run for run in runs if run["arm"] == arm]
        rows = [row for run in selected for row in run["decisions"]]
        relevant = [row for row in rows if row["expected"]]
        decoys = [row for row in rows if row["expected"] is None]
        times = sorted(row["latency_ms"] for row in rows)
        summaries[arm] = {
            "recall_median_ms": statistics.median(times),
            "recall_p95_ms": times[int(0.95 * (len(times) - 1))],
            "local_learning_median_ms": statistics.median(
                v for run in selected for v in run["local_learning_ms"]
            ),
            "relevant_correct": sum(row["selected"] == row["expected"] for row in relevant),
            "relevant_decisions": len(relevant),
            "decoy_candidate_matches": sum(row["selected"] is not None for row in decoys),
            "decoy_decisions": len(decoys),
            "pointer_median_tokens": statistics.median(row["pointer_tokens"] for row in relevant),
            "pointer_max_tokens": max(row["pointer_tokens"] for row in rows),
            "storage_median_bytes": statistics.median(run["storage_bytes"] for run in selected),
        }
    report = {
        "baseline_commit": baseline,
        "after_source_sha256": fingerprint.hexdigest(),
        "embedding": "cached local all-MiniLM-L6-v2, 384 dimensions",
        "size": args.size,
        "repeats": args.repeats,
        "limitations": [
            "Synthetic retrieval fixtures; no coding agent or paid model was run.",
            "Compares committed local baseline with corrected checkout, not the interim rollout.",
            "No estimate of task success, real token savings, curation quality, or monetary cost.",
            "No external model calls; uses cached embeddings and temporary stores only.",
            "Candidate matches are not actual injections; current-file probes were not run.",
            "Pointer sizes use native-hook single-candidate rendering; API plan gaps are recorded.",
        ],
        "summary": summaries,
        "runs": runs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
