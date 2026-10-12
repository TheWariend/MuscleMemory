"""Measured local manifest-search workload, not a model-performance claim."""

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from musclememory.cli import migrate
from musclememory.compiler import materialize
from musclememory.config import Settings
from musclememory.core import (
    EvaluateInput,
    LearnInput,
    ObserveInput,
    ProcedureDefinition,
    RecallInput,
)
from musclememory.observer import ObservedRun
from musclememory.service import MemoryService


class LocalSink:
    def __init__(self, service, actor):
        self.service, self.actor = service, actor

    def musclememory_observe(self, **arguments):
        return self.service.observe(self.actor, ObserveInput(**arguments))


def percentile(samples, fraction):
    return sorted(samples)[min(len(samples) - 1, int((len(samples) - 1) * fraction))]


def benchmark(repetitions=20):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "project.toml"
        source.write_text('[project]\nname = "sample"\n')
        url = f"sqlite:///{root / 'memory.db'}"
        migrate(url)
        service = MemoryService(Settings(database_url=url, requests_per_minute=10000))
        actor = service.authenticate(
            service.issue_key("bench", {"local": ["read", "write"]})["api_key"], False
        )
        profile = {"agent_name": "serial-python-agent", "bindings": {"repository.inspect": "read"}}
        sink = LocalSink(service, actor)

        def execute(token, paths):
            run = ObservedRun(sink, "Find project package name", profile, "local", token)

            @run.instrument("repository.inspect")
            def read(path):
                return Path(path).read_text() if Path(path).exists() else "absent"

            success = False
            for path in paths:
                content = read(str(path))
                success = success or 'name = "sample"' in content
            run.complete(success)
            return run, success

        training, _ = execute("training", [source])
        started = time.perf_counter()
        learned = service.learn(
            actor,
            LearnInput(
                run_token=training.run_token,
                completion_proof={
                    "succeeded": True,
                    "evidence_event_ids": ["e1"],
                    "summary": "Found sample name",
                },
                learning_policy={
                    "parameters": {
                        "manifest_path": {
                            "definition": {"kind": "string"},
                            "observed_value": str(source),
                        }
                    }
                },
            ),
        )
        compilation_ms = (time.perf_counter() - started) * 1000
        for i in range(2):
            run, _ = execute(f"validation-{i}", [source])
            service.evaluate(
                actor,
                EvaluateInput(
                    procedure_token=learned["procedure_token"],
                    run_token=run.run_token,
                    outcome_report={
                        "succeeded": True,
                        "procedure_version": 1,
                        "evidence_event_ids": ["e1"],
                    },
                    performance_metrics={"observed": run.measurements()},
                ),
            )
        samples, retrieval_ms = [], []
        for i in range(repetitions):
            baseline_start = time.perf_counter()
            baseline, base_success = execute(
                f"baseline-{i}", [root / "package.json", root / "setup.py", source]
            )
            baseline_wall_ms = (time.perf_counter() - baseline_start) * 1000
            memory_start = time.perf_counter()
            started = time.perf_counter()
            recalled = service.recall(
                actor,
                RecallInput(
                    intent_text="Find project package name",
                    runtime_profile=profile,
                    visibility_scope="local",
                ),
            )
            retrieval_ms.append((time.perf_counter() - started) * 1000)
            definition = ProcedureDefinition.model_validate(recalled["candidates"][0]["definition"])
            steps = materialize(definition, {"manifest_path": str(source)})
            remembered, mem_success = execute(
                f"remembered-{i}", [step["arguments"]["path"] for step in steps]
            )
            memory_wall_ms = (time.perf_counter() - memory_start) * 1000
            service.evaluate(
                actor,
                EvaluateInput(
                    procedure_token=learned["procedure_token"],
                    run_token=remembered.run_token,
                    outcome_report={
                        "succeeded": mem_success,
                        "procedure_version": 1,
                        "evidence_event_ids": ["e1"],
                    },
                    performance_metrics={
                        "observed": remembered.measurements(),
                        "baseline": baseline.measurements(),
                    },
                ),
            )
            samples.append(
                {
                    "baseline": baseline.measurements(),
                    "memory": remembered.measurements(),
                    "baseline_succeeded": base_success,
                    "memory_succeeded": mem_success,
                    "baseline_wall_ms": baseline_wall_ms,
                    "memory_wall_ms": memory_wall_ms,
                }
            )
        result = {
            "workload": "Deterministic local manifest lookup; no LLM calls",
            "repetitions": repetitions,
            "samples": samples,
            "compilation_ms": compilation_ms,
            "retrieval_ms": {
                "median": statistics.median(retrieval_ms),
                "p95": percentile(retrieval_ms, 0.95),
            },
            "storage_bytes": (root / "memory.db").stat().st_size,
            "tokens": None,
            "cost_usd": None,
            "success_counts": {
                "baseline": sum(sample["baseline_succeeded"] for sample in samples),
                "memory": sum(sample["memory_succeeded"] for sample in samples),
            },
            "notes": "Tool time excludes retrieval/observation overhead. "
            "Retrieval is reported separately. "
            "Wall times include observation on both paths, plus retrieval on the memory path; "
            "evaluation and one-time learning are separate. "
            "Token usage and model cost are unavailable. Storage bytes exclude WAL.",
        }
        service.db.engine.dispose()
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--output", default="benchmark-results.json")
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 10000:
        parser.error("repetitions must be between 1 and 10000")
    result = benchmark(args.repetitions)
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "samples"}, indent=2))
