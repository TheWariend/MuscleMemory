from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from conftest import PROFILE, learned, trace, validated
from musclememory.config import Settings
from musclememory.core import DomainError, LearnInput, ObserveInput, RecallInput
from musclememory.retrieval import Embeddings
from musclememory.security import redact


def test_semantic_provider_is_configurable_and_space_isolated(service, principal, monkeypatch):
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs["json"]))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"data": [{"embedding": [1.0] + [0.0] * 127}]},
        )

    monkeypatch.setattr(httpx, "post", post)
    settings = replace(
        service.settings,
        embedding_url="https://inference.example/embed",
        embedding_model="semantic-fixture",
    )
    service.settings = settings
    service.embeddings = Embeddings(settings)
    validated(service, principal)
    response = service.recall(
        principal,
        RecallInput(
            intent_text="Examine source package metadata",
            runtime_profile=PROFILE,
            visibility_scope="team",
        ),
    )
    assert not response["no_match"] and response["vector_mode"] == "semantic"
    assert requests and requests[0][1]["model"] == "semantic-fixture"
    service.embeddings = Embeddings(replace(settings, embedding_model="another-model"))
    switched = service.recall(
        principal,
        RecallInput(
            intent_text="Inspect repository manifest",
            runtime_profile=PROFILE,
            visibility_scope="team",
        ),
    )
    # Exact/lexical evidence survives a provider switch; old vectors cannot
    # participate in the new embedding space until explicit reindexing.
    assert not switched["no_match"]
    assert switched["candidates"][0]["explanation"]["vector_similarity"] == 0


@pytest.mark.parametrize("vector", [[1.0], [float("nan")] * 128, [0.0] * 128])
def test_invalid_provider_vectors_fail_closed(monkeypatch, vector):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: httpx.Response(
            200, request=httpx.Request("POST", url), json={"data": [{"embedding": vector}]}
        ),
    )
    with pytest.raises(DomainError) as error:
        Embeddings(Settings(embedding_url="https://provider.example/embed")).embed("intent")
    assert error.value.status == 503


def test_learning_retry_requires_identical_inputs(service, principal):
    result = learned(service, principal)
    request = LearnInput(
        run_token="training",
        completion_proof={
            "succeeded": True,
            "evidence_event_ids": ["read"],
            "summary": "Manifest inspected",
        },
    )
    assert service.learn(principal, request) == result
    request.learning_policy.title = "Changed procedure"
    with pytest.raises(DomainError) as error:
        service.learn(principal, request)
    assert error.value.status == 409


def test_concurrent_event_resends_are_idempotent(service, principal):
    events = trace(service, principal)
    request = ObserveInput(
        run_token="training", event_batch=events, runtime_profile=PROFILE, visibility_scope="team"
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.observe(principal, request), range(8)))
    assert all(result["duplicate_event_count"] == 3 for result in results)
    assert len(service.get_run(principal, "training")["events"]) == 3


def test_insecure_settings_and_deep_payloads_rejected():
    with pytest.raises(ValueError):
        Settings(min_successes=1)
    with pytest.raises(ValueError):
        Settings(embedding_url="http://insecure.example")
    value = {"token": "sensitive", "auth": "sensitive"}
    assert "sensitive" not in str(redact(value))
    for _ in range(25):
        value = {"child": value}
    with pytest.raises(DomainError):
        redact(value)
