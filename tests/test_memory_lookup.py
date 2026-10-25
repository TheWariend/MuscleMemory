import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import PROFILE, evaluate, learned, trace
from musclememory.client_hooks import DEFAULT_TOOLS, HookQueue, run_hook
from musclememory.core import DomainError, MemoryLookupInput
from musclememory.sdk import MuscleMemoryClient
from musclememory.storage import MemoryLookup, Procedure, Revision
from sqlalchemy import func, select


def hook_client(http, credential):
    def forward(request):
        response = http.request(
            request.method, str(request.url), content=request.content, headers=dict(request.headers)
        )
        assert response.status_code < 400, response.text
        return httpx.Response(response.status_code, content=response.content)

    return MuscleMemoryClient(
        "http://testserver", credential["api_key"], transport=httpx.MockTransport(forward)
    )


@pytest.mark.parametrize("ready", [False, True])
def test_hook_reports_real_lookup_without_changing_proof(
    http, service, principal, credential, tmp_path, monkeypatch, ready
):
    skill = learned(service, principal, approve=False)["procedure_token"]
    if ready:
        evaluate(service, principal, skill, "check-1")
        evaluate(service, principal, skill, "check-2")
        service.approve(principal, skill)
    client = hook_client(http, credential)
    config = {"namespace": "team", "state_dir": str(tmp_path / "state")}
    monkeypatch.setattr(
        "musclememory.client_hooks.load_config",
        lambda *a: (config, DEFAULT_TOOLS["codex"], PROFILE, client),
    )
    output = run_hook(
        "unused",
        "codex",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "real-prompt",
            "prompt": "Inspect repository manifest",
            "cwd": str(tmp_path),
        },
    )
    page = http.get("/v1/runs?visibility_scope=team&limit=100").json()["items"]
    row = next(row for row in page if row["memory_lookup"])
    lookup = row["memory_lookup"]
    assert lookup["status"] == ("delivered" if ready else "no_match")
    assert lookup["application_verified"] is False
    assert http.get(f"/v1/runs/{row['run_token']}").json()["memory_lookups"] == [lookup]
    if ready:
        assert json.loads(output["hookSpecificOutput"]["additionalContext"])["procedure"] == skill
        assert lookup["proof"]["successes"] == 2 and lookup["proof"]["reviewed"]
        assert lookup["context_tokens"] > 0
    else:
        assert output == {}  # No tokens added to the agent's prompt on abstention.
        assert lookup["workspace_readiness"] == {
            "needs_review": 1,
            "needs_checks": 1,
            "required_checks": 2,
        }
    with service.db.session() as db:
        revision = db.get(Revision, (skill, 1))
        assert revision.successes == (2 if ready else 0)
        assert revision.failures == 0
        assert (db.get(Procedure, skill).reviewed_at is not None) == ready


def test_lookup_retry_is_immutable_owned_and_purged_with_run(http, service, principal):
    trace(service, principal)
    body = {"lookup_token": "lookup-1", "status": "no_match", "reason": "no_eligible_skill"}
    path = "/v1/runs/training/memory"
    assert http.post(path, json=body).status_code == 200
    assert http.post(path, json=body).status_code == 200
    conflict = http.post(path, json={**body, "reason": "different"})
    assert conflict.status_code == 409
    assert len(service.get_run(principal, "training")["memory_lookups"]) == 1
    other = service.authenticate(service.issue_key("other", {"team": ["write"]})["api_key"], False)
    with pytest.raises(DomainError) as error:
        service.record_memory_lookup(other, "training", MemoryLookupInput(**body))
    assert error.value.code == "not_found"
    service.delete_memory(principal, "training", "run")
    with service.db.session() as db:
        assert db.scalar(select(func.count()).select_from(MemoryLookup)) == 0


def test_report_cannot_claim_a_skill_from_another_namespace(service, principal):
    skill = learned(service, principal)["procedure_token"]
    actor = service.authenticate(
        service.issue_key("tenant-a", {"team": ["read"], "private": ["read", "write"]})["api_key"],
        False,
    )
    trace(service, actor, "private-run", namespace="private")
    with pytest.raises(DomainError) as error:
        service.record_memory_lookup(
            actor,
            "private-run",
            MemoryLookupInput(
                lookup_token="attempt",
                status="delivered",
                reason="delivered",
                procedure_token=skill,
                procedure_version=1,
            ),
        )
    assert error.value.code == "lookup_scope"


def test_offline_lookup_report_survives_retry(tmp_path):
    queue = HookQueue(tmp_path / "state", "a" * 64)
    try:
        queue.record_lookup("run-1", "error", "lookup_failed")

        def offline(*args, **kwargs):
            raise httpx.ConnectError("offline")

        with pytest.raises(httpx.ConnectError):
            queue.flush_lookups(SimpleNamespace(record_memory_lookup=offline))
        received = []
        assert (
            queue.flush_lookups(
                SimpleNamespace(
                    record_memory_lookup=lambda token, **body: received.append((token, body))
                )
            )
            == 1
        )
        assert received[0][0] == "run-1"
        assert received[0][1]["status"] == "error"
        assert queue.flush_lookups(SimpleNamespace()) == 0
    finally:
        queue.close()


@pytest.mark.parametrize("missing_endpoint", [False, True])
def test_deleted_run_does_not_block_new_reports_but_old_server_keeps_queue(
    tmp_path, missing_endpoint
):
    queue = HookQueue(tmp_path / "state", "a" * 64)
    try:
        queue.record_lookup("deleted", "no_match", "no_eligible_skill")
        queue.record_lookup("live", "no_match", "no_eligible_skill")
        received = []

        def send(token, **body):
            if token == "deleted":
                response = httpx.Response(
                    404,
                    request=httpx.Request("POST", "http://localhost/v1/runs/deleted/memory"),
                    json={"detail": "Not Found"}
                    if missing_endpoint
                    else {
                        "error": {"code": "not_found", "message": "Execution record was not found"}
                    },
                )
                response.raise_for_status()
            received.append(token)

        client = SimpleNamespace(record_memory_lookup=send)
        if missing_endpoint:
            with pytest.raises(httpx.HTTPStatusError):
                queue.flush_lookups(client)
            assert not received
            assert (
                queue.db.execute("SELECT count(*) FROM memory_lookups WHERE ack=0").fetchone()[0]
                == 2
            )
        else:
            assert queue.flush_lookups(client) == 1 and received == ["live"]
    finally:
        queue.close()


def test_reports_require_write_access_and_reject_validation_fields(http, service, principal):
    trace(service, principal)
    read_key = service.issue_key("tenant-a", {"team": ["read"]})["api_key"]
    body = {"lookup_token": "x", "status": "no_match", "reason": "no_eligible_skill"}
    assert (
        http.post(
            "/v1/runs/training/memory", json=body, headers={"Authorization": f"Bearer {read_key}"}
        ).status_code
        == 403
    )
    assert http.post("/v1/runs/training/memory", json={**body, "successes": 2}).status_code == 422
    assert (
        http.post("/v1/runs/training/memory", json={**body, "status": "delivered"}).status_code
        == 422
    )
