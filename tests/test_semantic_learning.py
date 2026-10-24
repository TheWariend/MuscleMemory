from dataclasses import replace

import pytest
from musclememory.artifacts import extract_experience, targets
from musclememory.artifacts import test_command as extracted_test_command
from musclememory.core import DomainError, EvaluateInput, LearnInput
from musclememory.learning import AgentLearner, Assessment, Proposal
from musclememory.storage import Evaluation, LearningDecision, Procedure, Revision
from sqlalchemy import func, select
from test_check_evidence import FIX, edit_session


def source(service, principal, token="source", edits=None):
    edit_session(service, principal, token, edits or [FIX])


def proposal(evidence, **overrides):
    claim = {
        "text": "Merge overrides by assigning each supplied key into the base mapping",
        "evidence_event_ids": ["edit-1"],
    }
    return Proposal.model_validate(
        {
            "action": "create",
            "reason": "Reusable override precedence procedure",
            "title": "Apply configuration overrides consistently",
            "trigger": {**claim, "text": "Use when merging explicit configuration overrides"},
            "steps": [claim],
            "principle": claim,
            "adaptation": {
                **claim,
                "text": "Use the supplied override keys without replacing unrelated base keys",
            },
            **overrides,
        }
    )


def accepted():
    return Assessment(
        accept=True, reason="supported", explanation="The final edit supports this method"
    )


class Learner:
    def __init__(self, handler):
        self.handler, self.calls, self.evidence = handler, 0, None

    def decide(self, evidence):
        self.calls += 1
        self.evidence = evidence
        return self.handler(evidence)


def test_learning_is_a_distinct_cited_method_with_a_scoped_evidence_graph(service, principal):
    source(service, principal)
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    result = service.distill_run(principal, "source")
    assert result["action"] == "learned"
    token = result["procedure_token"]
    with service.db.session() as db:
        revision = db.get(Revision, (token, 1))
        assert revision.successes == 0 and revision.validation_status == "provisional"
        assert revision.definition["provenance"]["learning"]["proposal"]["steps"][0][
            "text"
        ].startswith("Merge")
    graph = service.memory_graph(principal, "team", token)
    assert {n["kind"] for n in graph["nodes"]} == {"skill", "claim", "run", "event"}
    assert {e["relation"] for e in graph["edges"]} == {"derived_from", "supported_by"}
    listed = service.listing(principal, "team", "procedures")["items"][0]
    assert listed["learned_method"] == {
        "trigger": proposal({}).trigger.text,
        "principle": proposal({}).principle.text,
    }
    assert listed["admission"] is None
    assert service.distill_run(principal, "source")["cached"]
    assert service.learner.calls == 1
    assert len(service.memory_graph(principal, "team", token)["edges"]) == len(graph["edges"])


def test_rejection_is_durable_and_does_not_create_a_skill(service, principal):
    source(service, principal)
    service.learner = Learner(
        lambda e: (
            proposal(e),
            Assessment(
                accept=False,
                reason="insufficient_abstraction",
                explanation="Only describes a single incident",
            ),
        )
    )
    assert service.distill_run(principal, "source")["action"] == "rejected"
    assert service.listing(principal, "team", "procedures")["items"] == []
    assert service.distill_run(principal, "source")["cached"]
    assert service.learner.calls == 1


def test_model_failure_remains_retryable_and_distinct_from_rejection(service, principal):
    source(service, principal)

    def fail(evidence):
        raise DomainError("learning_agent_failed", "Temporary model outage")

    service.learner = Learner(fail)
    assert service.distill_run(principal, "source")["action"] == "deferred"
    service.learner.handler = lambda e: (proposal(e), accepted())
    assert service.distill_run(principal, "source")["action"] == "learned"
    with service.db.session() as db:
        decision = db.scalar(select(LearningDecision))
        assert decision.status == "accepted" and decision.attempts == 2


def test_invalid_citations_cannot_be_admitted_even_with_model_acceptance(service, principal):
    source(service, principal)
    service.learner = Learner(
        lambda e: (
            proposal(
                e,
                steps=[
                    {
                        "text": "A fabricated action the source never executed",
                        "evidence_event_ids": ["unrelated-event"],
                    }
                ],
            ),
            accepted(),
        )
    )
    result = service.distill_run(principal, "source")
    assert result == {"action": "deferred", "reason": "learning_citation"}
    assert not service.listing(principal, "team", "procedures")["items"]


def test_dry_run_writes_neither_decisions_nor_skills(service, principal):
    source(service, principal)
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    assert service.distill_run(principal, "source", dry_run=True)["action"] == "create"
    with service.db.session() as db:
        assert db.scalar(select(func.count()).select_from(LearningDecision)) == 0
        assert db.scalar(select(func.count()).select_from(Procedure)) == 0


def test_refinement_versions_the_method_and_resets_validation(service, principal):
    source(service, principal)
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    first = service.distill_run(principal, "source")
    source(service, principal, "refinement")
    service.learner.handler = lambda e: (
        proposal(e, action="refine", target=first["procedure_token"], target_version=1),
        accepted(),
    )
    result = service.distill_run(principal, "refinement")
    assert result["action"] == "learned", result
    assert (
        result["procedure_token"] == first["procedure_token"] and result["procedure_version"] == 2
    )
    with service.db.session() as db:
        assert db.get(Revision, (result["procedure_token"], 1)) is not None
        assert db.get(Revision, (result["procedure_token"], 2)).successes == 0
    assert "refines" in {e["relation"] for e in service.memory_graph(principal, "team")["edges"]}


def test_semantic_corroboration_does_not_fabricate_an_execution_check(service, principal):
    source(service, principal)
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    first = service.distill_run(principal, "source")
    source(service, principal, "related")
    service.learner.handler = lambda e: (
        proposal(e, action="corroborate", target=first["procedure_token"], target_version=1),
        accepted(),
    )
    result = service.distill_run(principal, "related")
    assert result["action"] == "corroborated", result
    with service.db.session() as db:
        assert db.get(Revision, (first["procedure_token"], 1)).successes == 0
        assert db.scalar(select(func.count()).select_from(Evaluation)) == 0


def test_ready_semantic_export_teaches_the_method_instead_of_replaying_the_patch(
    service, principal, tmp_path, monkeypatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    source(service, principal)
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    token = service.distill_run(principal, "source")["procedure_token"]
    with pytest.raises(DomainError) as error:
        service.export_skill(principal, token, "codex")
    assert error.value.code == "not_ready"
    for index in range(2):
        run = f"verified-reuse-{index}"
        source(service, principal, run)
        assert service.auto_learn(principal, run, hint=token)["action"] == "checked"
    service.approve(principal, token)
    result = service.export_skill(principal, token, "codex")
    text = (tmp_path / "skills" / result["name"] / "SKILL.md").read_text()
    assert "## Method" in text and "## Adapt to the current task" in text
    assert "without replacing unrelated base keys" in text
    assert "## Original request" not in text and "```diff" not in text


def test_unknown_graph_roots_and_foreign_scopes_are_not_exposed(service, principal):
    source(service, principal)
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    token = service.distill_run(principal, "source")["procedure_token"]
    foreign = service.authenticate(service.issue_key("other", {"team": ["read"]})["api_key"], False)
    assert service.memory_graph(foreign, "team")["nodes"] == []
    with pytest.raises(DomainError):
        service.memory_graph(foreign, "team", token)


def test_unconfigured_learning_preserves_source_for_retry(service, principal):
    source(service, principal)
    service.learner = AgentLearner(
        replace(service.settings, learning_agent=None, preferences_path=None)
    )
    assert service.distill_run(principal, "source") == {
        "action": "deferred",
        "reason": "learning_unconfigured",
    }
    assert service.get_run(principal, "source")["status"] == "success"


def test_final_literal_change_summary_excludes_reverted_instrumentation():
    steps = [
        {
            "capability": "filesystem.edit",
            "effect": "write",
            "step_token": str(i),
            "arguments": {"file_path": "capture.py", "old_string": before, "new_string": after},
        }
        for i, (before, after) in enumerate(
            [
                ("", "debug_log(event)"),
                ("old_logic()", "fixed_logic()"),
                ("debug_log(event)", ""),
            ]
        )
    ]
    assert extract_experience(steps)["edits"] == [
        {"file": "capture.py", "before": "old_logic()", "after": "fixed_logic()"},
    ]


def test_embedded_program_text_is_neither_a_path_nor_a_passing_test():
    script = "python3 - <<'PY'\nif found / 0:break\npytest tests/fake.py\nPY\npytest tests/real.py"
    reads, writes = targets({"command": script}, "destructive", "shell.execute")
    assert reads == ["tests/real.py"] and writes == []
    assert extracted_test_command(script) == "pytest tests/real.py"
    assert extracted_test_command("python3 - <<'PY'\npytest tests/fake.py\nPY") is None
    assert targets({"command": 'rg "code.with.extension" src/real.py'}, "read", "shell.execute")[
        0
    ] == ["src/real.py"]


def test_combined_edit_and_verify_cannot_fall_back_to_overlapping_files(service, principal):
    from musclememory.artifacts import WORKSPACE_CHANGES
    from musclememory.core import ObserveInput

    runtime = {"bindings": {"shell.execute": "Bash"}}
    service.observe(
        principal,
        ObserveInput(
            run_token="combined",
            visibility_scope="team",
            runtime_profile=runtime,
            event_batch=[
                {
                    "event_id": "intent",
                    "sequence": 0,
                    "kind": "intent",
                    "intent_text": "Update contracts",
                },
                {
                    "event_id": "edit-test",
                    "sequence": 1,
                    "kind": "tool",
                    "tool_name": "Bash",
                    "capability": "shell.execute",
                    "effect": "write",
                    "arguments": {
                        "command": "python3 update.py && npm run test:contracts",
                        WORKSPACE_CHANGES: ["app/settings.py"],
                    },
                },
                {"event_id": "done", "sequence": 2, "kind": "completion"},
            ],
        ),
    )
    token = service.learn(
        principal,
        LearnInput(
            run_token="combined",
            completion_proof={
                "succeeded": True,
                "evidence_event_ids": ["edit-test"],
                "summary": "Contracts passed",
            },
        ),
    )["procedure_token"]
    edit_session(service, principal, "wrong-suite", [FIX], test="npm run test:ota")
    with pytest.raises(DomainError, match="required passing tests"):
        service.evaluate(
            principal,
            EvaluateInput(
                procedure_token=token,
                run_token="wrong-suite",
                outcome_report={
                    "succeeded": True,
                    "procedure_version": 1,
                    "evidence_event_ids": ["test"],
                },
                performance_metrics={"observed": {"tool_calls": 2, "duration_ms": 0}},
            ),
        )
