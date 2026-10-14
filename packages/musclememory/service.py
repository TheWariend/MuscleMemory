"""Transactional application boundary shared by both public interfaces."""

import hashlib
import json
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from .artifacts import operation_categories, search_document, semantic_document
from .boundaries import derive_boundaries, refine_boundaries
from .compiler import compile_trace
from .config import Settings
from .core import (
    CompletionProof,
    CreateRunInput,
    DomainError,
    EvaluateInput,
    ExecutionEvent,
    InspectInput,
    LearnInput,
    ObserveInput,
    RecallInput,
    SessionUsageInput,
)
from .retrieval import (
    Embeddings,
    clear_vector_cache,
    graph_neighbors,
    hybrid_candidates,
    rank_candidates,
    reliability,
)
from .security import Principal, digest_key, new_key, redact
from .shapes import (
    FILE_CHANGE,
    bound_capability,
    capability_signature,
    contrast_diff,
    shape_matches,
    signature_key,
)
from .storage import (
    ArtifactPosting,
    Audit,
    Contrast,
    Credential,
    Database,
    Evaluation,
    Event,
    Procedure,
    RateWindow,
    Revision,
    Run,
    SessionUsage,
    SignInCode,
    utcnow,
)


def changed_files(event):
    """Project files a successful tool event changed, by any tool, as a set."""
    from .artifacts import WORKSPACE_CHANGES, targets

    if event.status != "success" and not event.arguments.get(WORKSPACE_CHANGES):
        return set()
    if event.capability in FILE_CHANGE or event.arguments.get(WORKSPACE_CHANGES):
        return set(targets(event.arguments, event.effect, event.capability)[1]) or {"*"}
    return set()


class MemoryService:
    def __init__(self, settings: Settings, database=None, embeddings=None):
        self.settings = settings
        self.db = database or Database(settings.database_url)
        self.embeddings = embeddings or Embeddings(settings)

    def issue_key(self, tenant, grants, label="agent"):
        """Administrative CLI only: API users cannot create arbitrary tenant grants."""
        from pydantic import TypeAdapter

        from .core import Token

        TypeAdapter(Token).validate_python(tenant)
        for namespace, operations in grants.items():
            TypeAdapter(Token).validate_python(namespace)
            if not operations or set(operations) - {"read", "write", "delete", "admin"}:
                raise DomainError("invalid_grants", "Unknown or empty credential operations")
        if not grants:
            raise DomainError("invalid_grants", "A credential requires namespace grants")
        key = new_key()
        with self.db.transaction() as session:
            credential = Credential(
                tenant=tenant, digest=digest_key(key), label=redact(label[:100]), grants=grants
            )
            session.add(credential)
            session.flush()
            key_id = credential.key_id
            session.commit()
        return {"api_key": key, "key_id": key_id, "tenant": tenant, "grants": grants}

    def authenticate(self, key, rate_limit=True):
        if not key or len(key) > 512:
            raise DomainError("unauthorized", "A valid bearer credential is required", 401)
        with self.db.transaction() as session:
            credential = session.scalar(
                select(Credential)
                .where(Credential.digest == digest_key(key), Credential.revoked.is_(False))
                .with_for_update()
            )
            if not credential:
                raise DomainError("unauthorized", "A valid bearer credential is required", 401)
            if rate_limit:
                minute = int(time.time() // 60)
                window = session.get(RateWindow, credential.key_id, with_for_update=True)
                if not window:
                    window = RateWindow(key_id=credential.key_id, minute=minute, requests=0)
                    session.add(window)
                if window.minute != minute:
                    window.minute, window.requests = minute, 0
                if window.requests >= self.settings.requests_per_minute:
                    raise DomainError("rate_limited", "Credential request limit exceeded", 429)
                window.requests += 1
            principal = Principal(credential.key_id, credential.tenant, credential.grants)
            session.commit()
            return principal

    @staticmethod
    def audit(session, principal, namespace, operation, reference, units=1, duration_ms=0):
        session.add(
            Audit(
                tenant=principal.tenant,
                namespace=namespace,
                key_id=principal.key_id,
                operation=operation,
                reference=reference,
                units=units,
                duration_ms=duration_ms,
            )
        )

    @staticmethod
    def _clean(contract):
        return type(contract).model_validate(redact(contract.model_dump(mode="json")))

    def _run(self, session, principal, token, operation="write"):
        run = session.scalar(
            select(Run)
            .where(Run.tenant == principal.tenant, Run.run_token == token)
            .with_for_update()
        )
        if not run:
            raise DomainError("not_found", "Execution record was not found", 404)
        principal.require(run.namespace, operation)
        if run.expires_at <= utcnow():
            raise DomainError("expired_trace", "Execution trace retention has expired", 410)
        return run

    @staticmethod
    def _events(session, run):
        return [
            ExecutionEvent.model_validate(row.payload)
            for row in session.scalars(
                select(Event)
                .where(Event.tenant == run.tenant, Event.run_token == run.run_token)
                .order_by(Event.sequence)
            )
        ]

    def _procedure(self, session, principal, token, operation="read"):
        procedure = session.scalar(
            select(Procedure)
            .where(Procedure.tenant == principal.tenant, Procedure.procedure_token == token)
            .with_for_update()
        )
        if not procedure:
            raise DomainError("not_found", "Procedure was not found", 404)
        principal.require(procedure.namespace, operation)
        return procedure

    def create_run(self, principal, request: CreateRunInput):
        principal.require(request.visibility_scope, "write")
        request = self._clean(request)
        with self.db.transaction() as session:
            existing = session.get(Run, (principal.tenant, request.run_token))
            if existing:
                if (
                    existing.namespace != request.visibility_scope
                    or existing.intent != request.intent_text
                    or existing.runtime != request.runtime_profile.model_dump(mode="json")
                ):
                    raise DomainError(
                        "run_conflict", "Run token already identifies another execution", 409
                    )
            else:
                session.add(
                    Run(
                        tenant=principal.tenant,
                        run_token=request.run_token,
                        namespace=request.visibility_scope,
                        intent=request.intent_text,
                        runtime=request.runtime_profile.model_dump(mode="json"),
                        expires_at=utcnow() + timedelta(days=self.settings.trace_retention_days),
                    )
                )
                self.audit(
                    session, principal, request.visibility_scope, "create_run", request.run_token
                )
            session.commit()
        return {"run_token": request.run_token, "trace_reference": request.run_token}

    def observe(self, principal, request: ObserveInput):
        principal.require(request.visibility_scope, "write")
        request = self._clean(request)
        accepted, duplicates, errors = 0, 0, []
        with self.db.transaction() as session:
            run = session.scalar(
                select(Run)
                .where(Run.tenant == principal.tenant, Run.run_token == request.run_token)
                .with_for_update()
            )
            if not run:
                # MCP observation can create a trace when the batch starts with an intent.
                try:
                    first = ExecutionEvent.model_validate(request.event_batch[0])
                except ValidationError as error:
                    raise DomainError(
                        "missing_intent", "New traces must start with a valid intent event"
                    ) from error
                if first.kind != "intent" or first.sequence != 0:
                    raise DomainError(
                        "missing_intent", "New traces must start with sequence-zero intent"
                    )
                run = Run(
                    tenant=principal.tenant,
                    run_token=request.run_token,
                    namespace=request.visibility_scope,
                    intent=first.intent_text,
                    runtime=request.runtime_profile.model_dump(mode="json"),
                    expires_at=utcnow() + timedelta(days=self.settings.trace_retention_days),
                )
                session.add(run)
                session.flush()
            if run.namespace != request.visibility_scope:
                raise DomainError("run_conflict", "Run belongs to a different namespace", 409)
            if run.runtime != request.runtime_profile.model_dump(mode="json"):
                raise DomainError(
                    "runtime_conflict", "Runtime profile cannot change during an execution", 409
                )
            if run.expires_at <= utcnow():
                raise DomainError("expired_trace", "Execution trace retention has expired", 410)
            events = self._events(session, run)
            by_id = {event.event_id: event for event in events}
            next_sequence = max((e.sequence for e in events), default=-1) + 1
            closed = run.status != "recording"
            for index, raw in enumerate(request.event_batch):
                try:
                    event = ExecutionEvent.model_validate(raw)
                    prior = by_id.get(event.event_id)
                    if prior:
                        if prior.model_dump() != event.model_dump():
                            raise DomainError(
                                "event_conflict", "Duplicate event identifier has changed contents"
                            )
                        duplicates += 1
                        continue
                    if closed:
                        raise DomainError("trace_closed", "A completed trace is immutable")
                    if next_sequence >= 1024:
                        raise DomainError(
                            "trace_size", "An execution trace is limited to 1024 events"
                        )
                    if event.sequence != next_sequence:
                        raise DomainError(
                            "event_order", "Events require consecutive execution ordering"
                        )
                    if any(dep not in by_id for dep in event.dependencies):
                        raise DomainError(
                            "dependency_order", "Dependencies must reference earlier events"
                        )
                    if event.retry_of and (
                        event.retry_of not in by_id or by_id[event.retry_of].status != "failure"
                    ):
                        raise DomainError(
                            "retry_reference", "Retries must reference an earlier failed event"
                        )
                    if event.kind == "intent" and (
                        next_sequence != 0 or event.intent_text != run.intent
                    ):
                        raise DomainError(
                            "intent_conflict", "Intent must match the execution and appear first"
                        )
                    if event.kind == "tool":
                        if (
                            request.runtime_profile.bindings.get(event.capability)
                            != event.tool_name
                        ):
                            raise DomainError(
                                "capability_binding", "Tool is not bound to its declared capability"
                            )
                        if (
                            event.permission
                            and event.permission not in request.runtime_profile.permissions
                        ):
                            raise DomainError(
                                "permission_binding",
                                "Permission is absent from the runtime profile",
                            )
                    if len(json.dumps(event.model_dump(mode="json"))) > 65536:
                        raise DomainError("event_size", "Sanitized event exceeds 64 KiB")
                    session.add(
                        Event(
                            tenant=principal.tenant,
                            run_token=run.run_token,
                            event_id=event.event_id,
                            sequence=event.sequence,
                            payload=event.model_dump(mode="json"),
                        )
                    )
                    by_id[event.event_id] = event
                    next_sequence += 1
                    accepted += 1
                    if event.kind == "completion":
                        run.status = (
                            ("success" if event.status == "success" else "failure")
                            if event.capture_complete
                            else "incomplete"
                        )
                        closed = True
                except ValidationError as error:
                    errors.append(
                        {
                            "batch_index": index,
                            "code": "invalid_event",
                            "fields": [list(e["loc"]) for e in error.errors()],
                        }
                    )
                except DomainError as error:
                    errors.append(
                        {"batch_index": index, "code": error.code, "message": error.message}
                    )
            run.capability_signature = capability_signature(
                sorted(by_id.values(), key=lambda e: e.sequence)
            )
            run.capability_key = signature_key(run.capability_signature)
            self.audit(session, principal, run.namespace, "observe", run.run_token, units=accepted)
            session.commit()
        return {
            "accepted_event_count": accepted,
            "rejected_event_count": len(errors),
            "duplicate_event_count": duplicates,
            "validation_errors": errors,
            "trace_reference": request.run_token,
            "contract_version": "1",
        }

    @staticmethod
    def validate_proof(events, proof: CompletionProof):
        by_id = {e.event_id: e for e in events}
        if (
            not proof.succeeded
            or not events
            or events[-1].kind != "completion"
            or events[-1].status != "success"
            or not events[-1].capture_complete
        ):
            raise DomainError(
                "unverified_run", "Learning requires a successful closed trace and proof"
            )
        for event_id in proof.evidence_event_ids:
            event = by_id.get(event_id)
            if (
                not event
                or event.kind != "tool"
                or event.status != "success"
                or not event.outcome_verified
            ):
                raise DomainError(
                    "invalid_evidence", "Evidence must reference successful tool events"
                )

    def learn(self, principal, request: LearnInput, *, auto=False):
        request = self._clean(request)
        fingerprint = hashlib.sha256(
            json.dumps(request.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        with self.db.transaction() as session:
            run = self._run(session, principal, request.run_token)
            if run.learned_result:
                if run.learned_result.get("request_digest") != fingerprint:
                    raise DomainError(
                        "learning_conflict", "Learning inputs changed for a compiled run", 409
                    )
                return run.learned_result["result"]
            events = self._events(session, run)
            self.validate_proof(events, request.completion_proof)
            definition, warnings = compile_trace(
                run.intent,
                events,
                request.learning_policy,
                run.run_token,
                request.completion_proof.model_dump(mode="json"),
            )
            kept = set(definition.step_graph["nodes"])
            if not set(request.completion_proof.evidence_event_ids).issubset(kept):
                raise DomainError(
                    "invalid_evidence", "Completion evidence was pruned from the graph"
                )
            definition.capability_signature = capability_signature(events)
            # Learn from failures: compare this success with failed runs that took
            # the same sequence of actions, and record what differed.
            contrasts = []
            failed_runs = session.scalars(
                select(Run)
                .where(
                    Run.tenant == principal.tenant,
                    Run.namespace == run.namespace,
                    Run.status == "failure",
                    Run.expires_at > utcnow(),
                    Run.capability_key == signature_key(capability_signature(events)),
                )
                .order_by(Run.created_at.desc(), Run.run_token)
                .limit(self.settings.candidate_limit)
            ).all()
            for failed in failed_runs:
                # Hash equality is followed by exact sequence equality.
                if failed.capability_signature != capability_signature(events):
                    continue
                try:
                    diff = contrast_diff(
                        self._events(session, failed),
                        [event for event in events if event.event_id in kept],
                    )
                except DomainError:
                    warnings.append("Failure contrast excluded: incomplete or redacted shapes.")
                    continue
                contrast = session.scalar(
                    select(Contrast).where(
                        Contrast.tenant == principal.tenant,
                        Contrast.namespace == run.namespace,
                        Contrast.failed_run_token == failed.run_token,
                        Contrast.successful_run_token == run.run_token,
                    )
                )
                if contrast is None:
                    contrast = Contrast(
                        tenant=principal.tenant,
                        namespace=run.namespace,
                        failed_run_token=failed.run_token,
                        successful_run_token=run.run_token,
                        capability_signature=capability_signature(events),
                        step_diff=diff,
                    )
                    session.add(contrast)
                contrasts.append(contrast)
            session.flush()
            definition.provenance["contrast_ids"] = [item.contrast_id for item in contrasts]
            derive_boundaries(definition, events, contrasts)
            warnings.extend(refine_boundaries(definition, contrasts, self.settings))
            if request.learning_policy.revise_procedure_token:
                procedure = self._procedure(
                    session, principal, request.learning_policy.revise_procedure_token, "write"
                )
                if procedure.namespace != run.namespace:
                    raise DomainError(
                        "revision_scope", "Revision traces must use the procedure namespace"
                    )
                procedure.current_version += 1
                procedure.lifecycle = "provisional"
                procedure.updated_at = utcnow()
            else:
                # Skills learned on purpose are approved; automatic ones wait for an OK.
                procedure = Procedure(
                    tenant=principal.tenant,
                    namespace=run.namespace,
                    current_version=1,
                    reviewed_at=None if auto else utcnow(),
                )
                session.add(procedure)
                session.flush()
            vector = self.embeddings.embed(semantic_document(definition.model_dump(mode="json")))
            session.add(
                Revision(
                    tenant=principal.tenant,
                    procedure_token=procedure.procedure_token,
                    version=procedure.current_version,
                    definition=definition.model_dump(mode="json"),
                    search_text=search_document(definition.model_dump(mode="json")),
                    embedding=vector,
                    embedding_dimensions=len(vector),
                    embedding_space=self.embeddings.space,
                    step_graph=definition.step_graph,
                    capability_signature=definition.capability_signature,
                )
            )
            session.flush()
            for direction, field in (
                ("read", "reads"),
                ("write", "writes"),
                ("command", "commands"),
            ):
                session.add_all(
                    ArtifactPosting(
                        tenant=principal.tenant,
                        namespace=run.namespace,
                        procedure_token=procedure.procedure_token,
                        version=procedure.current_version,
                        direction=direction,
                        value=value,
                    )
                    for value in definition.experience[field]
                    if len(value.encode()) <= 1024
                )
            result = {
                "procedure_token": procedure.procedure_token,
                "procedure_version": procedure.current_version,
                "extracted_steps": [s.model_dump(mode="json") for s in definition.steps],
                "validation_status": "provisional",
                "compilation_warnings": warnings,
                "contract_version": "1",
            }
            run.learned_result = {"result": result, "request_digest": fingerprint}
            self.audit(session, principal, run.namespace, "learn", procedure.procedure_token)
            session.commit()
            return result

    def recall(self, principal, request: RecallInput, *, observation_run=None):
        principal.require(request.visibility_scope, "read")
        request = self._clean(request)
        start = time.perf_counter()
        vector = self.embeddings.embed(request.intent_text)
        with self.db.session() as session:
            plan = None
            if observation_run:
                run = self._run(session, principal, observation_run, "read")
                if (
                    run.namespace != request.visibility_scope
                    or run.intent != request.intent_text
                    or run.runtime != request.runtime_profile.model_dump(mode="json")
                ):
                    raise DomainError(
                        "recall_run_conflict",
                        "Recall context differs from the owned execution",
                        409,
                    )
                if run.status != "recording":
                    raise DomainError(
                        "recall_run_closed", "Live recall requires a recording execution", 409
                    )
            candidates = hybrid_candidates(
                session,
                principal.tenant,
                request.visibility_scope,
                request.intent_text,
                vector,
                self.embeddings.space,
                self.settings.candidate_limit,
            )
            ranked = rank_candidates(
                candidates,
                request.intent_text,
                vector,
                request.runtime_profile,
                self.settings,
                self.embeddings.space,
            )[: request.match_limit]
            from .planning import compose_plan, task_segments

            segments = task_segments(request.intent_text)
            requested, gaps = [], []
            if len(segments) > 4:
                gaps.append({"reason": "segment_limit"})
            elif len(segments) > 1:
                for segment in segments:
                    segment_vector = self.embeddings.embed(segment)
                    pool = hybrid_candidates(
                        session,
                        principal.tenant,
                        request.visibility_scope,
                        segment,
                        segment_vector,
                        self.embeddings.space,
                        self.settings.candidate_limit,
                    )
                    matches = rank_candidates(
                        pool,
                        segment,
                        segment_vector,
                        request.runtime_profile,
                        self.settings,
                        self.embeddings.space,
                    )
                    if matches:
                        requested.append(matches[0])
                    else:
                        gaps.append({"reason": "uncovered_subgoal", "segment": segment[:200]})
            else:
                requested = ranked[:1]
            if requested or gaps:
                plan = compose_plan(
                    session,
                    principal.tenant,
                    request.visibility_scope,
                    requested,
                    request.runtime_profile,
                    self.settings,
                    gaps=gaps,
                )
                if not ranked and requested:
                    ranked = requested[: request.match_limit]
            related = graph_neighbors(
                session,
                principal.tenant,
                request.visibility_scope,
                ranked,
                request.runtime_profile,
                self.settings,
            )
            self.audit(
                session,
                principal,
                request.visibility_scope,
                "recall",
                "query",
                duration_ms=(time.perf_counter() - start) * 1000,
            )
            session.commit()
        from .delivery import render_pointer

        result = {
            "candidates": ranked,
            "no_match": not bool(ranked),
            "reason": None if ranked else "No relevant, validated, fresh, compatible procedure.",
            "vector_mode": self.embeddings.mode,
            "embedding": self.embeddings.metadata(),
            "related_procedures": related,
            "plan": plan,
            "contract_version": "1",
        }
        result["guidance"] = render_pointer(result, self.settings.guidance_token_budget)
        return result

    def reindex_embeddings(self, principal, namespace):
        """Rebuild search data without discarding history or validation evidence."""
        principal.require(namespace, "admin")
        count = 0
        with self.db.transaction() as session:
            rows = session.execute(
                select(Procedure, Revision)
                .join(
                    Revision,
                    (Revision.procedure_token == Procedure.procedure_token)
                    & (Revision.version == Procedure.current_version),
                )
                .where(
                    Procedure.tenant == principal.tenant,
                    Revision.tenant == principal.tenant,
                    Procedure.namespace == namespace,
                )
                .with_for_update()
            ).all()
            for _, revision in rows:
                document = search_document(revision.definition)
                vector = self.embeddings.embed(semantic_document(revision.definition))
                revision.search_text = document
                revision.embedding = vector
                revision.embedding_payload = None
                revision.embedding_dimensions = len(vector)
                revision.embedding_space = self.embeddings.space
                count += 1
            self.audit(session, principal, namespace, "reindex", "embeddings", units=count)
            clear_vector_cache()
            session.commit()
        return {"indexed_revisions": count, "embedding": self.embeddings.metadata()}

    def evaluate(self, principal, request: EvaluateInput):
        request = self._clean(request)
        with self.db.transaction() as session:
            procedure = self._procedure(session, principal, request.procedure_token, "write")
            run = self._run(session, principal, request.run_token)
            if run.namespace != procedure.namespace:
                raise DomainError("evaluation_scope", "Evaluation must use the procedure namespace")
            revision = session.get(
                Revision, (procedure.procedure_token, request.outcome_report.procedure_version)
            )
            if not revision:
                raise DomainError("not_found", "Procedure revision was not found", 404)
            if run.run_token == revision.definition["provenance"]["source_run"]:
                raise DomainError(
                    "training_evaluation", "The training run cannot validate its own procedure"
                )
            if (
                run.status not in {"success", "failure"}
                or (run.status == "success") != request.outcome_report.succeeded
            ):
                raise DomainError(
                    "outcome_conflict", "Outcome must match the closed execution trace"
                )
            events = self._events(session, run)
            if request.outcome_report.succeeded:
                required = {
                    step["capability"]
                    for step in revision.definition["steps"]
                    if not step.get("condition")
                }
                executed = {
                    e.capability
                    for e in events
                    if e.kind == "tool" and e.status == "success" and e.outcome_verified
                }
                # Changing a file counts however it was done: an edit, a patch, or a
                # shell command that changed project files.
                if any(changed_files(e) for e in events if e.kind == "tool"):
                    executed |= set(FILE_CHANGE)
                if any(bound_capability(capability, executed) is None for capability in required):
                    raise DomainError(
                        "application_evidence", "Trace did not execute the required capabilities"
                    )
                from .artifacts import (
                    WORKSPACE_CHANGES,
                    documentation_only,
                    inspection_only,
                    operation_categories,
                    targets,
                    test_command,
                )

                last_change = max(
                    (
                        event.sequence
                        for event in events
                        if event.kind == "tool"
                        and changed_files(event)
                        and not documentation_only(event.arguments, event.effect, event.capability)
                    ),
                    default=-1,
                )
                # Check actual observed action shape and operation categories;
                # a shell capability alone cannot prove a test or edit happened.
                for step in revision.definition["steps"]:
                    operations = operation_categories(step["arguments"], step["capability"])
                    if (
                        step.get("condition")
                        or inspection_only(step["arguments"], step["capability"])
                        or not (
                            step["effect"] != "read"
                            or step.get("critical")
                            or "verify" in operations
                        )
                    ):
                        continue
                    matches = [
                        event
                        for event in events
                        if event.kind == "tool"
                        and event.status == "success"
                        and event.outcome_verified
                        and event.capability == step["capability"]
                        and shape_matches(
                            step["arguments"],
                            {k: v for k, v in event.arguments.items() if k != WORKSPACE_CHANGES},
                            revision.definition["parameters"],
                        )
                        and set(operations).issubset(
                            operation_categories(event.arguments, event.capability)
                        )
                    ]
                    command = step["arguments"].get("command")
                    if "verify" in operations and not (
                        isinstance(command, dict) and set(command) == {"$mm_parameter"}
                    ):
                        stored_test = test_command(command)
                        matches = [
                            event
                            for event in matches
                            if stored_test
                            and test_command(event.arguments.get("command")) == stored_test
                            and event.event_id in request.outcome_report.evidence_event_ids
                            and event.sequence >= last_change
                        ]
                    if not matches and step["capability"] in FILE_CHANGE:
                        # Another agent's tool: the same files changed is the same step.
                        wanted = set(
                            targets(step["arguments"], step["effect"], step["capability"])[1]
                        )
                        matches = [
                            event
                            for event in events
                            if event.kind == "tool"
                            and (changed := changed_files(event))
                            and (not wanted or wanted & changed)
                        ]
                    if not matches:
                        raise DomainError(
                            "application_evidence",
                            "Trace lacks compatible action/verification evidence",
                        )
            if request.outcome_report.succeeded:
                self.validate_proof(
                    events,
                    CompletionProof(
                        succeeded=True,
                        evidence_event_ids=request.outcome_report.evidence_event_ids,
                        summary="Outcome evaluation",
                    ),
                )
            tools = [event for event in events if event.kind == "tool"]
            metrics = request.performance_metrics.observed
            if (
                metrics.tool_calls != len(tools)
                or abs(metrics.duration_ms - sum(e.duration_ms for e in tools)) > 1
            ):
                raise DomainError(
                    "metrics_conflict", "Measured tool calls and durations must match trace events"
                )
            if all(e.token_usage is not None for e in tools) and metrics.tokens != sum(
                e.token_usage for e in tools
            ):
                raise DomainError(
                    "metrics_conflict", "Token measurements must match available trace usage"
                )
            prior = session.scalar(
                select(Evaluation).where(
                    Evaluation.tenant == principal.tenant,
                    Evaluation.procedure_token == procedure.procedure_token,
                    Evaluation.version == revision.version,
                    Evaluation.run_token == run.run_token,
                )
            )
            outcome = request.outcome_report.model_dump(mode="json")
            performance = request.performance_metrics.model_dump(mode="json")
            if prior:
                if prior.outcome != outcome or prior.metrics != performance:
                    raise DomainError(
                        "evaluation_conflict", "An evaluation cannot change after recording", 409
                    )
            else:
                session.add(
                    Evaluation(
                        tenant=principal.tenant,
                        procedure_token=procedure.procedure_token,
                        version=revision.version,
                        run_token=run.run_token,
                        outcome=outcome,
                        metrics=performance,
                    )
                )
                revision.successes += int(request.outcome_report.succeeded)
                revision.failures += int(not request.outcome_report.succeeded)
                if not request.outcome_report.succeeded:
                    revision.validation_status = "flagged"
                elif (
                    revision.successes >= self.settings.min_successes
                    and reliability(revision) >= self.settings.min_reliability
                    and revision.validation_status != "flagged"
                ):
                    revision.validation_status = "validated"
                    revision.validated_at = utcnow()
                # An outcome for an old revision cannot change the current revision's lifecycle.
                if (
                    procedure.current_version == revision.version
                    and procedure.lifecycle != "retired"
                ):
                    procedure.lifecycle = (
                        "active"
                        if revision.validation_status == "validated"
                        else revision.validation_status
                    )
                    procedure.updated_at = utcnow()
                self.audit(session, principal, run.namespace, "evaluate", procedure.procedure_token)
            session.commit()
            return {
                "successes": revision.successes,
                "failures": revision.failures,
                "reliability": round(reliability(revision), 4),
                "validation_status": revision.validation_status,
                "regression_warnings": ["Revision failed; compile and validate a new revision."]
                if revision.validation_status == "flagged"
                else [],
                "contract_version": "1",
            }

    def inspect(self, principal, request: InspectInput):
        with self.db.transaction() as session:
            procedure = self._procedure(session, principal, request.procedure_token)
            version = (
                procedure.current_version
                if request.revision_selector == "latest"
                else request.revision_selector
            )
            revision = session.get(Revision, (procedure.procedure_token, version))
            if not revision:
                raise DomainError("not_found", "Procedure revision was not found", 404)
            history = session.scalars(
                select(Revision)
                .where(
                    Revision.procedure_token == procedure.procedure_token,
                    Revision.tenant == principal.tenant,
                )
                .order_by(Revision.version.desc())
                .limit(100)
            ).all()
            evaluations = session.scalars(
                select(Evaluation)
                .where(
                    Evaluation.tenant == principal.tenant,
                    Evaluation.procedure_token == procedure.procedure_token,
                    Evaluation.version == version,
                )
                .order_by(Evaluation.created_at.desc())
                .limit(100)
            ).all()
            contrasts = session.scalars(
                select(Contrast)
                .where(
                    Contrast.tenant == principal.tenant,
                    Contrast.namespace == procedure.namespace,
                    Contrast.successful_run_token
                    == revision.definition["provenance"]["source_run"],
                )
                .order_by(Contrast.created_at.desc())
                .limit(100)
            ).all()
            self.audit(
                session, principal, procedure.namespace, "inspect", procedure.procedure_token
            )
            session.commit()
            return {
                "procedure_token": procedure.procedure_token,
                "procedure_version": version,
                "visibility_scope": procedure.namespace,
                "lifecycle_status": procedure.lifecycle,
                "needs_review": procedure.reviewed_at is None,
                "definition": revision.definition,
                "created_at": procedure.created_at.isoformat() + "Z",
                "updated_at": procedure.updated_at.isoformat() + "Z",
                "version_history": [
                    {
                        "version": r.version,
                        "status": r.validation_status,
                        "successes": r.successes,
                        "failures": r.failures,
                    }
                    for r in history
                ],
                "validation_history": [
                    {"run_token": e.run_token, "outcome": e.outcome, "metrics": e.metrics}
                    for e in evaluations
                ],
                "contrasts": [
                    {
                        "contrast_id": item.contrast_id,
                        "failed_run_token": item.failed_run_token,
                        "successful_run_token": item.successful_run_token,
                        "capability_signature": item.capability_signature,
                        "step_diff": item.step_diff,
                    }
                    for item in contrasts
                ],
                "summary": self._summary(session, principal, revision, evaluations),
                "successes": revision.successes,
                "failures": revision.failures,
                "confidence": reliability(revision),
                "stale": (utcnow() - (revision.validated_at or revision.created_at)).days
                >= self.settings.stale_days,
                "contract_version": "1",
            }

    def _summary(self, session, principal, revision, evaluations):
        """What a person needs to read a skill: files, edits, the test and who proved it."""
        from .artifacts import extract_experience, inspection_only, test_command

        experience = extract_experience(revision.definition["steps"])
        changed = set(experience["writes"])
        tests = [
            test_command(step["arguments"].get("command"))
            for step in revision.definition["steps"]
            if "verify" in operation_categories(step["arguments"], step["capability"])
        ]

        def agent(run_token):
            run = session.get(Run, (principal.tenant, run_token))
            return (run.runtime or {}).get("agent_name") if run else None

        checked = {}
        for evaluation in evaluations:
            if (evaluation.outcome or {}).get("succeeded"):
                name = agent(evaluation.run_token) or "unknown"
                checked[name] = checked.get(name, 0) + 1
        source = session.get(
            Run, (principal.tenant, revision.definition["provenance"].get("source_run", ""))
        )
        return {
            "request": source.intent if source else None,
            "reads": [path for path in experience["reads"] if path not in changed][:12],
            "changes": experience["writes"][:12],
            "edits": experience["edits"][:6],
            "test": next((test for test in reversed(tests) if test), None),
            "look_around_steps": sum(
                inspection_only(step["arguments"], step["capability"])
                for step in revision.definition["steps"]
            ),
            "learned_by": agent(revision.definition["provenance"].get("source_run", "")),
            "checked_by": checked,
        }

    def export_skill(self, principal, token, agent):
        """Write a proven, kept skill into an agent's skills folder as SKILL.md."""
        from . import skill_export

        detail = self.inspect(principal, InspectInput(procedure_token=token))
        namespace = detail["visibility_scope"]
        principal.require(namespace, "admin")
        if self.settings.deployment_mode != "local":
            raise DomainError(
                "local_only", "Exporting skills works only on a local installation", 403
            )
        if detail["lifecycle_status"] != "active" or detail["needs_review"]:
            raise DomainError(
                "not_ready",
                "Only skills that are Ready (checked twice and kept) can be exported",
                409,
            )
        definition = detail["definition"]
        text, name = skill_export.render_proven(
            detail["summary"]["request"] or definition["title"],
            detail["summary"],
            detail["successes"],
            [*dict.fromkeys(definition.get("avoidance_rules", []))][:3],
            [*dict.fromkeys(definition.get("recovery_notes", []))][:2],
        )
        with tempfile.TemporaryDirectory() as staging:
            draft = Path(staging) / name
            skill_export.write_skill(draft, text)
            result = skill_export.install(agent, name, draft)
        with self.db.transaction() as session:
            self.audit(session, principal, namespace, "skill_export", f"{token}:{agent}")
            session.commit()
        return {**result, "procedure_token": token}

    def listing(self, principal, namespace, kind, limit=20, offset=0):
        principal.require(namespace, "read")
        if not 1 <= limit <= 100 or offset < 0:
            raise DomainError("pagination", "Invalid pagination", 422)
        with self.db.session() as session:
            if kind == "procedures":
                rows = session.execute(
                    select(Procedure, Revision)
                    .join(
                        Revision,
                        (Revision.procedure_token == Procedure.procedure_token)
                        & (Revision.version == Procedure.current_version)
                        & (Revision.tenant == Procedure.tenant),
                    )
                    .where(Procedure.tenant == principal.tenant, Procedure.namespace == namespace)
                    .order_by(Procedure.updated_at.desc(), Procedure.procedure_token)
                    .offset(offset)
                    .limit(limit)
                ).all()
                # The request each skill was learned from, when that session is still kept.
                sources = {
                    r.definition.get("provenance", {}).get("source_run") for _, r in rows
                } - {None}
                asked = dict(
                    session.execute(
                        select(Run.run_token, Run.intent).where(
                            Run.tenant == principal.tenant, Run.run_token.in_(sources)
                        )
                    ).all()
                )

                def summary(definition):
                    from .artifacts import extract_experience, test_command

                    experience = extract_experience(definition["steps"])
                    checks = [
                        test_command(command) or command
                        for command in experience["verification_commands"]
                    ]
                    return {
                        "learned_from": (
                            asked.get(definition.get("provenance", {}).get("source_run")) or ""
                        )[:300],
                        "changes": experience["writes"][:4],
                        "check": checks[-1:],
                    }

                return {
                    "items": [
                        {
                            **summary(r.definition),
                            "procedure_token": p.procedure_token,
                            "version": p.current_version,
                            "status": p.lifecycle,
                            "title": r.definition.get("title", ""),
                            "step_count": len(r.definition.get("steps", [])),
                            "successes": r.successes,
                            "failures": r.failures,
                            "confidence": round(reliability(r), 4),
                            "updated_at": p.updated_at.isoformat() + "Z",
                            "needs_review": p.reviewed_at is None,
                            "auto_learned": bool(r.definition.get("provenance", {}).get("auto")),
                        }
                        for p, r in rows
                    ],
                    "limit": limit,
                    "offset": offset,
                }
            rows = session.scalars(
                select(Run)
                .where(
                    Run.tenant == principal.tenant,
                    Run.namespace == namespace,
                    Run.expires_at > utcnow(),
                )
                .order_by(Run.created_at.desc(), Run.run_token)
                .offset(offset)
                .limit(limit)
            ).all()
            return {
                "items": [
                    {
                        "run_token": r.run_token,
                        "intent_text": r.intent,
                        "status": r.status,
                        "agent_name": (r.runtime or {}).get("agent_name"),
                        "created_at": r.created_at.isoformat() + "Z",
                    }
                    for r in rows
                ],
                "limit": limit,
                "offset": offset,
            }

    def get_run(self, principal, token):
        with self.db.transaction() as session:
            run = self._run(session, principal, token, "read")
            return {
                "run_token": run.run_token,
                "intent_text": run.intent,
                "status": run.status,
                "runtime_profile": run.runtime,
                "events": [e.model_dump(mode="json") for e in self._events(session, run)],
            }

    def delete_memory(self, principal, token, kind="procedure"):
        with self.db.transaction() as session:
            record = (
                self._procedure(session, principal, token, "delete")
                if kind == "procedure"
                else self._run(session, principal, token, "delete")
            )
            namespace = record.namespace
            if kind == "procedure":
                # Remove cached compilation result so deleted guidance is not returned on retry.
                runs = session.scalars(
                    select(Run).where(Run.tenant == principal.tenant, Run.namespace == namespace)
                ).all()
                for run in runs:
                    if (
                        run.learned_result
                        and run.learned_result["result"]["procedure_token"] == token
                    ):
                        run.learned_result = None
            session.delete(record)
            self.audit(session, principal, namespace, f"delete_{kind}", token)
            session.commit()
        return {"deleted": True}

    def approve(self, principal, token):
        """A person's OK: an automatically learned skill may now reach agents once verified."""
        with self.db.transaction() as session:
            procedure = self._procedure(session, principal, token, "write")
            if procedure.reviewed_at is None:
                procedure.reviewed_at = utcnow()
                procedure.updated_at = utcnow()
            self.audit(session, principal, procedure.namespace, "approve", token)
            session.commit()
            return {"procedure_token": token, "approved": True}

    def auto_learn(self, principal, run_token, hint=None):
        """After a session whose tests passed: count it as a check, or learn a new skill."""
        from .artifacts import extract_experience, operation_categories

        with self.db.session() as session:
            run = self._run(session, principal, run_token, "write")
            if run.status != "success":
                raise DomainError("unverified_run", "Only sessions that passed can teach skills")
            events = self._events(session, run)
            namespace = run.namespace
        tools = [e for e in events if e.kind == "tool" and e.status == "success"]
        verifiers = [
            e
            for e in tools
            if e.outcome_verified and "verify" in operation_categories(e.arguments, e.capability)
        ]
        if not verifiers:
            raise DomainError("unverified_run", "The session has no passing test command")
        experience = extract_experience(
            [
                {
                    "step_token": e.event_id,
                    "arguments": e.arguments,
                    "capability": e.capability,
                    "effect": e.effect,
                }
                for e in tools
            ]
        )
        writes = set(experience["writes"])
        candidates = []
        if hint:
            candidates.append(hint)
        if writes:
            with self.db.session() as session:
                candidates += session.scalars(
                    select(ArtifactPosting.procedure_token)
                    .join(
                        Procedure,
                        (Procedure.procedure_token == ArtifactPosting.procedure_token)
                        & (Procedure.current_version == ArtifactPosting.version),
                    )
                    .where(
                        ArtifactPosting.tenant == principal.tenant,
                        ArtifactPosting.namespace == namespace,
                        ArtifactPosting.direction == "write",
                        ArtifactPosting.value.in_(sorted(writes)),
                        Procedure.lifecycle.in_(["provisional", "active"]),
                    )
                    .distinct()
                    .limit(self.settings.candidate_limit)
                ).all()
        metrics = {
            "observed": {
                "tool_calls": len(tools),
                "duration_ms": sum(e.duration_ms for e in tools),
            }
        }
        validated = []
        for token in dict.fromkeys(candidates):
            with self.db.session() as session:
                procedure = session.get(Procedure, token)
                if not procedure or procedure.tenant != principal.tenant:
                    continue
                version = procedure.current_version
            try:
                result = self.evaluate(
                    principal,
                    EvaluateInput(
                        procedure_token=token,
                        run_token=run_token,
                        outcome_report={
                            "succeeded": True,
                            "procedure_version": version,
                            "evidence_event_ids": [verifiers[-1].event_id],
                        },
                        performance_metrics=metrics,
                    ),
                )
            except DomainError:
                continue  # A different task touching the same files is not a check.
            validated.append({"procedure_token": token, "successes": result.get("successes")})
        if validated:
            return {"action": "checked", "procedures": validated}
        learned = self.learn(
            principal,
            LearnInput(
                run_token=run_token,
                completion_proof={
                    "succeeded": True,
                    "evidence_event_ids": [verifiers[-1].event_id],
                    "verifier": "tool_assertion",
                    "summary": "Tests passed after the last change",
                },
                learning_policy={},
            ),
            auto=True,
        )
        with self.db.transaction() as session:
            revision = session.get(Revision, (learned["procedure_token"], 1))
            revision.definition = {
                **revision.definition,
                "provenance": {**revision.definition["provenance"], "auto": True},
            }
            session.commit()
        return {"action": "learned", "procedure_token": learned["procedure_token"]}

    def create_sign_in_code(self, tenant, grants, minutes=5):
        """Administrative CLI only: a one-time code the dashboard swaps for a key."""
        code = new_key()
        with self.db.transaction() as session:
            session.add(
                SignInCode(
                    code_digest=digest_key(code),
                    tenant=tenant,
                    grants=grants,
                    expires_at=utcnow() + timedelta(minutes=minutes),
                )
            )
            session.commit()
        return code

    def redeem_sign_in_code(self, code):
        with self.db.transaction() as session:
            row = session.get(SignInCode, digest_key(code or ""))
            if not row or row.used or row.expires_at < utcnow():
                raise DomainError(
                    "sign_in_expired", "This sign-in link expired. Run it again.", 401
                )
            row.used = True
            tenant, grants = row.tenant, row.grants
            session.commit()
        return self.issue_key(tenant, grants, "Dashboard sign-in")

    def retire(self, principal, token):
        with self.db.transaction() as session:
            procedure = self._procedure(session, principal, token, "write")
            procedure.lifecycle = "retired"
            self.audit(session, principal, procedure.namespace, "retire", token)
            session.commit()
        return {"lifecycle_status": "retired"}

    def purge_expired(self):
        with self.db.transaction() as session:
            count = session.execute(delete(Run).where(Run.expires_at <= utcnow())).rowcount
            session.commit()
        return {"deleted_traces": count}

    def usage(self, principal, namespace):
        principal.require(namespace, "read")
        with self.db.session() as session:
            operations = session.execute(
                select(Audit.operation, func.count(), func.sum(Audit.units))
                .where(Audit.tenant == principal.tenant, Audit.namespace == namespace)
                .group_by(Audit.operation)
            ).all()
            metrics = session.scalars(
                select(Evaluation)
                .join(Procedure, Procedure.procedure_token == Evaluation.procedure_token)
                .where(
                    Evaluation.tenant == principal.tenant,
                    Procedure.tenant == principal.tenant,
                    Procedure.namespace == namespace,
                )
            ).all()
            savings = {"tool_calls": 0, "duration_ms": 0.0, "tokens": 0, "cost_usd": 0.0}
            pairs = {key: 0 for key in savings}
            for evaluation in metrics:
                baseline = evaluation.metrics.get("baseline")
                if not baseline:
                    continue
                observed = evaluation.metrics["observed"]
                for key in savings:
                    if baseline.get(key) is not None and observed.get(key) is not None:
                        savings[key] += baseline[key] - observed[key]
                        pairs[key] += 1
            return {
                "operations": [
                    {"operation": op, "requests": count, "units": units}
                    for op, count, units in operations
                ],
                "observed_savings": savings,
                "paired_measurements": pairs,
                "measurement_source": (
                    "client-reported paired baselines; observed calls/durations trace-checked"
                ),
                "evaluations": len(metrics),
                "sessions": self.session_usage(principal, namespace),
            }

    def record_session_usage(self, principal, token, request: SessionUsageInput):
        request = self._clean(request)
        payload = request.model_dump(mode="json")
        with self.db.transaction() as session:
            run = self._run(session, principal, token)
            captured_calls = (
                request.captured_tool_calls
                if request.captured_tool_calls is not None
                else request.tool_calls
            )
            if request.capture_complete and captured_calls is not None:
                count = session.scalar(
                    select(func.count())
                    .select_from(Event)
                    .where(
                        Event.tenant == principal.tenant,
                        Event.run_token == token,
                        Event.payload["kind"].as_string() == "tool",
                    )
                )
                if count != captured_calls:
                    raise DomainError("usage_conflict", "Complete capture tool count differs", 409)
            prior = session.get(SessionUsage, (principal.tenant, token))
            previous = (
                SessionUsageInput.model_validate(prior.measurements).model_dump(mode="json")
                if prior
                else None
            )
            if previous is not None and previous != payload:
                raise DomainError("usage_conflict", "Session measurements are immutable", 409)
            if not prior:
                session.add(
                    SessionUsage(
                        tenant=principal.tenant,
                        run_token=token,
                        namespace=run.namespace,
                        measurements=payload,
                    )
                )
                self.audit(session, principal, run.namespace, "session_usage", token)
            session.commit()
        return {"recorded": True, "run_token": token}

    def session_usage(self, principal, namespace, limit=100, offset=0):
        principal.require(namespace, "read")
        with self.db.session() as session:
            rows = session.scalars(
                select(SessionUsage)
                .where(SessionUsage.tenant == principal.tenant, SessionUsage.namespace == namespace)
                .order_by(SessionUsage.created_at.desc(), SessionUsage.run_token)
                .limit(limit)
                .offset(offset)
            ).all()
            return {
                "items": [{"run_token": row.run_token, **row.measurements} for row in rows],
                "limit": limit,
                "offset": offset,
                "cost_notice": "API-equivalent estimates are separate from billed charges; "
                "subscription usage does not establish a per-task bill.",
            }

    def list_keys(self, principal, namespace):
        principal.require(namespace, "admin")
        with self.db.session() as session:
            rows = session.scalars(
                select(Credential).where(Credential.tenant == principal.tenant)
            ).all()
            return {
                "items": [
                    {
                        "key_id": c.key_id,
                        "label": c.label,
                        "revoked": c.revoked,
                        "grants": {namespace: c.grants[namespace]},
                    }
                    for c in rows
                    if namespace in c.grants
                ]
            }

    def revoke_key(self, principal, key_id, namespace):
        principal.require(namespace, "admin")
        with self.db.transaction() as session:
            credential = session.scalar(
                select(Credential)
                .where(Credential.tenant == principal.tenant, Credential.key_id == key_id)
                .with_for_update()
            )
            if not credential or namespace not in credential.grants:
                raise DomainError("not_found", "Credential was not found", 404)
            # A namespace administrator cannot revoke credentials covering other namespaces.
            if any("admin" not in principal.grants.get(n, []) for n in credential.grants):
                raise DomainError(
                    "forbidden", "Admin access to all credential namespaces is required", 403
                )
            credential.revoked = True
            self.audit(session, principal, namespace, "revoke_key", key_id)
            session.commit()
        return {"revoked": True}


def conflict_error(error: IntegrityError):
    return DomainError("write_conflict", "Concurrent write conflicted; retry the same request", 409)
