"""Recover test-proven skills from old Codex recordings without replaying tools.

Preview by default. --apply first backs up the local SQLite database, preserves
every source recording, and learns from deterministic native-receipt copies.
"""

import argparse
import json
import os
import re
import sqlite3
from collections import Counter
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from musclememory.client_hooks import CODEX_CONTEXT_TOOLS, digest, load_config, private_file
from musclememory.compiler import compile_trace
from musclememory.config import Settings
from musclememory.core import DomainError, ExecutionEvent, LearningPolicy, ObserveInput
from musclememory.local import config, home
from musclememory.recovery import RecoverySkipped, read_native_turns, recover_native_run
from musclememory.service import MemoryService
from musclememory.storage import Audit, Run
from sqlalchemy import select
from sqlalchemy.engine import make_url


def backup(path):
    directory = home() / "backups"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("Backup directory must be private (0700)")
    target = directory / ("skill-recovery-" + uuid4().hex + ".sqlite")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(target) as destination:
            source.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
    return str(target)


def migrate_recordings(apply=False):
    local = config()
    url = make_url(local["database_url"])
    if url.get_backend_name() != "sqlite" or not url.database:
        raise ValueError("Recovery requires the local SQLite installation")
    path = Path(url.database).resolve(strict=True)
    hook, _, _, client = load_config(home() / "hooks/codex.json", "codex")
    if str(client.http.base_url).rstrip("/") != local["url"].rstrip("/"):
        raise ValueError("The hook endpoint must match this local installation")
    service = MemoryService(
        replace(Settings.from_env(), database_url=local["database_url"], preferences_path=None)
    )
    report = {"applied": apply, "backup": None, "candidates": [], "skipped": {}, "results": []}
    reasons = Counter()
    try:
        principal = service.authenticate(client.http.headers["authorization"][7:], rate_limit=False)
        queue_path = Path(hook["state_dir"]) / "hooks.sqlite"
        private_file(queue_path)
        with sqlite3.connect(queue_path.as_uri() + "?mode=ro", uri=True) as queue:
            queue.row_factory = sqlite3.Row
            rows = queue.execute(
                "SELECT * FROM runs WHERE identity=? AND closed=1 AND ended=1 ORDER BY created",
                (client.capture_identity,),
            ).fetchall()
            by_session = {}
            for row in rows:
                with service.db.session() as session:
                    run = session.get(Run, (principal.tenant, row["token"]))
                    intent = queue.execute(
                        "SELECT payload FROM events WHERE run=? AND sequence=0", (row["token"],)
                    ).fetchone()
                    if (
                        not run
                        or run.namespace != row["scope"]
                        or run.runtime != json.loads(row["profile"])
                        or not intent
                        or run.intent != json.loads(intent[0]).get("intent_text")
                    ):
                        reasons["source_identity_mismatch"] += 1
                        continue
                    if run.status == "success":
                        # Includes the intentionally deleted/retired training skill.
                        reasons["already_successful"] += 1
                        continue
                    principal.require(run.namespace, "write")
                by_session.setdefault(row["session"], []).append(row)
            folder = Path(os.getenv("CODEX_HOME", Path.home() / ".codex")) / "sessions"
            plans = []
            found = set()
            for transcript in sorted(folder.glob("**/*.jsonl")):
                match = re.search(r"([0-9a-f-]{36})\.jsonl$", transcript.name)
                if not match:
                    continue
                session_id = digest(["codex", match[1]])
                if session_id not in by_session:
                    continue
                selected = by_session[session_id]
                found.add(session_id)
                try:
                    native = read_native_turns(transcript, {row["turn"] for row in selected})
                except (RecoverySkipped, OSError) as error:
                    reasons[
                        str(error)
                        if isinstance(error, RecoverySkipped)
                        else "unreadable_transcript"
                    ] += len(selected)
                    continue
                for row in selected:
                    if row["turn"] not in native:
                        reasons["missing_native_turn"] += 1
                        continue
                    captured = [
                        (r[0], json.loads(r[1]))
                        for r in queue.execute(
                            "SELECT source,payload FROM events WHERE run=? ORDER BY sequence",
                            (row["token"],),
                        )
                    ]
                    try:
                        plan = recover_native_run(
                            row, captured, native[row["turn"]], CODEX_CONTEXT_TOOLS
                        )
                        proof = {
                            "evidence_event_ids": [
                                plan["events"][-1]["response"]["verifier_event"]
                            ],
                            "summary": "Native tests passed after all recorded code changes",
                        }
                        compile_trace(
                            plan["events"][0]["intent_text"],
                            [ExecutionEvent.model_validate(e) for e in plan["events"]],
                            LearningPolicy(),
                            plan["run_token"],
                            proof,
                        )
                        plans.append(plan)
                        report["candidates"].append(
                            {
                                "source_run": row["token"],
                                "recovered_run": plan["run_token"],
                                "tool_calls": len(plan["events"]) - 2,
                            }
                        )
                    except (RecoverySkipped, DomainError) as error:
                        reasons[error.code if isinstance(error, DomainError) else str(error)] += 1
            reasons["missing_transcript"] += sum(
                len(v) for k, v in by_session.items() if k not in found
            )
        if apply:
            report["backup"] = backup(path)
            for plan in plans:
                with service.db.session() as session:
                    existing = session.get(Run, (principal.tenant, plan["run_token"]))
                    recorded = session.scalar(
                        select(Audit.audit_id)
                        .where(
                            Audit.tenant == principal.tenant,
                            Audit.operation == "recover_capture",
                            Audit.reference == plan["run_token"],
                        )
                        .limit(1)
                    )
                    if existing and (existing.learned_result or recorded):
                        report["results"].append(
                            {"source_run": plan["source_run"], "action": "already_learned"}
                        )
                        continue
                try:
                    for start in range(0, len(plan["events"]), 128):
                        result = service.observe(
                            principal,
                            ObserveInput(
                                run_token=plan["run_token"],
                                runtime_profile=plan["runtime_profile"],
                                visibility_scope=plan["visibility_scope"],
                                event_batch=plan["events"][start : start + 128],
                            ),
                        )
                        if result["rejected_event_count"]:
                            raise ValueError(
                                "Recovered event delivery rejected: " + json.dumps(result)
                            )
                    result = service.auto_learn(principal, plan["run_token"])
                    report["results"].append({"source_run": plan["source_run"], **result})
                    if result.get("action") == "deferred":
                        continue  # Model outages are retryable, not processed watermarks.
                    with service.db.transaction() as session:
                        service.audit(
                            session,
                            principal,
                            plan["visibility_scope"],
                            "recover_capture",
                            plan["run_token"],
                        )
                        session.commit()
                except DomainError as error:
                    report["results"].append(
                        {
                            "source_run": plan["source_run"],
                            "action": "skipped",
                            "reason": error.code,
                        }
                    )
        report["skipped"] = dict(reasons)
        report["candidate_count"] = len(report["candidates"])
        report["actions"] = dict(Counter(r["action"] for r in report["results"]))
        return report
    finally:
        client.close()
        service.db.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, help="Save a private detailed JSON report")
    args = parser.parse_args()
    result = migrate_recordings(args.apply)
    if args.report:
        descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(result, output, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k not in {"candidates", "results"}}))
