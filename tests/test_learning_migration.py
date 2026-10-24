import pytest
from musclememory.backfill import rebuild_memory, revalidate_checks
from musclememory.core import DomainError, EvaluateInput, LearnInput, ObserveInput
from musclememory.learning import Assessment
from musclememory.storage import Evaluation, Event, Procedure, Revision, Run, utcnow
from sqlalchemy import select
from test_check_evidence import FIX, edit_session
from test_semantic_learning import Learner, accepted, proposal


def test_backfill_invalidates_old_false_checks_without_deleting_evidence(service, principal):
    edit_session(service, principal, "source", [FIX], test="npm run test:contracts")
    token = service.learn(
        principal,
        LearnInput(
            run_token="source",
            completion_proof={
                "succeeded": True,
                "evidence_event_ids": ["test"],
                "summary": "Contracts passed",
            },
        ),
    )["procedure_token"]
    edit_session(service, principal, "wrong", [FIX], test="npm run test:ota")
    with service.db.transaction() as db:
        db.add(
            Evaluation(
                tenant=principal.tenant,
                procedure_token=token,
                version=1,
                run_token="wrong",
                outcome={"succeeded": True, "procedure_version": 1, "evidence_event_ids": ["test"]},
                metrics={"observed": {"tool_calls": 2, "duration_ms": 0}},
            )
        )
        revision = db.get(Revision, (token, 1))
        revision.successes = 1
        original = list(db.scalars(select(Event.payload).order_by(Event.run_token, Event.sequence)))
        db.commit()
    assert revalidate_checks(service, principal, "team")[0]["valid"] is False
    with service.db.session() as db:
        assert db.get(Revision, (token, 1)).successes == 1
    assert revalidate_checks(service, principal, "team", apply=True)[0]["valid"] is False
    with service.db.session() as db:
        assert db.get(Revision, (token, 1)).successes == 0
        assert db.scalar(select(Evaluation)).outcome["invalidated"]
        assert (
            list(db.scalars(select(Event.payload).order_by(Event.run_token, Event.sequence)))
            == original
        )
    assert revalidate_checks(service, principal, "team", apply=True)[0]["valid"] is False


def test_rejected_backfill_quarantines_the_legacy_skill_and_preserves_its_revision(
    service, principal
):
    edit_session(service, principal, "source", [FIX])
    token = service.learn(
        principal,
        LearnInput(
            run_token="source",
            completion_proof={
                "succeeded": True,
                "evidence_event_ids": ["test"],
                "summary": "Tests passed",
            },
        ),
    )["procedure_token"]
    service.learner = Learner(
        lambda e: (
            proposal(e),
            Assessment(
                accept=False,
                reason="temporary",
                explanation="This is only a temporary incident recap",
            ),
        )
    )
    assert rebuild_memory(service, principal, "team")[0]["action"] == "rejected"
    with service.db.session() as db:
        assert db.get(Procedure, token).lifecycle == "flagged"
        assert db.get(Revision, (token, 1)) is not None
        assert db.get(Run, (principal.tenant, "source")) is not None
    listed = service.listing(principal, "team", "procedures")["items"][0]
    assert listed["admission"] == "legacy_pending_curation"
    assert listed["learned_method"] is None
    with pytest.raises(DomainError) as error:
        service.approve(principal, token)
    assert error.value.code == "learning_required"
    edit_session(service, principal, "repeated-patch", [FIX])
    with pytest.raises(DomainError) as error:
        service.evaluate(
            principal,
            EvaluateInput(
                procedure_token=token,
                run_token="repeated-patch",
                outcome_report={
                    "succeeded": True,
                    "procedure_version": 1,
                    "evidence_event_ids": ["test"],
                },
                performance_metrics={"observed": {"tool_calls": 2, "duration_ms": 0}},
            ),
        )
    assert error.value.code == "learning_required"
    with service.db.session() as db:
        assert db.get(Procedure, token).reviewed_at is None
        assert db.get(Procedure, token).lifecycle == "flagged"
        assert db.get(Revision, (token, 1)).successes == 0


def test_later_user_corrections_are_included_only_in_the_same_project(service, principal):
    edit_session(service, principal, "source", [FIX])
    events = service.get_run(principal, "source")["events"]
    for token, project, text in [
        (
            "correction",
            "same-project",
            "Use the environment key instead; remove the static key file",
        ),
        ("unrelated", "other-project", "Undo every environment-key change"),
    ]:
        batch = [{**e} for e in events]
        batch[0]["intent_text"] = text
        service.observe(
            principal,
            ObserveInput(
                run_token=token,
                visibility_scope="team",
                runtime_profile=service.get_run(principal, "source")["runtime_profile"],
                event_batch=batch,
            ),
        )
        with service.db.transaction() as db:
            db.get(Run, (principal.tenant, token)).project_scope = project
            db.get(Run, (principal.tenant, "source")).project_scope = "same-project"
            db.commit()
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    service.distill_run(principal, "source", dry_run=True)
    assert [c["id"] for c in service.learner.evidence["corrections"]] == ["user:correction"]


def test_activity_pages_and_search_cover_sessions_after_the_first_hundred(service, principal, http):
    from datetime import timedelta

    with service.db.transaction() as db:
        db.add_all(
            Run(
                tenant=principal.tenant,
                namespace="team",
                run_token=f"page-{i:03}",
                intent=f"Recorded session {i}" if i != 0 else "Older needle task",
                runtime={},
                status="success" if i % 2 else "unverified",
                created_at=utcnow() + timedelta(seconds=i),
                expires_at=utcnow() + timedelta(days=5),
            )
            for i in range(155)
        )
        db.add(
            Run(
                tenant="foreign",
                namespace="team",
                run_token="foreign",
                intent="Older needle task",
                runtime={},
                status="success",
                expires_at=utcnow() + timedelta(days=5),
            )
        )
        db.commit()
    first = http.get("/v1/runs", params={"visibility_scope": "team", "limit": 100}).json()
    second = http.get(
        "/v1/runs", params={"visibility_scope": "team", "limit": 100, "offset": 100}
    ).json()
    assert len(first["items"]) == 100 and first["total"] == 155 and first["has_more"]
    assert len(second["items"]) == 55 and not second["has_more"]
    assert not {r["run_token"] for r in first["items"]} & {r["run_token"] for r in second["items"]}
    search = http.get("/v1/runs", params={"visibility_scope": "team", "query": "needle"}).json()
    assert search["total"] == 1 and search["items"][0]["run_token"] == "page-000"
    filtered = http.get("/v1/runs", params={"visibility_scope": "team", "status": "success"}).json()
    assert filtered["total"] == 77 and filtered["status_counts"]["unverified"] == 78


def test_graph_and_learning_history_have_authenticated_scoped_api_routes(service, principal, http):
    edit_session(service, principal, "source", [FIX])
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    result = service.distill_run(principal, "source")
    graph = http.get(
        "/v1/graph", params={"visibility_scope": "team", "root": result["procedure_token"]}
    )
    assert graph.status_code == 200 and graph.json()["edges"]
    assert (
        http.get("/v1/learning", params={"visibility_scope": "team"}).json()["items"][0]["status"]
        == "accepted"
    )
    assert http.get("/v1/graph", params={"visibility_scope": "private"}).status_code == 403
    assert http.get("/v1/learning", params={"visibility_scope": "private"}).status_code == 403


def test_historical_evidence_cannot_replace_a_newer_method(service, principal):
    from datetime import timedelta

    edit_session(service, principal, "newer", [FIX])
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    first = service.distill_run(principal, "newer")
    edit_session(service, principal, "historical", [FIX])
    with service.db.transaction() as db:
        db.get(Run, (principal.tenant, "historical")).created_at -= timedelta(days=1)
        db.commit()
    service.learner.handler = lambda e: (
        proposal(
            e,
            action="refine",
            target=first["procedure_token"],
            target_version=1,
        ),
        accepted(),
    )
    result = service.distill_run(principal, "historical")
    assert result == {"action": "deferred", "reason": "learning_older_evidence"}
    with service.db.session() as db:
        assert db.get(Procedure, first["procedure_token"]).current_version == 1


def test_current_explicit_correction_can_supersede_a_prior_version(service, principal):
    edit_session(service, principal, "prior", [FIX])
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    first = service.distill_run(principal, "prior")
    record = service.get_run(principal, "prior")
    batch = [{**e} for e in record["events"]]
    batch[0]["intent_text"] = "Use explicit overrides instead of replacing the base mapping"
    service.observe(
        principal,
        ObserveInput(
            run_token="correction",
            visibility_scope="team",
            runtime_profile=record["runtime_profile"],
            event_batch=batch,
        ),
    )
    service.learner.handler = lambda e: (
        proposal(
            e,
            action="supersede",
            target=first["procedure_token"],
            target_version=1,
            correction_event_ids=["user:correction"],
        ),
        accepted(),
    )
    result = service.distill_run(principal, "correction")
    assert result["procedure_version"] == 2
    assert "supersedes" in {
        edge["relation"] for edge in service.memory_graph(principal, "team")["edges"]
    }
