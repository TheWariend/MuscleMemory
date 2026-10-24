"""Naming skills with the agent you already use: off by default, never trusted blindly."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import learned
from musclememory import titles
from musclememory.backfill import align_skill_names
from musclememory.core import DomainError, InspectInput
from musclememory.storage import Procedure, Revision
from test_semantic_learning import Learner, accepted, proposal, source


@pytest.fixture
def agent(service, tmp_path, monkeypatch):
    """A stand-in for Claude Code and Codex that records how it was run."""
    service.settings = replace(service.settings, preferences_path=str(tmp_path / "prefs.json"))
    runs, replies = [], ["Read the repository manifest"]

    def run(command, **kwargs):
        runs.append({"command": command, **kwargs})
        reply = replies[0]
        if "--output-last-message" in command:  # Codex writes its answer to a file
            Path(command[command.index("--output-last-message") + 1]).write_text(reply)
            reply = "codex progress output"
        return subprocess.CompletedProcess(command, 0, stdout=reply, stderr="")

    monkeypatch.setattr(titles.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(titles.subprocess, "run", run)
    return runs, replies


def name(service, principal, token):
    return service.inspect(principal, InspectInput(procedure_token=token))["name"]


@pytest.mark.parametrize("choice", ["claude", "codex"])
def test_the_chosen_agent_names_new_skills(service, principal, agent, choice):
    runs, _ = agent
    assert service.preferences(principal)["skill_titles"] == "off"
    assert service.set_preferences(principal, choice)["skill_titles"] == choice
    token = learned(service, principal)["procedure_token"]
    service.titling.join()
    assert name(service, principal, token) == "Read the repository manifest"
    assert service.listing(principal, "team", "procedures")["items"][0]["name"] == (
        "Read the repository manifest"
    )
    (run,) = runs
    command = run["command"]
    assert command[0] == f"/usr/local/bin/{choice}" and Path(run["cwd"]).name != "musclememory"
    if choice == "claude":
        assert command[command.index("--tools") + 1] == "" and "--no-session-persistence" in command
    else:
        assert {"--ephemeral", "read-only"} <= set(command)
    prompt = next(part for part in command if "Evidence: " in part)
    evidence = json.loads(prompt.split("Evidence: ", 1)[1])
    assert evidence == {"request": "Inspect repository manifest", "files_changed": [], "test": ""}
    # Search and agents keep the title learned from the request.
    detail = service.inspect(principal, InspectInput(procedure_token=token))
    assert detail["definition"]["title"] == "Inspect repository manifest"


@pytest.mark.parametrize("reply", ["password=hunter2-secret", "x" * 81, "ok", ""])
def test_unusable_replies_keep_the_built_name(service, principal, agent, reply):
    runs, replies = agent
    replies[0] = reply
    service.set_preferences(principal, "claude")
    token = learned(service, principal)["procedure_token"]
    service.titling.join()
    assert len(runs) == 1 and name(service, principal, token) == ""


def test_nothing_runs_while_naming_is_off(service, principal, agent):
    runs, _ = agent
    learned(service, principal)
    assert runs == [] and not hasattr(service, "titling")
    with pytest.raises(DomainError):
        service.set_preferences(principal, "gemini")


@pytest.mark.parametrize("choice", ["off", "claude", "codex"])
def test_curation_cannot_bypass_the_separate_naming_choice(service, principal, agent, choice):
    runs, replies = agent
    replies[0] = "Merge configuration overrides safely"
    service.set_preferences(principal, choice, "codex")
    source(service, principal)
    service.learner = Learner(lambda evidence: (proposal(evidence), accepted()))
    token = service.distill_run(principal, "source")["procedure_token"]
    if choice != "off":
        service.titling.join()
    detail = service.inspect(principal, InspectInput(procedure_token=token))
    assert detail["definition"]["title"] == "Fix config"
    assert detail["definition"]["provenance"]["learning"]["proposal"]["title"] == (
        "Apply configuration overrides consistently"
    )
    if choice == "off":
        assert runs == [] and not detail["has_custom_name"]
        assert detail["name"]  # A built label must not hide the LLM naming button.
        assert detail["name"] != "Apply configuration overrides consistently"
        assert service.name_skill(principal, token, "codex")["name"] == replies[0]
        assert service.inspect(principal, InspectInput(procedure_token=token))["has_custom_name"]
    else:
        assert len(runs) == 1 and runs[0]["command"][0] == f"/usr/local/bin/{choice}"
        assert detail["has_custom_name"] and detail["name"] == replies[0]


def test_turning_off_naming_during_a_request_does_not_publish_the_pending_title(
    service, principal, agent, monkeypatch
):
    service.set_preferences(principal, "codex")

    def finish_after_turning_off(*_):
        titles.choose_agent(service.settings, None)
        return "A title that arrived after opting out"

    monkeypatch.setattr("musclememory.service.ask_agent", finish_after_turning_off)
    token = learned(service, principal)["procedure_token"]
    service.titling.join()
    assert not service.inspect(principal, InspectInput(procedure_token=token))["has_custom_name"]


def test_name_repair_clears_only_the_curator_bypass_and_preserves_evidence(
    service, principal, agent
):
    service.learner = Learner(lambda evidence: (proposal(evidence), accepted()))
    tokens = {}
    originals = {}
    for kind in ("bypass", "manual_same", "manual_different", "separate", "auto_named"):
        source(service, principal, kind)
        token = service.distill_run(principal, kind)["procedure_token"]
        tokens[kind] = token
        with service.db.transaction() as db:
            revision = db.get(Revision, (token, 1))
            definition = json.loads(json.dumps(revision.definition))
            if kind != "separate":
                definition["provenance"]["learning"].pop("naming_policy")
            revision.definition = definition
            originals[token] = definition
            db.get(Procedure, token).name = (
                "My chosen title" if kind == "manual_different" else proposal({}).title
            )
            if kind in {"manual_same", "auto_named"}:
                service.audit(
                    db, principal, "team", "name" if kind == "manual_same" else "name_auto", token
                )
            db.commit()
    assert [r["procedure_token"] for r in align_skill_names(service, principal, "team")] == [
        tokens["bypass"]
    ]
    assert len(align_skill_names(service, principal, "team", apply=True)) == 1
    assert align_skill_names(service, principal, "team", apply=True) == []
    for kind, token in tokens.items():
        detail = service.inspect(principal, InspectInput(procedure_token=token))
        assert detail["has_custom_name"] == (kind != "bypass")
        assert detail["definition"] == originals[token]
        assert detail["successes"] == 0 and detail["needs_review"]
        assert service.get_run(principal, kind)["events"]


def test_the_button_names_an_existing_skill_with_either_agent(
    service, principal, agent, monkeypatch
):
    token = learned(service, principal)["procedure_token"]
    assert service.name_skill(principal, token, "codex")["name"] == "Read the repository manifest"
    assert name(service, principal, token) == "Read the repository manifest"
    monkeypatch.setattr(titles.shutil, "which", lambda name: None)
    with pytest.raises(DomainError, match="isn't installed"):
        service.name_skill(principal, token, "claude")


def test_settings_are_not_saved_without_a_local_preferences_file(http):
    assert http.get("/v1/preferences").json()["skill_titles"] == "off"
    assert http.put("/v1/preferences", json={"skill_titles": "claude"}).status_code == 403


@pytest.fixture
def hosted_service(service, agent):
    service.settings = replace(
        service.settings,
        deployment_mode="hosted",
        learning_agent=None,
        database_url="postgresql+psycopg://memory:example@postgres/musclememory",
        public_url="https://memory.example",
        allowed_hosts=("memory.example",),
        allowed_origins=("https://memory.example",),
    )
    return service


def test_hosted_preferences_cannot_read_or_change_local_settings(hosted_service, http):
    path = Path(hosted_service.settings.preferences_path)
    saved = '{"skill_titles": "claude"}\n'
    path.write_text(saved)
    headers = {"Host": "memory.example"}

    assert http.get("/v1/preferences", headers=headers).json()["skill_titles"] == "off"
    for choice in ("off", "claude", "codex"):
        response = http.put("/v1/preferences", json={"skill_titles": choice}, headers=headers)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "local_only"
        assert path.read_text() == saved

    response = http.post("/v1/procedures/existing/name?agent=claude", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "local_only"


@pytest.mark.parametrize("choice", ["claude", "codex"])
def test_hosted_learning_ignores_local_agent_preferences(hosted_service, principal, agent, choice):
    runs, _ = agent
    Path(hosted_service.settings.preferences_path).write_text(json.dumps({"skill_titles": choice}))

    learned(hosted_service, principal)
    if hasattr(hosted_service, "titling"):
        hosted_service.titling.join()

    assert runs == []
