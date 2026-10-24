"""Revalidate derived claims and redistill evidence without replaying tools."""

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from sqlalchemy import select

from .core import DomainError, EvaluateInput
from .memory_graph import connect, link_learning, node
from .service import proves_test, reliability, skill_test
from .storage import Audit, Evaluation, Procedure, Revision, Run, utcnow


def align_skill_names(service, principal, namespace, *, apply=False):
    """Clear names assigned by the initial curator bypass, without naming anything.

    Only the known v3 assignment is repaired: the stored name equals the current
    proposal title, the revision predates separate naming, and no explicit naming
    audit exists. Deliberately named skills and their evidence remain untouched.
    """
    principal.require(namespace, "admin")
    results = []
    with service.db.transaction() as session:
        rows = session.execute(
            select(Procedure, Revision)
            .join(
                Revision,
                (Revision.procedure_token == Procedure.procedure_token)
                & (Revision.version == Procedure.current_version),
            )
            .where(Procedure.tenant == principal.tenant, Procedure.namespace == namespace)
        ).all()
        for procedure, revision in rows:
            learning = revision.definition.get("provenance", {}).get("learning") or {}
            if (
                learning.get("pipeline") != "semantic-learning-v3"
                or learning.get("naming_policy") == "separate"
                or not procedure.name
                or procedure.name != (learning.get("proposal") or {}).get("title")
            ):
                continue
            named = session.scalar(
                select(Audit.audit_id)
                .where(
                    Audit.tenant == principal.tenant,
                    Audit.namespace == namespace,
                    Audit.reference == procedure.procedure_token,
                    Audit.operation.in_(["name", "name_auto"]),
                )
                .limit(1)
            )
            if named:
                continue
            results.append(
                {"procedure_token": procedure.procedure_token, "previous_name": procedure.name}
            )
            if apply:
                procedure.name = None
                service.audit(
                    session, principal, namespace, "align_skill_names", procedure.procedure_token
                )
        if apply:
            session.commit()
    return results


def align_learning_contract(service, principal, namespace, *, apply=False):
    """Repair generated v3 metadata without rebuilding skills or changing trust.

    The display name and proposal remain intact. Only the canonical source title,
    its search index, and the implicitly added repository requirement are repaired.
    Explicitly requested isolation and all caller-authored requirements are kept.
    """
    from .artifacts import first_sentence, search_document, semantic_document
    from .compiler import signature

    principal.require(namespace, "write")
    results = []
    with service.db.transaction() as session:
        rows = session.execute(
            select(Procedure, Revision)
            .join(Revision, Procedure.procedure_token == Revision.procedure_token)
            .where(Procedure.tenant == principal.tenant, Procedure.namespace == namespace)
        ).all()
        for procedure, revision in rows:
            definition = revision.definition
            learning = definition.get("provenance", {}).get("learning")
            if not learning or learning.get("pipeline") != "semantic-learning-v3":
                continue
            source = session.get(Run, (principal.tenant, definition["provenance"]["source_run"]))
            requirements = dict(definition.get("environment_requirements", {}))
            implicit_scope = (
                not service.settings.learning_project_isolation
                and not learning.get("project_isolated", False)
                and learning.get("scope")
                and requirements.get("mm_project") == learning["scope"]
            )
            if implicit_scope:
                requirements.pop("mm_project")
            updated = {
                **definition,
                "title": first_sentence(source.intent) if source else definition["title"],
                "intent_signature": signature(source.intent)
                if source
                else definition["intent_signature"],
                "environment_requirements": requirements,
            }
            if updated == definition:
                continue
            results.append(
                {
                    "procedure_token": procedure.procedure_token,
                    "version": revision.version,
                    "canonical_title_restored": updated["title"] != definition["title"],
                    "implicit_project_requirement_removed": bool(implicit_scope),
                }
            )
            if apply:
                revision.definition = updated
                revision.search_text = search_document(updated)
                revision.embedding = service.embeddings.embed(semantic_document(updated))
                revision.embedding_payload = None
                revision.embedding_dimensions = service.settings.embedding_dim
                revision.embedding_space = service.embeddings.space
                service.audit(
                    session,
                    principal,
                    namespace,
                    "align_learning_contract",
                    procedure.procedure_token,
                )
        if apply:
            session.commit()
    if apply and results:
        from .retrieval import clear_vector_cache

        clear_vector_cache()
    return results


def index_native_scopes(service, principal, namespace):
    """Recover project metadata for old requests, including unverified corrections.

    This never changes event payloads or asserts an outcome. Native session/turn
    identity is bound to the owned hook queue before accepting its working root.
    """
    from .client_hooks import digest, load_config, private_file
    from .local import home
    from .recovery import RecoverySkipped, read_native_turns
    from .workspace import project_root

    config_path = home() / "hooks/codex.json"
    if not config_path.exists():
        return 0
    hook, _, _, client = load_config(config_path, "codex")
    indexed = 0
    try:
        queue_path = Path(hook["state_dir"]) / "hooks.sqlite"
        private_file(queue_path)
        with sqlite3.connect(queue_path.as_uri() + "?mode=ro", uri=True) as queue:
            queue.row_factory = sqlite3.Row
            rows = queue.execute(
                "SELECT * FROM runs WHERE identity=? AND scope=?",
                (client.capture_identity, namespace),
            ).fetchall()
            sessions = {}
            for row in rows:
                sessions.setdefault(row["session"], []).append(row)
            folder = Path(os.getenv("CODEX_HOME", Path.home() / ".codex")) / "sessions"
            with service.db.transaction() as session:
                for transcript in folder.glob("**/*.jsonl"):
                    match = re.search(r"([0-9a-f-]{36})\.jsonl$", transcript.name)
                    if not match or digest(["codex", match[1]]) not in sessions:
                        continue
                    selected = sessions[digest(["codex", match[1]])]
                    try:
                        turns = read_native_turns(transcript, {row["turn"] for row in selected})
                    except (RecoverySkipped, OSError):
                        continue  # Unknown scope stays unknown; never infer cross-project support.
                    for row in selected:
                        turn = turns.get(row["turn"])
                        run = session.get(Run, (principal.tenant, row["token"]))
                        if not turn or not turn.get("cwd") or not run:
                            continue
                        if (
                            run.namespace != namespace
                            or run.runtime != json.loads(row["profile"])
                            or digest(["codex", turn["session"]]) != row["session"]
                        ):
                            continue
                        root = project_root(turn["cwd"])[0] or turn["cwd"]
                        scope = hashlib.sha256(root.encode()).hexdigest()
                        if run.project_scope is None:
                            run.project_scope = scope
                            indexed += 1
                session.commit()
    finally:
        client.close()
    return indexed


def revalidate_checks(service, principal, namespace, *, apply=False):
    principal.require(namespace, "write")
    with service.db.session() as session:
        rows = session.scalars(
            select(Evaluation)
            .join(
                Procedure,
                Procedure.procedure_token == Evaluation.procedure_token,
            )
            .where(
                Evaluation.tenant == principal.tenant,
                Procedure.tenant == principal.tenant,
                Procedure.namespace == namespace,
            )
        ).all()
        checks = [
            (e.evaluation_id, e.procedure_token, e.version, e.run_token, dict(e.outcome), e.metrics)
            for e in rows
            if e.outcome.get("succeeded")
        ]
    results = []
    for identifier, token, _version, run, outcome, metrics in checks:
        if outcome.get("invalidated"):
            results.append(
                {
                    "evaluation": identifier,
                    "valid": False,
                    "reason": outcome.get("invalidation_reason", "previously_invalidated"),
                }
            )
            continue
        try:
            service.evaluate(
                principal,
                EvaluateInput(
                    procedure_token=token,
                    run_token=run,
                    outcome_report={
                        k: v
                        for k, v in outcome.items()
                        if k
                        in {
                            "succeeded",
                            "procedure_version",
                            "evidence_event_ids",
                            "deviations",
                            "failure_summary",
                        }
                    },
                    performance_metrics=metrics,
                ),
                _validate_only=True,
            )
            result = {"evaluation": identifier, "valid": True}
        except DomainError as error:
            result = {"evaluation": identifier, "valid": False, "reason": error.code}
            if apply:
                with service.db.transaction() as session:
                    stored = session.get(Evaluation, identifier)
                    stored.outcome = {
                        **stored.outcome,
                        "invalidated": True,
                        "invalidation_reason": error.code,
                    }
                    service.audit(session, principal, namespace, "invalidate_check", identifier)
                    session.commit()
        results.append(result)
    if apply:
        with service.db.transaction() as session:
            revisions = session.scalars(
                select(Revision)
                .join(
                    Procedure,
                    Procedure.procedure_token == Revision.procedure_token,
                )
                .where(Revision.tenant == principal.tenant, Procedure.namespace == namespace)
            ).all()
            for revision in revisions:
                evaluations = session.scalars(
                    select(Evaluation).where(
                        Evaluation.tenant == principal.tenant,
                        Evaluation.procedure_token == revision.procedure_token,
                        Evaluation.version == revision.version,
                    )
                ).all()
                valid = [e for e in evaluations if not e.outcome.get("invalidated")]
                revision.successes = sum(bool(e.outcome.get("succeeded")) for e in valid)
                revision.failures = sum(not e.outcome.get("succeeded") for e in valid)
                if revision.validation_status != "flagged":
                    revision.validation_status = (
                        "validated"
                        if (
                            revision.successes >= service.settings.min_successes
                            and reliability(revision) >= service.settings.min_reliability
                        )
                        else "provisional"
                    )
                if revision.validation_status != "validated":
                    revision.validated_at = None
                sources = [
                    revision.definition["provenance"]["source_run"],
                    *(e.run_token for e in valid if e.outcome.get("succeeded")),
                ]
                test = skill_test(revision.definition)
                proven = False
                for source in sources:
                    run = session.get(Run, (principal.tenant, source))
                    if run and proves_test(service._events(session, run), test):
                        proven = True
                        break
                revision.test_proven_at = utcnow() if proven else None
                procedure = session.get(Procedure, revision.procedure_token)
                if (
                    procedure.current_version == revision.version
                    and procedure.lifecycle != "retired"
                ):
                    procedure.lifecycle = (
                        "active"
                        if revision.validation_status == "validated"
                        else revision.validation_status
                    )
            session.commit()
    return results


def rebuild_memory(service, principal, namespace, *, limit=None, progress=None):
    """Apply to the selected database (the CLI uses a scratch copy for previews)."""
    principal.require(namespace, "write")
    with service.db.session() as session:
        runs = session.scalars(
            select(Run).where(
                Run.tenant == principal.tenant,
                Run.namespace == namespace,
                Run.status == "success",
            )
        ).all()
        ordered = []
        for run in runs:
            events = service._events(session, run)
            when = (
                events[0].occurred_at.replace(tzinfo=None)
                if events and events[0].occurred_at
                else run.created_at
            )
            ordered.append(
                (
                    when,
                    run.run_token,
                    (run.learned_result or {}).get("result", {}).get("procedure_token"),
                )
            )
    results = []
    for _, run_token, legacy in sorted(ordered)[:limit]:
        if legacy:
            with service.db.transaction() as session:
                procedure = session.get(Procedure, legacy)
                if not procedure or procedure.lifecycle == "retired":
                    continue
                current = session.get(Revision, (legacy, procedure.current_version))
                if not current.definition.get("provenance", {}).get("learning"):
                    procedure.lifecycle, procedure.reviewed_at = "flagged", None
                    current.validation_status = "flagged"
                    current.definition = {
                        **current.definition,
                        "provenance": {
                            **current.definition["provenance"],
                            "admission": "legacy_pending_curation",
                        },
                    }
                session.commit()
        try:
            result = service.distill_run(principal, run_token, rebuild=True)
        except DomainError as error:
            result = {"action": "deferred", "reason": error.code}
        if legacy and result["action"] in {"learned", "corroborated", "rejected"}:
            with service.db.transaction() as session:
                procedure = session.get(Procedure, legacy)
                target_token = result.get("procedure_token")
                target = session.get(Procedure, target_token) if target_token else None
                if target_token != legacy:
                    procedure.lifecycle, procedure.reviewed_at = "flagged", None
                    revision = session.get(Revision, (legacy, procedure.current_version))
                    revision.validation_status = "flagged"
                    if target:
                        connect(
                            session,
                            principal.tenant,
                            namespace,
                            node("skill", target_token, target.current_version),
                            "redistills",
                            node("skill", legacy, procedure.current_version),
                            {"source_run": run_token},
                        )
                session.commit()
        results.append({"run_token": run_token, **result})
        if progress:
            progress(results[-1])
    # Populate source edges for old revisions too. Their status/history remains explicit.
    with service.db.transaction() as session:
        rows = session.execute(
            select(Procedure, Revision)
            .join(
                Revision,
                Revision.procedure_token == Procedure.procedure_token,
            )
            .where(Procedure.tenant == principal.tenant, Procedure.namespace == namespace)
        ).all()
        for procedure, revision in rows:
            link_learning(
                session,
                principal.tenant,
                namespace,
                procedure.procedure_token,
                revision.version,
                revision.definition["provenance"]["source_run"],
            )
        session.commit()
    return results
