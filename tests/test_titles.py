"""Naming skills with the agent you already use: off by default, never trusted blindly."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import learned
from musclememory import titles
from musclememory.core import DomainError, InspectInput


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
