import json
from datetime import timedelta

import pytest
from conftest import PROFILE, evaluate, learned, trace, validated
from musclememory.compiler import compile_trace, materialize
from musclememory.core import (
    DomainError,
    EvaluateInput,
    ExecutionEvent,
    InspectInput,
    LearningPolicy,
    LearnInput,
    ObserveInput,
    ProcedureDefinition,
    RecallInput,
    RuntimeProfile,
)
from musclememory.storage import Event, Procedure, Revision, Run, utcnow
from sqlalchemy import func, select


def recall(service, principal, profile=PROFILE, intent="Inspect repository manifest"):
    return service.recall(
        principal, RecallInput(intent_text=intent, runtime_profile=profile, visibility_scope="team")
    )


@pytest.mark.parametrize("status", ["success", "failure"])
def test_unverified_completion_cannot_grant_learning(service, principal, status):
    events = trace(service, principal, "source")
    events[-1].update(status=status, outcome_verified=False)
    service.observe(
        principal,
        ObserveInput(
            run_token="unverified",
            runtime_profile=PROFILE,
            visibility_scope="team",
            event_batch=events,
        ),
    )
    assert service.get_run(principal, "unverified")["status"] == "unverified"
    with pytest.raises(DomainError, match="successful closed trace"):
        service.learn(
            principal,
            LearnInput(
                run_token="unverified",
                completion_proof={
                    "succeeded": True,
                    "evidence_event_ids": ["read"],
                    "summary": "ok",
                },
            ),
        )
    with pytest.raises(DomainError, match="successful completion"):
        compile_trace(
            "task",
            [ExecutionEvent.model_validate(e) for e in events],
            LearningPolicy(),
            "unverified",
            {},
        )


def test_learning_validation_and_cross_agent_portability(service, principal):
    result = learned(
        service,
        principal,
        policy={
            "parameters": {
                "manifest_path": {
                    "definition": {"kind": "string"},
                    "observed_value": "package.json",
                }
            }
        },
    )
    assert recall(service, principal)["no_match"]
    assert result["extracted_steps"][0]["arguments"]["path"] == {"$mm_parameter": "manifest_path"}
    evaluate(service, principal, result["procedure_token"], "first-evaluation")
    assert recall(service, principal)["no_match"]
    evaluate(service, principal, result["procedure_token"], "second-evaluation")
    other_agent = RuntimeProfile(
        agent_name="mcp-agent", bindings={"repository.inspect": "workspace.read"}
    )
    candidate = recall(service, principal, other_agent)["candidates"][0]
    assert candidate["resolved_tools"] == {"repository.inspect": "workspace.read"}
    assert candidate["successes"] == 2 and candidate["confidence"] == 0.75
    procedure = ProcedureDefinition.model_validate(candidate["definition"])
    assert (
        materialize(procedure, {"manifest_path": "pyproject.toml"})[0]["arguments"]["path"]
        == "pyproject.toml"
    )
    with pytest.raises(DomainError, match="Incorrect parameter type"):
        materialize(procedure, {"manifest_path": 22})


def test_hybrid_ranking_no_match_and_capability_filter(service, principal):
    result = validated(service, principal)
    explanation = recall(service, principal)["candidates"][0]["explanation"]
    assert explanation["bm25"] > 0
    assert explanation["exact_match"]  # identical task signature
    assert not recall(service, principal, intent="Inspect manifest in repository")["no_match"]
    assert recall(service, principal, intent="Bake chocolate cake")["no_match"]
    assert recall(service, principal, RuntimeProfile())["no_match"]
    with service.db.transaction() as session:
        revision = session.get(Revision, (result["procedure_token"], 1))
        revision.definition = {**revision.definition, "environment_requirements": {"os": "linux"}}
        session.commit()
    assert recall(service, principal)["no_match"]
    compatible = PROFILE.model_copy(update={"environment": {"os": "linux"}})
    assert not recall(service, principal, compatible)["no_match"]


def test_tenant_and_namespace_isolation(service, principal):
    result = validated(service, principal)
    foreign = service.issue_key("tenant-b", {"team": ["read", "write", "delete"]})
    actor = service.authenticate(foreign["api_key"], rate_limit=False)
    assert recall(service, actor)["no_match"]
    for action in [
        lambda: service.inspect(actor, InspectInput(procedure_token=result["procedure_token"])),
        lambda: service.delete_memory(actor, result["procedure_token"]),
        lambda: service.get_run(actor, "training"),
    ]:
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.status == 404
    private = service.issue_key("tenant-a", {"personal": ["read"]})
    actor = service.authenticate(private["api_key"], rate_limit=False)
    with pytest.raises(DomainError) as error:
        recall(service, actor)
    assert error.value.status == 403
    reader = service.authenticate(
        service.issue_key("tenant-a", {"team": ["read"]})["api_key"], False
    )
    with pytest.raises(DomainError):
        service.learn(
            reader,
            LearnInput(
                run_token="training",
                completion_proof={
                    "succeeded": True,
                    "evidence_event_ids": ["read"],
                    "summary": "ok",
                },
            ),
        )


def test_redaction_before_persistence_and_secret_compilation_refusal(service, principal):
    secret = "sk-sensitivevalue123456789"
    trace(
        service,
        principal,
        arguments={
            "api_key": secret,
            "headers": {"Authorization": f"Bearer {secret}"},
            "message": f"password={secret}",
            "nested": [{"password": "hidden"}],
        },
    )
    with service.db.session() as session:
        stored = json.dumps([e.payload for e in session.scalars(select(Event))])
    assert secret not in stored and "hidden" not in stored
    assert "[REDACTED]" in stored
    with pytest.raises(DomainError, match="redacted credentials"):
        service.learn(
            principal,
            LearnInput(
                run_token="training",
                completion_proof={
                    "succeeded": True,
                    "evidence_event_ids": ["read"],
                    "summary": "ok",
                },
            ),
        )


def test_ordering_partial_errors_and_idempotence(service, principal):
    events = trace(service, principal)
    request = ObserveInput(
        run_token="training", event_batch=events, runtime_profile=PROFILE, visibility_scope="team"
    )
    result = service.observe(principal, request)
    assert result["accepted_event_count"] == 0 and result["duplicate_event_count"] == 3
    events[1]["response"] = "changed"
    result = service.observe(principal, request.model_copy(update={"event_batch": events}))
    assert result["rejected_event_count"] == 1
    assert result["validation_errors"][0]["code"] == "event_conflict"
    bad = [
        events[0],
        {
            "event_id": "bad",
            "sequence": 7,
            "kind": "tool",
            "tool_name": "read_file",
            "capability": "repository.inspect",
        },
    ]
    result = service.observe(
        principal,
        ObserveInput(
            run_token="new", event_batch=bad, runtime_profile=PROFILE, visibility_scope="team"
        ),
    )
    assert result["accepted_event_count"] == 1 and result["rejected_event_count"] == 1


def test_compiler_preserves_dependencies_and_drops_retries():
    events = [
        ExecutionEvent(
            event_id="a",
            sequence=0,
            kind="tool",
            tool_name="read",
            capability="repository.inspect",
            exploratory=True,
        ),
        ExecutionEvent(
            event_id="b",
            sequence=1,
            kind="tool",
            tool_name="read",
            capability="repository.inspect",
            status="failure",
        ),
        ExecutionEvent(
            event_id="c",
            sequence=2,
            kind="tool",
            tool_name="read",
            capability="repository.inspect",
            dependencies=["a"],
            retry_of="b",
        ),
        ExecutionEvent(event_id="d", sequence=3, kind="completion"),
    ]
    definition, warnings = compile_trace(
        "inspect", events, LearningPolicy(), "run", {"summary": "ok"}
    )
    assert [s.step_token for s in definition.steps] == ["a", "c"]
    assert definition.steps[1].dependencies == ["a"] and warnings
    events[2].dependencies = ["b"]
    with pytest.raises(DomainError, match="unavailable evidence"):
        compile_trace("inspect", events, LearningPolicy(), "run", {"summary": "ok"})


def test_conditions_and_side_effect_approval():
    policy = LearningPolicy(
        parameters={"publish": {"definition": {"kind": "boolean"}, "observed_value": True}}
    )
    events = [
        ExecutionEvent(
            event_id="a",
            sequence=0,
            kind="tool",
            tool_name="publish",
            capability="artifact.publish",
            effect="write",
            arguments={"enabled": True},
            condition={"parameter": "publish", "equals": True},
        ),
        ExecutionEvent(event_id="b", sequence=1, kind="completion"),
    ]
    definition, _ = compile_trace("publish", events, policy, "run", {"summary": "ok"})
    assert materialize(definition, {"publish": False}) == []
    assert materialize(definition, {"publish": True})[0]["requires_approval"]


def test_versioning_independent_validation_and_regression(service, principal):
    result = validated(service, principal)
    token = result["procedure_token"]
    revised = learned(service, principal, "new-training", policy={"revise_procedure_token": token})
    assert revised["procedure_version"] == 2
    assert recall(service, principal)["no_match"]
    evaluate(service, principal, token, "old-revision-failure", success=False)
    with service.db.session() as session:
        assert session.get(Procedure, token).lifecycle == "provisional"
    for i in range(2):
        evaluate(service, principal, token, f"new-validation-{i}", version=2)
    assert not recall(service, principal)["no_match"]
    outcome = evaluate(service, principal, token, "new-failure", version=2, success=False)
    assert outcome["regression_warnings"] and recall(service, principal)["no_match"]
    detail = service.inspect(principal, InspectInput(procedure_token=token))
    assert len(detail["version_history"]) == 2 and detail["failures"] == 1


def test_staleness_retention_deletion_and_usage(service, principal):
    result = validated(service, principal)
    usage = service.usage(principal, "team")
    assert usage["observed_savings"]["tool_calls"] == 4
    assert usage["observed_savings"]["tokens"] == 100
    with service.db.transaction() as session:
        session.get(Revision, (result["procedure_token"], 1)).validated_at = utcnow() - timedelta(
            days=91
        )
        session.get(Run, (principal.tenant, "training")).expires_at = utcnow() - timedelta(days=1)
        session.commit()
    assert recall(service, principal)["no_match"]
    with pytest.raises(DomainError) as error:
        service.get_run(principal, "training")
    assert error.value.status == 410
    assert service.purge_expired()["deleted_traces"] == 1
    service.delete_memory(principal, result["procedure_token"])
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Revision)) == 0


def test_training_cannot_validate_itself_and_metrics_are_checked(service, principal):
    result = learned(service, principal)
    request = EvaluateInput(
        procedure_token=result["procedure_token"],
        run_token="training",
        outcome_report={"succeeded": True, "procedure_version": 1, "evidence_event_ids": ["read"]},
        performance_metrics={"observed": {"tool_calls": 1, "duration_ms": 10, "tokens": 50}},
    )
    with pytest.raises(DomainError, match="training run"):
        service.evaluate(principal, request)
    trace(service, principal, "evaluation")
    request.run_token = "evaluation"
    request.performance_metrics.observed.tool_calls = 0
    with pytest.raises(DomainError, match="must match trace"):
        service.evaluate(principal, request)


def test_every_skill_waits_for_a_person_and_agents_cannot_approve(service, principal):
    procedure = learned(service, principal, approve=False)["procedure_token"]
    for index in range(2):
        evaluate(service, principal, procedure, f"check-{index}")
    assert recall(service, principal)["no_match"]  # checked, but not approved
    agent = service.authenticate(
        service.issue_key("tenant-a", {"team": ["read", "write"]})["api_key"], rate_limit=False
    )
    with pytest.raises(DomainError):
        service.approve(agent, procedure)
    service.approve(principal, procedure)
    assert not recall(service, principal)["no_match"]


def test_a_retired_skill_stays_retired(service, principal):
    procedure = validated(service, principal)["procedure_token"]
    service.retire(principal, procedure)
    assert recall(service, principal)["no_match"]
    with pytest.raises(DomainError, match="removed"):
        learned(service, principal, token="relearn")
    with pytest.raises(DomainError, match="removed|retired"):
        trace(service, principal, "revision")
        service.learn(
            principal,
            LearnInput(
                run_token="revision",
                completion_proof={
                    "succeeded": True,
                    "evidence_event_ids": ["read"],
                    "summary": "Manifest inspected",
                },
                learning_policy={"revise_procedure_token": procedure},
            ),
        )


def test_auto_approve_skips_only_the_persons_ok(service, principal):
    from dataclasses import replace

    service.settings = replace(service.settings, auto_approve=True)
    procedure = learned(service, principal, approve=False)["procedure_token"]
    assert recall(service, principal)["no_match"]  # approved, but not yet checked twice
    for index in range(2):
        evaluate(service, principal, procedure, f"check-{index}")
    assert not recall(service, principal)["no_match"]
