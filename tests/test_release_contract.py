from dataclasses import replace

import pytest
from conftest import PROFILE, validated
from musclememory.artifacts import semantic_document
from musclememory.config import DEFAULT_EMBEDDING_DIM, DEFAULT_EMBEDDING_MODEL, Settings
from musclememory.retrieval import rank_candidates
from musclememory.storage import Procedure, Revision


def test_release_defaults(monkeypatch):
    for key in (
        "MM_EMBEDDING_PROVIDER",
        "MM_EMBEDDING_MODEL",
        "MM_EMBEDDING_DIM",
        "MM_EMBEDDING_URL",
        "MM_SEMANTIC_MIN_SIMILARITY",
        "MM_GUIDANCE_TOKEN_BUDGET",
        "MM_LINK_FANOUT_LIMIT",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.from_env()
    assert settings.embedding_provider == "fastembed"
    assert (settings.embedding_model, settings.embedding_dim) == (
        DEFAULT_EMBEDDING_MODEL,
        DEFAULT_EMBEDDING_DIM,
    )
    assert settings.semantic_min_similarity == 0.40
    assert settings.guidance_token_budget == 350
    assert settings.link_fanout_limit == 8


def test_semantic_document_is_deterministic_task_text(service, principal):
    result = validated(service, principal)
    with service.db.session() as session:
        definition = session.get(Revision, (result["procedure_token"], 1)).definition
    document = semantic_document(definition)
    # No model call: the embedded text is the compiled task signature and title.
    assert definition["intent_signature"] in document and definition["title"] in document
    assert semantic_document(definition) == document


def test_exact_artifact_match_is_literal(service, principal):
    result = validated(service, principal)
    with service.db.session() as session:
        rows = [
            (
                session.get(Procedure, result["procedure_token"]),
                session.get(Revision, (result["procedure_token"], 1)),
            )
        ]
        query = "Check package.json"
        matches = rank_candidates(
            rows, query, service.embeddings.embed(query), PROFILE, service.settings
        )
        assert matches[0]["explanation"]["exact_match"]
        query = "Check package.json.backup"
        matches = rank_candidates(
            rows, query, service.embeddings.embed(query), PROFILE, service.settings
        )
        assert not matches or not matches[0]["explanation"]["exact_target_matches"]


def test_link_fanout_limit_is_validated():
    with pytest.raises(ValueError, match="fan-out"):
        Settings(link_fanout_limit=0)
    assert replace(Settings(), link_fanout_limit=3).link_fanout_limit == 3
