from dataclasses import replace

from conftest import PROFILE, evaluate, learned
from musclememory.api import dashboard_dir


def test_rest_learning_workflow(http):
    events = [
        {"event_id": "intent", "sequence": 0, "kind": "intent", "intent_text": "Inspect manifest"},
        {
            "event_id": "read",
            "sequence": 1,
            "kind": "tool",
            "tool_name": "read_file",
            "capability": "repository.inspect",
            "arguments": {"path": "package.json"},
            "duration_ms": 4,
        },
        {"event_id": "done", "sequence": 2, "kind": "completion"},
    ]
    common = {"runtime_profile": PROFILE.model_dump(), "visibility_scope": "team"}
    for i in range(3):
        token = f"run-{i}"
        assert (
            http.post(
                "/v1/runs", json={**common, "run_token": token, "intent_text": "Inspect manifest"}
            ).status_code
            == 201
        )
        assert (
            http.post(
                "/v1/observe", json={**common, "run_token": token, "event_batch": events}
            ).json()["accepted_event_count"]
            == 3
        )
        if i == 0:
            response = http.post(
                "/v1/learn",
                json={
                    "run_token": token,
                    "completion_proof": {
                        "succeeded": True,
                        "evidence_event_ids": ["read"],
                        "summary": "Manifest inspected",
                    },
                    "learning_policy": {},
                },
            )
            assert response.status_code == 200
            procedure = response.json()["procedure_token"]
        else:
            response = http.post(
                "/v1/evaluate",
                json={
                    "procedure_token": procedure,
                    "run_token": token,
                    "outcome_report": {
                        "succeeded": True,
                        "procedure_version": 1,
                        "evidence_event_ids": ["read"],
                    },
                    "performance_metrics": {"observed": {"tool_calls": 1, "duration_ms": 4}},
                },
            )
            assert response.status_code == 200, response.text
    response = http.post("/v1/recall", json={**common, "intent_text": "Inspect manifest"})
    assert not response.json()["no_match"]
    assert (
        http.get(f"/v1/procedures/{procedure}/revisions").json()["items"][0]["status"]
        == "validated"
    )
    listing = http.get("/v1/procedures?visibility_scope=team&limit=1").json()
    assert listing["limit"] == 1
    summary = listing["items"][0]
    assert summary["procedure_token"] == procedure and summary["step_count"] == 1
    assert summary["successes"] == 2 and summary["failures"] == 0 and summary["updated_at"]
    run_summary = http.get("/v1/runs?visibility_scope=team&limit=1").json()["items"][0]
    assert run_summary["agent_name"] and run_summary["created_at"].endswith("Z")
    assert http.post(f"/v1/procedures/{procedure}/retire").status_code == 200
    assert http.delete(f"/v1/procedures/{procedure}").json()["deleted"]


def test_auth_validation_and_error_privacy(http):
    assert http.get("/v1/session", headers={"Authorization": ""}).status_code == 401
    assert http.post("/mcp", headers={"Authorization": ""}).status_code == 401
    assert http.get("/v1/usage?visibility_scope=private").status_code == 403
    response = http.post("/v1/recall", json={"intent_text": {"api_key": "sensitive"}})
    assert response.status_code == 422 and "sensitive" not in response.text
    assert http.get("/v1/procedures/missing").status_code == 404
    assert http.get("/v1/procedures?visibility_scope=team&limit=1000").status_code == 422
    assert (
        http.post("/v1/recall", headers={"Origin": "https://evil.example"}, json={}).status_code
        == 403
    )
    assert http.get("/health").json()["status"] == "ok"
    assert "/v1/observe" in http.get("/openapi.json").json()["paths"]
    # The CSP blocks Swagger's CDN scripts, so the reference lives on the website.
    docs = http.get("/docs", follow_redirects=False)
    assert docs.status_code == 307
    assert docs.headers["location"] == "https://thewariend.com/musclememory/api"
    assert http.get("/redoc").status_code == 404
    if dashboard_dir().is_dir():  # CI runs these tests without building the dashboard
        for address in ("/", "/dashboard"):
            opened = http.get(address, follow_redirects=False)
            assert opened.status_code == 307 and opened.headers["location"] == "/dashboard/"


def test_keys_and_rate_limit(http, service, credential):
    keys = http.get("/v1/keys?visibility_scope=team").json()
    assert "digest" not in str(keys) and "api_key" not in str(keys)
    service.settings = replace(service.settings, requests_per_minute=1)
    assert http.get("/v1/session").status_code == 429
    service.settings = replace(service.settings, requests_per_minute=120)
    assert http.delete(f"/v1/keys/{credential['key_id']}?visibility_scope=team").status_code == 200
    assert http.get("/v1/session").status_code == 401


def test_evaluation_idempotence_and_no_savings_without_baseline(service, principal):
    result = learned(service, principal)
    outcome = evaluate(service, principal, result["procedure_token"], "independent")
    from musclememory.core import EvaluateInput

    request = EvaluateInput(
        procedure_token=result["procedure_token"],
        run_token="independent",
        outcome_report={"succeeded": True, "procedure_version": 1, "evidence_event_ids": ["read"]},
        performance_metrics={
            "observed": {"tool_calls": 1, "duration_ms": 10, "tokens": 50},
            "baseline": {"tool_calls": 3, "duration_ms": 30, "tokens": 100},
        },
    )
    assert service.evaluate(principal, request) == outcome
    assert service.usage(principal, "team")["evaluations"] == 1
