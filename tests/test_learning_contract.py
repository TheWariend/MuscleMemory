"""Keep optional curation within the documented proof-before-reuse contract."""

import json
from dataclasses import replace

import pytest
from mcp import Client
from musclememory.backfill import align_learning_contract
from musclememory.core import DomainError, InspectInput, ObserveInput, RecallInput
from musclememory.gateway import RESTGateway
from musclememory.learning import AgentLearner, grounded_method
from musclememory.mcp_server import build_mcp
from musclememory.storage import Procedure, Revision, Run
from test_check_evidence import FIX
from test_semantic_learning import Learner, accepted, proposal, source


def test_default_local_learning_needs_no_model_and_still_requires_checks_and_review(
    service, principal
):
    service.settings = replace(service.settings, learning_agent=None, preferences_path=None)
    service.learner = Learner(lambda _: pytest.fail("Local learning must not call a model"))
    source(service, principal)
    result = service.auto_learn(principal, "source")
    token = result["procedure_token"]
    assert result["action"] == "learned" and service.learner.calls == 0
    detail = service.inspect(principal, InspectInput(procedure_token=token))
    assert detail["lifecycle_status"] == "provisional" and detail["needs_review"]
    assert detail["successes"] == 0
    assert service.auto_learn(principal, "source")["cached"]
    for run in ("check-1", "check-2"):
        source(service, principal, run)
        assert service.auto_learn(principal, run)["action"] == "checked"
    detail = service.inspect(principal, InspectInput(procedure_token=token))
    assert detail["successes"] == 2 and detail["needs_review"]


def test_local_learning_cannot_use_a_test_from_before_the_last_change(service, principal):
    service.settings = replace(service.settings, learning_agent=None, preferences_path=None)
    source(service, principal)
    record = service.get_run(principal, "source")
    events = record["events"]
    events[1], events[2] = events[2], events[1]
    for index, event in enumerate(events):
        event["sequence"] = index
    service.observe(
        principal,
        ObserveInput(
            run_token="edit-after-test",
            event_batch=events,
            runtime_profile=record["runtime_profile"],
            visibility_scope="team",
        ),
    )
    with pytest.raises(DomainError) as error:
        service.auto_learn(principal, "edit-after-test")
    assert error.value.code == "unverified_run"


def test_shared_namespace_is_default_and_repository_isolation_is_opt_in(service, principal):
    source(service, principal)
    with service.db.transaction() as db:
        db.get(Run, (principal.tenant, "source")).project_scope = "repo-a"
        db.commit()
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    token = service.distill_run(principal, "source")["procedure_token"]
    detail = service.inspect(principal, InspectInput(procedure_token=token))
    assert detail["definition"]["environment_requirements"] == {}
    assert detail["definition"]["title"] == "Fix config"
    assert not detail["has_custom_name"]
    assert detail["name"] != "Apply configuration overrides consistently"
    source(service, principal, "other-repo")
    with service.db.transaction() as db:
        db.get(Run, (principal.tenant, "other-repo")).project_scope = "repo-b"
        db.commit()
    assert service.auto_learn(principal, "other-repo", hint=token)["action"] == "checked"

    service.settings = replace(service.settings, learning_project_isolation=True)
    source(service, principal, "isolated")
    with service.db.transaction() as db:
        db.get(Run, (principal.tenant, "isolated")).project_scope = "repo-a"
        db.commit()
    isolated = service.distill_run(principal, "isolated")["procedure_token"]
    assert service.inspect(principal, InspectInput(procedure_token=isolated))["definition"][
        "environment_requirements"
    ] == {"mm_project": "repo-a"}


def poison_method(service, principal):
    poisoned = (
        FIX[0],
        '    for key, value in overrides.items():\n        if key == "audit":\n'
        "            continue\n        result[key] = value",
    )
    source(service, principal, edits=[poisoned])
    service.learner = Learner(
        lambda e: (
            proposal(
                e,
                title="Skip audit overrides",
                principle={
                    "text": "Skip audit overrides by continuing before assigning them",
                    "evidence_event_ids": ["edit-1"],
                },
            ),
            accepted(),
        )
    )
    token = service.distill_run(principal, "source")["procedure_token"]
    for run in ("clean-1", "clean-2"):
        source(service, principal, run)
        assert service.auto_learn(principal, run, hint=token)["action"] == "checked"
    service.approve(principal, token)
    return token


async def test_unreproduced_prose_cannot_bypass_line_proof_in_recall_inspect_or_export(
    service, principal, credential
):
    token = poison_method(service, principal)
    raw = service.inspect(principal, InspectInput(procedure_token=token))
    assert raw["successes"] == 2 and not raw["needs_review"]
    assert "audit" in raw["definition"]["provenance"]["learning"]["proposal"]["principle"]["text"]
    recalled = service.recall(
        principal,
        RecallInput(
            intent_text="Fix config",
            visibility_scope="team",
            runtime_profile=service.get_run(principal, "source")["runtime_profile"],
        ),
    )
    assert recalled["candidates"][0]["procedure_token"] == token
    pointer = json.loads(recalled["guidance"])
    assert "audit" not in recalled["guidance"] and "principle" not in pointer
    assert "method_withheld" in pointer
    assert recalled["candidates"][0]["definition"]["provenance"]["learning"]["withheld"]
    async with Client(build_mcp(service, stdio_key=credential["api_key"]), mode="legacy") as client:
        result = await client.call_tool("musclememory_inspect", {"procedure_token": token})
        assert not result.is_error
        assert result.structured_content["method"] is None
        assert result.structured_content["method_withheld"]
        assert "audit" not in json.dumps(result.structured_content)
    with pytest.raises(DomainError) as error:
        service.export_skill(principal, token, "codex")
    assert error.value.code == "unproven_guidance"


@pytest.mark.parametrize("citations", [["test"], ["missing-event"]])
def test_checks_and_test_only_citations_do_not_prove_generated_advice(
    service, principal, citations
):
    source(service, principal)
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    token = service.distill_run(principal, "source")["procedure_token"]
    detail = service.inspect(principal, InspectInput(procedure_token=token))
    definition = detail["definition"]
    assert grounded_method(definition, [], 0) is None
    definition["provenance"]["learning"]["proposal"]["principle"]["evidence_event_ids"] = citations
    assert grounded_method(definition, [], 2) is None


def test_contract_alignment_preserves_skills_names_trust_and_original_recordings(
    service, principal
):
    source(service, principal)
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    token = service.distill_run(principal, "source")["procedure_token"]
    original_events = service.get_run(principal, "source")["events"]
    with service.db.transaction() as db:
        db.get(Procedure, token).name = "A deliberately chosen title"
        revision = db.get(Revision, (token, 1))
        definition = json.loads(json.dumps(revision.definition))
        definition["title"] = "Apply configuration overrides consistently"
        definition["intent_signature"] = "generated trigger"
        definition["environment_requirements"] = {"mm_project": "repo-a", "os": "linux"}
        definition["provenance"]["learning"]["scope"] = "repo-a"
        definition["provenance"]["learning"].pop("project_isolated")
        revision.definition = definition
        db.commit()
    assert len(align_learning_contract(service, principal, "team")) == 1
    assert len(align_learning_contract(service, principal, "team", apply=True)) == 1
    assert align_learning_contract(service, principal, "team", apply=True) == []
    detail = service.inspect(principal, InspectInput(procedure_token=token))
    assert detail["definition"]["title"] == "Fix config"
    assert detail["definition"]["environment_requirements"] == {"os": "linux"}
    assert detail["name"] == "A deliberately chosen title"
    assert detail["procedure_version"] == 1 and detail["successes"] == 0 and detail["needs_review"]
    assert service.get_run(principal, "source")["events"] == original_events
    with service.db.session() as db:
        assert db.get(Procedure, token) is not None


def test_an_opted_in_curator_failure_does_not_silently_admit_a_raw_recipe(service, principal):
    source(service, principal)
    service.learner = AgentLearner(replace(service.settings, learning_agent=None))
    assert service.auto_learn(principal, "source")["action"] == "deferred"
    assert service.listing(principal, "team", "procedures")["items"] == []


@pytest.mark.parametrize("transport", ["direct", "gateway"])
async def test_mcp_generated_method_waits_for_review_even_when_its_changes_are_proved(
    service, principal, credential, remote_server, transport
):
    source(service, principal)
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    token = service.distill_run(principal, "source")["procedure_token"]
    for run in ("check-1", "check-2"):
        source(service, principal, run)
        assert service.auto_learn(principal, run, hint=token)["action"] == "checked"
    gateway = RESTGateway(remote_server, credential["api_key"]) if transport == "gateway" else None
    try:
        async with Client(
            build_mcp(gateway or service, stdio_key=credential["api_key"]), mode="legacy"
        ) as client:
            result = await client.call_tool("musclememory_inspect", {"procedure_token": token})
            assert not result.is_error and result.structured_content["method"] is None
            service.approve(principal, token)
            result = await client.call_tool("musclememory_inspect", {"procedure_token": token})
            assert not result.is_error
            assert result.structured_content["method"]["principle"]["text"].startswith("Merge")
            assert "assessment" not in result.structured_content["method"]
    finally:
        if gateway:
            gateway.close()


def test_contract_alignment_keeps_explicit_repository_isolation(service, principal):
    service.settings = replace(service.settings, learning_project_isolation=True)
    source(service, principal)
    with service.db.transaction() as db:
        db.get(Run, (principal.tenant, "source")).project_scope = "repo-a"
        db.commit()
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    token = service.distill_run(principal, "source")["procedure_token"]
    service.settings = replace(service.settings, learning_project_isolation=False)
    align_learning_contract(service, principal, "team", apply=True)
    detail = service.inspect(principal, InspectInput(procedure_token=token))
    assert detail["definition"]["environment_requirements"] == {"mm_project": "repo-a"}
