import pytest
from conftest import validated
from musclememory.core import DomainError, InspectInput


@pytest.fixture
def agents(tmp_path, monkeypatch):
    codex, claude = tmp_path / "codex-home", tmp_path / "claude-home"
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    return codex / "skills", claude / "skills"


def test_a_proven_skill_exports_as_skill_md(service, principal, agents):
    codex, _ = agents
    procedure = validated(service, principal)["procedure_token"]
    with pytest.raises(DomainError) as error:
        service.export_skill(principal, "missing-procedure", "codex")
    assert error.value.code == "not_found"
    detail = service.inspect(principal, InspectInput(procedure_token=procedure))
    assert detail["lifecycle_status"] == "active" and not detail["needs_review"]
    result = service.export_skill(principal, procedure, "codex")
    text = (codex / result["name"] / "SKILL.md").read_text()
    header = dict(line.split(": ", 1) for line in text.split("---")[1].strip().splitlines())
    assert result["name"].startswith("musclememory-") and header["name"] == result["name"]
    assert "passed 2 separate test checks" in header["description"]
    assert "## Original request" in text
    again = service.export_skill(principal, procedure, "codex")
    assert again["unchanged"]


def test_skill_details_have_a_readable_summary(service, principal):
    procedure = validated(service, principal)["procedure_token"]
    summary = service.inspect(principal, InspectInput(procedure_token=procedure))["summary"]
    assert set(summary) == {
        "request",
        "reads",
        "changes",
        "edits",
        "test",
        "look_around_steps",
        "learned_by",
        "checked_by",
    }
    assert sum(summary["checked_by"].values()) == 2


def test_export_never_replaces_a_different_skill(service, principal, agents):
    _, claude = agents
    procedure = validated(service, principal)["procedure_token"]
    name = service.export_skill(principal, procedure, "codex")["name"]
    (claude / name).mkdir(parents=True)
    (claude / name / "SKILL.md").write_text(f"---\nname: {name}\n---\nMine.\n")
    with pytest.raises(DomainError) as error:
        service.export_skill(principal, procedure, "claude")
    assert error.value.code == "skill_exists"
    assert "Mine." in (claude / name / "SKILL.md").read_text()


def test_export_needs_admin_and_a_local_install(service, agents):
    writer = service.authenticate(
        service.issue_key("tenant-a", {"team": ["read", "write", "delete", "admin"]})["api_key"],
        rate_limit=False,
    )
    procedure = validated(service, writer)["procedure_token"]
    reader = service.authenticate(
        service.issue_key("tenant-a", {"team": ["read", "write"]})["api_key"], rate_limit=False
    )
    with pytest.raises(DomainError) as error:
        service.export_skill(reader, procedure, "codex")
    assert error.value.code == "forbidden"
    # Only the mode matters here; full hosted settings are covered elsewhere.
    object.__setattr__(service.settings, "deployment_mode", "hosted")
    try:
        with pytest.raises(DomainError) as error:
            service.export_skill(writer, procedure, "codex")
        assert error.value.code == "local_only"
    finally:
        object.__setattr__(service.settings, "deployment_mode", "local")
