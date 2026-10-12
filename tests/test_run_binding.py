import pytest
from fastapi.testclient import TestClient
from musclememory.api import create_app
from musclememory.core import DomainError, ObserveInput, RecallInput, RuntimeProfile

PROFILE = RuntimeProfile(agent_name="codex", bindings={"shell.execute": "Bash"})


def record(service, principal, token, *, closed=False):
    events = [
        {"event_id": "i", "sequence": 0, "kind": "intent", "intent_text": "Inspect the project"},
        {
            "event_id": "t",
            "sequence": 1,
            "kind": "tool",
            "tool_name": "Bash",
            "capability": "shell.execute",
            "arguments": {"command": "rg --files"},
        },
    ]
    if closed:
        events.append(
            {
                "event_id": "end",
                "sequence": 2,
                "kind": "completion",
                "status": "failure",
                "capture_complete": False,
            }
        )
    service.observe(
        principal,
        ObserveInput(
            run_token=token, event_batch=events, runtime_profile=PROFILE, visibility_scope="team"
        ),
    )


def test_rest_binding_checks_ownership_intent_profile_and_lifecycle(service, principal, credential):
    memory = service
    record(memory, principal, "current")
    record(memory, principal, "ended", closed=True)
    body = {
        "intent_text": "Inspect the project",
        "runtime_profile": PROFILE.model_dump(),
        "visibility_scope": "team",
    }
    headers = {
        "Authorization": "Bearer " + credential["api_key"],
        "X-MuscleMemory-Run": "current",
    }
    with TestClient(create_app(memory), headers=headers) as client:
        assert client.post("/v1/recall", json=body).status_code == 200
        for name, changed in (
            ("intent_text", "Another task"),
            ("runtime_profile", {"agent_name": "another-agent", "bindings": PROFILE.bindings}),
        ):
            assert client.post("/v1/recall", json={**body, name: changed}).status_code == 409
        assert (
            client.post(
                "/v1/recall", json=body, headers={"X-MuscleMemory-Run": "ended"}
            ).status_code
            == 409
        )
    other = memory.issue_key("another-tenant", {"team": ["read"]})
    foreign = memory.authenticate(other["api_key"], rate_limit=False)
    with pytest.raises(DomainError, match="not found"):
        memory.recall(foreign, RecallInput(**body), observation_run="current")
