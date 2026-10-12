import json

import pytest
from conftest import PROFILE
from musclememory.core import ObserveInput
from musclememory.observer import ObservedRun
from musclememory.sdk import MuscleMemoryClient


class LocalSink:
    def __init__(self, service, principal):
        self.service, self.principal = service, principal

    def musclememory_observe(self, **arguments):
        return self.service.observe(self.principal, ObserveInput(**arguments))


def test_automatic_python_capture_and_preserved_errors(service, principal):
    run = ObservedRun(LocalSink(service, principal), "Inspect repository manifest", PROFILE, "team")

    @run.instrument("repository.inspect")
    def read(path, api_key):
        return {"path": path, "authorization": f"Bearer {api_key}"}

    result = read("package.json", "sk-abcdef0123456789")
    assert result["authorization"] == "Bearer sk-abcdef0123456789"
    assert "sk-abcdef0123456789" not in json.dumps(run.events)

    @run.instrument("repository.inspect")
    def fail(path):
        raise ValueError("password=sensitive")

    with pytest.raises(ValueError):
        fail("missing")
    assert "sensitive" not in json.dumps(run.events)
    run.complete(succeeded=False)
    trace = service.get_run(principal, run.run_token)
    assert trace["events"][2]["status"] == "failure"
    assert run.measurements()["tool_calls"] == 2 and run.measurements()["tokens"] is None
    with pytest.raises(RuntimeError, match="closed"):
        read("x", "x")


async def test_async_capture(service, principal):
    run = ObservedRun(LocalSink(service, principal), "Inspect repository manifest", PROFILE, "team")

    @run.instrument("repository.inspect")
    async def read(path):
        return {"found": path}

    assert (await read("manifest"))["found"] == "manifest"
    run.complete()
    assert service.get_run(principal, run.run_token)["status"] == "success"


def test_python_sdk_uses_exact_contracts(http, credential):
    import httpx

    def handle(request):
        response = http.request(
            request.method, request.url.path, content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(response.status_code, content=response.content)

    with MuscleMemoryClient(
        "http://testserver", credential["api_key"], transport=httpx.MockTransport(handle)
    ) as client:
        run = ObservedRun(client, "Inspect repository manifest", PROFILE, "team")
        run.instrument("repository.inspect")(lambda path: {"found": path})("manifest")
        run.complete()
        learned = client.musclememory_learn(
            run_token=run.run_token,
            completion_proof={
                "succeeded": True,
                "evidence_event_ids": ["e1"],
                "summary": "Checked manifest",
            },
            learning_policy={},
        )
        assert client.musclememory_inspect(procedure_token=learned["procedure_token"])[
            "definition"
        ]["steps"]
