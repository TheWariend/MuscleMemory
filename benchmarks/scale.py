"""Local scale test: recall speed and accuracy as the memory store grows.

Builds stores of 1k-100k validated coding procedures (no model calls), then
replays paraphrased tasks. Many stored procedures differ from each target by a
single detail (same file, different bug), so wrong picks are measurable.

    python benchmarks/scale.py --sizes 1000,10000 --backend sqlite
    python benchmarks/scale.py --backend postgres --postgres-url URL --reset-postgres

--reset-postgres drops and recreates the public schema of that database. Only
point it at a disposable test database.
"""

import argparse
import json
import platform
import random
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))

from musclememory.artifacts import extract_experience, search_document, semantic_document  # noqa: E402
from musclememory.cli import migrate  # noqa: E402
from musclememory.compiler import signature  # noqa: E402
from musclememory.config import DEFAULT_EMBEDDING_DIM, DEFAULT_EMBEDDING_MODEL, Settings  # noqa: E402
from musclememory.core import (  # noqa: E402
    EvaluateInput,
    LearnInput,
    ObserveInput,
    RecallInput,
    RuntimeProfile,
)
from musclememory.retrieval import local_embedding_model  # noqa: E402
from musclememory.service import MemoryService  # noqa: E402
from musclememory.storage import (  # noqa: E402
    ArtifactPosting,
    Procedure,
    Revision,
    ensure_vector_index,
    utcnow,
)
from sqlalchemy import create_engine, insert, text  # noqa: E402

SERVICES = ["api", "worker", "web", "cli", "sdk"]
MODULES = [
    "billing", "exports", "config", "pagination", "auth", "search", "cache", "payments",
    "invoices", "reports", "users", "sessions", "uploads", "notifications", "scheduler",
    "metrics", "logging", "routing", "storage", "inventory", "orders", "shipping", "pricing",
    "tax", "refunds", "ledger", "webhooks", "emails", "profiles", "permissions", "audit",
    "imports", "sync", "queue", "workers", "analytics", "dashboard", "settings", "locale",
    "themes",
]  # fmt: skip
COMPONENTS = [
    "parser", "serializer", "validator", "loader", "merger", "formatter", "client", "handler",
    "cursor", "index", "router", "model", "view", "controller", "adapter", "mapper", "filter",
    "sorter", "encoder", "decoder", "planner", "retrier", "limiter", "signer", "resolver",
    "builder", "reader", "writer", "exporter", "importer",
]  # fmt: skip
# Each bug has a different wording for stored tasks and for later requests.
BUGS = {
    "rounding error": "values come out rounded the wrong way",
    "duplicate rows": "the same record appears twice",
    "off-by-one page boundary": "the last item of each page goes missing",
    "null handling": "it crashes when a field is empty",
    "timezone offset": "times are shifted by a few hours",
    "unicode encoding": "accented characters turn into garbage",
    "refund sign": "refunds show up as positive amounts",
    "override precedence": "local settings are ignored over defaults",
    "explicit false values": "turning an option off has no effect",
    "nested merge": "inner sections get replaced instead of combined",
    "empty input": "an empty file makes it fail",
    "stale cache": "old results keep coming back after updates",
    "retry race": "two retries run at the same time",
    "missing header": "requests are rejected for lacking a header",
    "sort order": "results come back in the wrong order",
    "case sensitivity": "uppercase and lowercase names are treated differently",
    "trailing slash": "paths ending in a slash are not found",
    "ignored zero values": "a value of zero is treated as missing",
    "large id overflow": "very large identifiers break",
    "quote escaping": "text with quotes is mangled",
}
# Requests with nothing relevant in the store; MuscleMemory should stay quiet.
UNRELATED = [
    "Update the README badges for the new CI provider",
    "Bump the Node version used by the frontend build",
    "Write release notes for version 2.4",
    "Rename the default git branch to main",
    "Add a dark mode toggle to the marketing site",
    "Set up Dependabot for weekly dependency updates",
    "Translate the onboarding emails into Spanish",
    "Create a Grafana panel for disk usage",
    "Archive the old design mockups in the shared drive",
    "Configure pre-commit hooks for formatting",
]
PROFILE = RuntimeProfile(
    agent_name="scale-benchmark",
    bindings={"shell.execute": "Bash", "filesystem.patch": "apply_patch"},
)
PLACEHOLDER = {"service": "zzservice", "module": "zzmodule", "component": "zzcomponent"}


def combos():
    items = [
        (service, module, component, bug)
        for service in SERVICES
        for module in MODULES
        for component in COMPONENTS
        for bug in BUGS
    ]
    random.Random(7).shuffle(items)
    return items


def task_text(service, module, component, bug):
    return f"Fix the {bug} in the {module} {component} of the {service} service"


def request_text(service, module, component, bug, *, with_path=False):
    text_ = f"In the {service} service, the {module} {component} has a problem: {BUGS[bug]}."
    if with_path:
        text_ += f" See src/{service}/{module}/{component}.py"
    return text_ + " Please repair it."


def trace_events(intent):
    source = "src/zzservice/zzmodule/zzcomponent.py"
    test = "tests/zzservice/test_zzmodule_zzcomponent.py"
    patch = f"*** Begin Patch\n*** Update File: {source}\n@@\n-old\n+new\n*** End Patch"
    return [
        {"event_id": "intent", "sequence": 0, "kind": "intent", "intent_text": intent},
        {
            "event_id": "read",
            "sequence": 1,
            "kind": "tool",
            "tool_name": "Bash",
            "capability": "shell.execute",
            "effect": "destructive",
            "arguments": {"command": f"cat {source}"},
            "response": {"exit_code": 0},
        },
        {
            "event_id": "edit",
            "sequence": 2,
            "kind": "tool",
            "tool_name": "apply_patch",
            "capability": "filesystem.patch",
            "effect": "write",
            "arguments": {"input": patch},
            "dependencies": ["read"],
        },
        {
            "event_id": "verify",
            "sequence": 3,
            "kind": "tool",
            "tool_name": "Bash",
            "capability": "shell.execute",
            "effect": "destructive",
            "arguments": {"command": f"pytest -q {test}"},
            "response": {"exit_code": 0},
            "dependencies": ["edit"],
        },
        {"event_id": "done", "sequence": 4, "kind": "completion"},
    ]


def learn_template(service, principal, namespace):
    intent = "Fix the zzbug in the zzmodule zzcomponent of the zzservice service"
    for run in ("template", "template-check-0", "template-check-1"):
        service.observe(
            principal,
            ObserveInput(
                run_token=run,
                event_batch=trace_events(intent),
                runtime_profile=PROFILE,
                visibility_scope=namespace,
            ),
        )
    learned = service.learn(
        principal,
        LearnInput(
            run_token="template",
            completion_proof={
                "succeeded": True,
                "evidence_event_ids": ["verify"],
                "summary": "Tests passed",
            },
        ),
    )
    for run in ("template-check-0", "template-check-1"):
        service.evaluate(
            principal,
            EvaluateInput(
                procedure_token=learned["procedure_token"],
                run_token=run,
                outcome_report={
                    "succeeded": True,
                    "procedure_version": 1,
                    "evidence_event_ids": ["verify"],
                },
                performance_metrics={"observed": {"tool_calls": 3, "duration_ms": 1}},
            ),
        )
    with service.db.session() as session:
        procedure = session.get(Procedure, learned["procedure_token"])
        revision = session.get(Revision, (learned["procedure_token"], 1))
        template = {
            "lifecycle": procedure.lifecycle,
            "definition": revision.definition,
            "successes": revision.successes,
            "failures": revision.failures,
            "validation_status": revision.validation_status,
            "step_graph": revision.step_graph,
            "capability_signature": revision.capability_signature,
        }
    service.delete_memory(principal, learned["procedure_token"])
    assert template["lifecycle"] == "active", template["lifecycle"]
    return template


def instantiate(template, combo):
    service, module, component, bug = combo
    raw = json.dumps(template["definition"])
    for key, value in zip(("service", "module", "component"), combo[:3], strict=True):
        raw = raw.replace(PLACEHOLDER[key], value)
    raw = raw.replace("zzbug", bug)
    definition = json.loads(raw)
    intent = task_text(service, module, component, bug)
    definition["title"] = intent
    definition["intent_signature"] = signature(intent)
    definition["experience"] = extract_experience(definition["steps"])
    return definition


def build_store(service, template, items, tenant, namespace, batch=2000):
    model = local_embedding_model(service.settings.embedding_model, None)
    space = service.embeddings.space
    now = utcnow()
    with service.db.engine.begin() as connection:
        for start in range(0, len(items), batch):
            chunk = items[start : start + batch]
            definitions = [instantiate(template, combo) for combo in chunk]
            vectors = [
                vector.tolist()
                for vector in model.embed(
                    [semantic_document(item) for item in definitions], batch_size=256
                )
            ]
            procedures, revisions, postings = [], [], []
            for definition, vector in zip(definitions, vectors, strict=True):
                token = str(uuid.uuid4())
                procedures.append(
                    {
                        "procedure_token": token,
                        "tenant": tenant,
                        "namespace": namespace,
                        "current_version": 1,
                        "lifecycle": "active",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                revisions.append(
                    {
                        "procedure_token": token,
                        "version": 1,
                        "tenant": tenant,
                        "definition": definition,
                        "search_text": search_document(definition),
                        "embedding": vector,
                        "embedding_space": space,
                        "embedding_dimensions": len(vector),
                        "embedding_payload": None,
                        "step_graph": template["step_graph"],
                        "capability_signature": template["capability_signature"],
                        "successes": template["successes"],
                        "failures": template["failures"],
                        "validation_status": template["validation_status"],
                        "created_at": now,
                        "validated_at": now,
                    }
                )
                experience = definition["experience"]
                for direction, field in (
                    ("read", "reads"),
                    ("write", "writes"),
                    ("command", "commands"),
                ):
                    postings.extend(
                        {
                            "procedure_token": token,
                            "version": 1,
                            "direction": direction,
                            "value": value,
                            "tenant": tenant,
                            "namespace": namespace,
                        }
                        for value in dict.fromkeys(experience[field])
                    )
            connection.execute(insert(Procedure), procedures)
            connection.execute(insert(Revision), revisions)
            connection.execute(insert(ArtifactPosting), postings)
    if service.db.engine.dialect.name == "postgresql":
        with service.db.engine.begin() as connection:
            connection.execute(text("ANALYZE"))


def index_used(service, principal, namespace, query):
    """PostgreSQL only: confirm the semantic query plan uses the HNSW index."""
    vector = service.embeddings.embed(query)
    literal = "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
    dims = len(vector)
    with service.db.engine.begin() as connection:
        connection.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        plan = connection.execute(
            text(
                f"EXPLAIN SELECT r.procedure_token FROM revisions r "
                f"JOIN procedures p ON p.procedure_token = r.procedure_token "
                f"AND p.current_version = r.version "
                f"WHERE p.tenant = :tenant AND r.tenant = :tenant AND p.namespace = :ns "
                f"AND p.lifecycle = 'active' AND r.validation_status = 'validated' "
                f"AND r.embedding_dimensions = {dims} AND r.embedding_space = :space "
                f"ORDER BY CAST(r.embedding AS VECTOR({dims})) <=> '{literal}'::vector LIMIT 80"
            ),
            {"tenant": principal.tenant, "ns": namespace, "space": service.embeddings.space},
        ).scalars()
        plan = "\n".join(plan)
    return f"ix_revisions_vector_{dims}" in plan


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def run_size(url, size, queries, backend):
    namespace, settings = (
        "scale",
        Settings(
            database_url=url,
            embedding_provider="fastembed",
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            embedding_dim=DEFAULT_EMBEDDING_DIM,
            requests_per_minute=10_000_000,
        ),
    )
    migrate(url)
    service = MemoryService(settings)
    ensure_vector_index(service.db.engine, settings.embedding_dim)
    principal = service.authenticate(
        service.issue_key("scale", {namespace: ["read", "write", "delete"]})["api_key"], False
    )
    template = learn_template(service, principal, namespace)
    items = combos()
    stored, held_out = items[:size], items[-queries:]
    started = time.perf_counter()
    build_store(service, template, stored, principal.tenant, namespace)
    build_seconds = time.perf_counter() - started
    rng = random.Random(size)
    targets = rng.sample(stored, queries)
    cases = (
        [("paraphrase", combo, True) for combo in targets]
        + [("paraphrase_with_path", combo, True) for combo in targets[: queries // 2]]
        + [("not_stored", combo, False) for combo in held_out[: queries // 2]]
        + [("unrelated", text_, False) for text_ in UNRELATED]
    )
    service.recall(  # Warm the query path and model before timing.
        principal,
        RecallInput(
            intent_text=request_text(*targets[0]),
            runtime_profile=PROFILE,
            visibility_scope=namespace,
        ),
    )
    rows = []
    for kind, combo, should_match in cases:
        query = (
            combo
            if kind == "unrelated"
            else request_text(*combo, with_path=kind == "paraphrase_with_path")
        )
        begin = time.perf_counter()
        result = service.recall(
            principal,
            RecallInput(intent_text=query, runtime_profile=PROFILE, visibility_scope=namespace),
        )
        elapsed = (time.perf_counter() - begin) * 1000
        top = result["candidates"][0]["definition"]["title"] if result["candidates"] else None
        rows.append(
            {
                "kind": kind,
                "ms": elapsed,
                "matched": top is not None,
                "correct": kind != "unrelated" and top == task_text(*combo),
                "should_match": should_match,
            }
        )
    summary = {"backend": backend, "size": size, "build_seconds": round(build_seconds, 1)}
    for kind in ("paraphrase", "paraphrase_with_path", "not_stored", "unrelated"):
        group = [row for row in rows if row["kind"] == kind]
        times = [row["ms"] for row in group]
        entry = {
            "queries": len(group),
            "median_ms": round(statistics.median(times), 1),
            "p95_ms": round(percentile(times, 0.95), 1),
            "max_ms": round(max(times), 1),
        }
        if kind in {"not_stored", "unrelated"}:
            entry["injected_rate"] = round(sum(row["matched"] for row in group) / len(group), 3)
        else:
            entry["top1_correct"] = round(sum(row["correct"] for row in group) / len(group), 3)
            entry["wrong_pick"] = round(
                sum(row["matched"] and not row["correct"] for row in group) / len(group), 3
            )
            entry["no_match"] = round(sum(not row["matched"] for row in group) / len(group), 3)
        summary[kind] = entry
    if backend == "postgres":
        summary["hnsw_index_used"] = index_used(
            service, principal, namespace, request_text(*targets[0])
        )
    service.db.engine.dispose()
    return summary


def reset_postgres(url):
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sizes", default="1000,10000,100000")
    parser.add_argument("--backend", choices=["sqlite", "postgres"], default="sqlite")
    parser.add_argument("--postgres-url")
    parser.add_argument("--reset-postgres", action="store_true")
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.backend == "postgres" and not (args.postgres_url and args.reset_postgres):
        parser.error("postgres needs --postgres-url and --reset-postgres (disposable database)")
    results = []
    for size in [int(value) for value in args.sizes.split(",")]:
        if args.backend == "postgres":
            reset_postgres(args.postgres_url)
            url = args.postgres_url
            summary = run_size(url, size, args.queries, args.backend)
        else:
            with tempfile.TemporaryDirectory() as directory:
                url = f"sqlite:///{Path(directory) / 'memory.db'}"
                summary = run_size(url, size, args.queries, args.backend)
        print(json.dumps(summary), flush=True)
        results.append(summary)
    report = {
        "benchmark": "scale",
        "embedding_model": DEFAULT_EMBEDDING_MODEL,
        "python": platform.python_version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "results": results,
    }
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
