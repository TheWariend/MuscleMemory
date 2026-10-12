from dataclasses import replace

import httpx
import pytest
from conftest import PROFILE, validated
from musclememory.compiler import compile_trace
from musclememory.config import Settings
from musclememory.core import ExecutionEvent, LearningPolicy, RecallInput
from musclememory.retrieval import Embeddings, bm25_scores, reciprocal_rank_fusion
from musclememory.storage import Revision


def test_bm25_frequency_length_and_stopwords():
    documents = ["manifest manifest inspect", "the manifest " + "unrelated " * 30, "bake cake"]
    scores = bm25_scores(documents, "the manifest")
    assert scores[0] > scores[1] > scores[2] == 0
    assert scores == bm25_scores(documents, "manifest")
    assert bm25_scores([], "manifest") == []


def test_rrf_merges_independent_rankings_once():
    scores = reciprocal_rank_fusion([["a", "b", "a"], ["c", "b"]])
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(2 / 62)
    assert scores["b"] > scores["a"] == scores["c"]


def test_graph_prunes_failed_branch_preserves_parallel_sibling():
    events = [
        ExecutionEvent(
            event_id="root",
            sequence=0,
            kind="tool",
            tool_name="read",
            capability="repository.inspect",
        )
    ]
    for index, (token, dependencies, status) in enumerate(
        [
            ("failed", ["root"], "failure"),
            ("lost", ["failed"], "success"),
            ("lost_child", ["lost"], "success"),
            ("sibling", ["root"], "success"),
            ("parallel", ["root"], "success"),
        ],
        1,
    ):
        events.append(
            ExecutionEvent(
                event_id=token,
                sequence=index,
                kind="tool",
                tool_name="read",
                capability="repository.inspect",
                dependencies=dependencies,
                status=status,
            )
        )
    events.append(ExecutionEvent(event_id="done", sequence=len(events), kind="completion"))
    definition, _ = compile_trace("inspect", events, LearningPolicy(), "graph", {"summary": "ok"})
    assert definition.step_graph == {
        "nodes": ["root", "sibling", "parallel"],
        "edges": [["root", "sibling"], ["root", "parallel"]],
    }
    assert {step.step_token for step in definition.steps} == {"root", "sibling", "parallel"}


def test_configurable_embedding_dimension_and_hash_label(service, principal, monkeypatch):
    assert len(Embeddings(Settings(embedding_dim=32)).embed("a sentence")) == 32
    for dimension in (0, 2001):
        with pytest.raises(ValueError):
            Settings(embedding_dim=dimension)
    service.settings = replace(service.settings, embedding_dim=32)
    service.embeddings = Embeddings(service.settings)
    learned = validated(service, principal)
    with service.db.session() as session:
        revision = session.get(Revision, (learned["procedure_token"], 1))
        assert len(revision.embedding) == 32 and revision.embedding_payload is None
    result = service.recall(
        principal,
        RecallInput(
            intent_text="Inspect repository manifest",
            runtime_profile=PROFILE,
            visibility_scope="team",
        ),
    )
    assert result["vector_mode"] == "lexical-hash"
    explanation = result["candidates"][0]["explanation"]
    assert "lexical-hash" in explanation and "vector_similarity" not in explanation
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: httpx.Response(
            200, request=httpx.Request("POST", url), json={"data": [{"embedding": [1.0] * 32}]}
        ),
    )
    embeddings = Embeddings(
        Settings(embedding_dim=32, embedding_url="https://provider.example/embed")
    )
    assert embeddings.embed("semantic fixture") == [1.0] * 32
