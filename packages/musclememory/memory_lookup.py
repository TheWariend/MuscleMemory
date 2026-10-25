"""Delivery visibility. A hook report never proves the agent applied a skill."""

from sqlalchemy import func, select

from .artifacts import extract_experience, skill_name
from .core import DomainError
from .storage import MemoryLookup, Procedure, Revision


def record_lookup(service, principal, token, request):
    request = service._clean(request)
    with service.db.transaction() as session:
        run = service._run(session, principal, token)
        key = (principal.tenant, token, request.lookup_token)
        report = request.model_dump(mode="json")
        prior = session.get(MemoryLookup, key)
        if prior:
            if prior.payload["report"] != report:
                raise DomainError("lookup_conflict", "A delivery report cannot change", 409)
            return {"recorded": True}
        payload = {"report": report, "source": "hook_report", "application_verified": False}
        if request.procedure_token:
            procedure = service._procedure(session, principal, request.procedure_token)
            revision = session.get(Revision, (procedure.procedure_token, request.procedure_version))
            if procedure.namespace != run.namespace or not revision:
                raise DomainError("lookup_scope", "Skill does not belong to this workspace", 409)
            payload["skill_name"] = (
                procedure.name
                or skill_name(extract_experience(revision.definition["steps"]))
                or revision.definition["title"][:200]
            )
            payload["proof"] = {
                "successes": revision.successes,
                "failures": revision.failures,
                "reviewed": procedure.reviewed_at is not None,
                "validation_status": revision.validation_status,
            }
        else:
            rows = session.execute(
                select(Procedure.reviewed_at, Revision.successes)
                .join(
                    Revision,
                    (Revision.procedure_token == Procedure.procedure_token)
                    & (Revision.version == Procedure.current_version),
                )
                .where(
                    Procedure.tenant == principal.tenant,
                    Procedure.namespace == run.namespace,
                    Procedure.lifecycle.in_(["provisional", "active"]),
                )
            ).all()
            payload["workspace_readiness"] = {
                "needs_review": sum(reviewed is None for reviewed, _ in rows),
                "needs_checks": sum(checks < service.settings.min_successes for _, checks in rows),
                "required_checks": service.settings.min_successes,
            }
        session.add(
            MemoryLookup(
                tenant=principal.tenant,
                run_token=token,
                lookup_token=request.lookup_token,
                namespace=run.namespace,
                payload=payload,
            )
        )
        session.commit()
    return {"recorded": True}


def lookup_history(session, tenant, tokens):
    """One bounded query for a page of sessions; no prompts or file contents."""
    history = {token: [] for token in tokens}
    if not tokens:
        return history
    recent = (
        select(
            MemoryLookup.run_token,
            MemoryLookup.payload,
            MemoryLookup.created_at,
            func.row_number()
            .over(
                partition_by=MemoryLookup.run_token,
                order_by=(MemoryLookup.created_at.desc(), MemoryLookup.lookup_token.desc()),
            )
            .label("position"),
        )
        .where(MemoryLookup.tenant == tenant, MemoryLookup.run_token.in_(tokens))
        .subquery()
    )
    rows = session.execute(
        select(recent)
        .where(recent.c.position <= 20)
        .order_by(recent.c.run_token, recent.c.position)
    )
    for row in rows:
        history[row.run_token].append(
            {
                **row.payload["report"],
                **{key: value for key, value in row.payload.items() if key != "report"},
                "created_at": row.created_at.isoformat() + "Z",
            }
        )
    return history
