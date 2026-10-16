"""Learning from failures: contrasts, avoidance/recovery notes and validation gates."""

import json
from dataclasses import replace

import httpx
import pytest
from musclememory.core import (
    DomainError,
    EvaluateInput,
    InspectInput,
    LearnInput,
    ObserveInput,
    RecallInput,
    RuntimeProfile,
)
from musclememory.storage import Contrast, Procedure, Revision, Run
from sqlalchemy import func, select

PROFILE = RuntimeProfile(
    bindings={"repository.inspect": "read", "file.edit": "edit"},
    permissions=["workspace.read", "workspace.write"],
    environment={"os": "linux"},
)


def record(
    service,
    principal,
    token,
    *,
    intent="Modify the configuration",
    path="original.json",
    success=True,
    complete=True,
    namespace="team",
    extra=None,
    profile=PROFILE,
):
    events = [
        {"event_id": "intent", "sequence": 0, "kind": "intent", "intent_text": intent},
        {
            "event_id": "read",
            "sequence": 1,
            "kind": "tool",
            "capability": "repository.inspect",
            "tool_name": profile.bindings["repository.inspect"],
            "arguments": {"path": path},
            "permission": "workspace.read",
            "duration_ms": 1,
        },
        {
            "event_id": "edit",
            "sequence": 2,
            "kind": "tool",
            "capability": "file.edit",
            "tool_name": profile.bindings["file.edit"],
            "arguments": {"path": path, **(extra or {})},
            "dependencies": ["read"],
            "permission": "workspace.write",
            "effect": "write",
            "status": "success" if success else "failure",
            "duration_ms": 1,
        },
    ]
    if complete:
        events.append(
            {
                "event_id": "done",
                "sequence": 3,
                "kind": "completion",
                "status": "success" if success else "failure",
            }
        )
    observed = service.observe(
        principal,
        ObserveInput(
            run_token=token, event_batch=events, runtime_profile=profile, visibility_scope=namespace
        ),
    )
    assert observed["accepted_event_count"] == len(events)
    return events


def train(service, principal, token="training", *, extra=None, conditions=None):
    record(service, principal, token, extra=extra)
    result = service.learn(
        principal,
        LearnInput(
            run_token=token,
            completion_proof={
                "succeeded": True,
                "evidence_event_ids": ["edit"],
                "summary": "File checked",
            },
            learning_policy={
                "parameters": {
                    "path": {"definition": {"kind": "string"}, "observed_value": "original.json"}
                },
                "environment_requirements": {"os": "linux"},
            },
        ),
    )
    service.approve(principal, result["procedure_token"])
    for index in range(2):
        run = f"{token}-evaluation-{index}"
        record(service, principal, run, extra=extra)
        service.evaluate(
            principal,
            EvaluateInput(
                procedure_token=result["procedure_token"],
                run_token=run,
                outcome_report={
                    "succeeded": True,
                    "procedure_version": 1,
                    "evidence_event_ids": ["edit"],
                },
                performance_metrics={"observed": {"tool_calls": 2, "duration_ms": 2}},
            ),
        )
    if conditions:
        with service.db.transaction() as session:
            revision = session.get(Revision, (result["procedure_token"], 1))
            revision.definition = {**revision.definition, "applicability_conditions": conditions}
            session.commit()
    return result


def recall(service, principal, intent="Modify the configuration", profile=PROFILE):
    return service.recall(
        principal, RecallInput(intent_text=intent, runtime_profile=profile, visibility_scope="team")
    )


@pytest.mark.parametrize(
    "condition",
    [
        "environment:missing",
        "permission:root",
        "capability:other.tool",
        "state-capability:other.tool",
        "free form rule",
    ],
)
def test_boundary_conditions_fail_closed(service, principal, condition):
    train(service, principal, conditions=[condition])
    assert recall(service, principal)["no_match"]


def test_contrast_diff_and_critical_step_preserve_failed_run(service, principal):
    record(service, principal, "failed", success=False)
    learned = train(service, principal, extra={"format": "json"})
    detail = service.inspect(principal, InspectInput(procedure_token=learned["procedure_token"]))
    assert len(detail["contrasts"]) == 1
    diff = detail["contrasts"][0]["step_diff"]
    assert [step["capability"] for step in diff["only_success"]] == ["file.edit"]
    assert [step["capability"] for step in diff["only_failure"]] == ["file.edit"]
    assert diff["argument_shape_differences"][0]["successful_shape"] == sorted(
        [
            "$:object",
            "$/format:string",
            "$/path:string",
        ]
    )
    assert detail["definition"]["steps"][1]["critical"]
    assert not detail["definition"]["steps"][0]["critical"]
    assert detail["definition"]["avoidance_rules"] and detail["definition"]["recovery_notes"]
    with service.db.session() as session:
        assert session.get(Run, (principal.tenant, "failed")).status == "failure"


def test_contrast_isolation_secret_omission_and_idempotency(service, principal):
    other = service.authenticate(
        service.issue_key("foreign", {"team": ["read", "write"]})["api_key"], False
    )
    record(service, other, "foreign-failure", success=False)
    private = service.authenticate(
        service.issue_key(principal.tenant, {"private": ["read", "write"]})["api_key"], False
    )
    record(service, private, "private-failure", success=False, namespace="private")
    record(service, principal, "secret-failure", success=False, extra={"api_key": "never-store-me"})
    record(
        service, principal, "omitted-failure", success=False, extra={"$mm_capture_omitted": True}
    )
    result = train(service, principal)
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Contrast)) == 0
        definition = session.get(Revision, (result["procedure_token"], 1)).definition
    assert "never-store-me" not in json.dumps(definition)
    assert not service.inspect(principal, InspectInput(procedure_token=result["procedure_token"]))[
        "definition"
    ]["steps"][1]["critical"]
    assert recall(service, other)["no_match"]


def test_optional_boundary_refinement_called_once_and_retains_required_conditions(
    service, principal, monkeypatch
):
    calls = []
    service.settings = replace(
        service.settings, boundary_llm_url="https://provider.example/boundaries"
    )

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "applicability_conditions": [],
                "risk_cues": [],
                "avoidance_rules": ["Check the file shape before editing."],
                "recovery_notes": [],
            },
        )

    monkeypatch.setattr(httpx, "post", post)
    result = train(service, principal)
    assert len(calls) == 1 and "original.json" not in json.dumps(calls)
    candidate = recall(service, principal)["candidates"][0]
    assert candidate["avoidance_rules"] == ["Check the file shape before editing."]
    assert "environment:os" in candidate["definition"]["applicability_conditions"]
    assert candidate["procedure_token"] == result["procedure_token"] and len(calls) == 1


def test_refinement_failure_uses_deterministic_boundaries(service, principal, monkeypatch):
    service.settings = replace(
        service.settings, boundary_llm_url="https://provider.example/boundaries"
    )
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: httpx.Response(
            200, request=httpx.Request("POST", url), json={"risk_cues": ["password=private"]}
        ),
    )
    result = train(service, principal)
    assert any("deterministic fields retained" in note for note in result["compilation_warnings"])
    assert "password=private" not in json.dumps(recall(service, principal))
    with pytest.raises(ValueError):
        replace(service.settings, boundary_llm_url="http://insecure.example")


def test_validation_and_regression_gate(service, principal):
    record(service, principal, "source")
    learned = service.learn(
        principal,
        LearnInput(
            run_token="source",
            completion_proof={
                "succeeded": True,
                "evidence_event_ids": ["edit"],
                "summary": "Checked",
            },
        ),
    )
    service.approve(principal, learned["procedure_token"])
    assert recall(service, principal)["no_match"]
    for index in range(3):
        token, success = f"independent-{index}", index < 2
        record(service, principal, token, success=success)
        service.evaluate(
            principal,
            EvaluateInput(
                procedure_token=learned["procedure_token"],
                run_token=token,
                outcome_report={
                    "succeeded": success,
                    "procedure_version": 1,
                    "evidence_event_ids": ["edit"] if success else [],
                },
                performance_metrics={"observed": {"tool_calls": 2, "duration_ms": 2}},
            ),
        )
        assert recall(service, principal)["no_match"] == (index != 1)


def test_a_deleted_skill_is_not_learned_again(service, principal):
    record(service, principal, "failure", success=False)
    learned = train(service, principal, extra={"format": "json"})
    request = LearnInput(
        run_token="training",
        completion_proof={
            "succeeded": True,
            "evidence_event_ids": ["edit"],
            "summary": "File checked",
        },
        learning_policy={
            "parameters": {
                "path": {"definition": {"kind": "string"}, "observed_value": "original.json"}
            },
            "environment_requirements": {"os": "linux"},
        },
    )
    assert service.learn(principal, request) == learned
    service.delete_memory(principal, learned["procedure_token"])
    with pytest.raises(DomainError, match="removed"):
        service.learn(principal, request)
    # The same work done again in a new session doesn't bring it back either.
    record(service, principal, "again", extra={"format": "json"})
    with pytest.raises(DomainError, match="removed"):
        service.learn(principal, request.model_copy(update={"run_token": "again"}))
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Contrast)) == 1
        assert session.scalar(select(func.count()).select_from(Procedure)) == 0


def test_watch_out_names_the_command_that_worked():
    from types import SimpleNamespace

    from musclememory.boundaries import derive_boundaries
    from musclememory.core import ExecutionEvent

    def shell(sequence, command, status="success"):
        return ExecutionEvent.model_validate(
            {
                "event_id": f"e{sequence}",
                "sequence": sequence,
                "kind": "tool",
                "capability": "shell.execute",
                "tool_name": "Bash",
                "arguments": {"command": command},
                "status": status,
                "effect": "destructive",
            }
        )

    edit = ExecutionEvent.model_validate(
        {
            "event_id": "e3",
            "sequence": 3,
            "kind": "tool",
            "capability": "filesystem.patch",
            "tool_name": "apply_patch",
            "arguments": {
                "command": "*** Begin Patch\n*** Update File: app/exports.py\n*** End Patch"
            },
            "effect": "write",
        }
    )
    events = [
        shell(1, "sed -n '1,200p' app/exports.py"),
        # Fails before the fix and passes after it: the test caught the bug, no note.
        shell(2, "python3 -m unittest discover -s tests", "failure"),
        edit,
        shell(4, "python3 -m unittest", "failure"),
        shell(5, "python3 -m unittest discover", "failure"),
        shell(6, "python3 -m unittest discover -s tests"),
        shell(7, "make lint", "failure"),
    ]
    definition = SimpleNamespace(
        required_capabilities=["shell.execute"],
        required_permissions=[],
        environment_requirements={},
        step_graph={"nodes": ["e1", "e3", "e6"]},
        steps=[],
    )
    derive_boundaries(definition, events, [])
    assert definition.avoidance_rules == [
        "`python3 -m unittest` and `python3 -m unittest discover` failed here; "
        "`python3 -m unittest discover -s tests` worked.",
        "Before running `make lint` again, check why it failed last time.",
    ]
    # Reading a file is not described as changing anything.
    assert not any("sed -n" in cue and "changes things" in cue for cue in definition.risk_cues)
