from dataclasses import replace

import pytest
from alembic import command
from alembic.config import Config
from conftest import evaluate, learned, validated
from musclememory.cli import ROOT
from musclememory.retrieval import Embeddings, bm25_scores
from musclememory.storage import Revision
from sqlalchemy import select, text


def actor(service):
    return service.authenticate(
        service.issue_key("parity", {"team": ["read", "write", "admin"]})["api_key"], False
    )


def populate_corpus(service, principal):
    for index, document in enumerate(
        ("manifest manifest inspect", "read manifest value", "run tests")
    ):
        result = learned(service, principal, f"train-{index}")
        for evaluation in range(2):
            evaluate(service, principal, result["procedure_token"], f"eval-{index}-{evaluation}")
        with service.db.transaction() as session:
            revision = session.get(Revision, (result["procedure_token"], 1))
            revision.search_text = document
            session.commit()
    with service.db.session() as session:
        documents = list(
            session.scalars(select(Revision.search_text).order_by(Revision.search_text))
        )
    return documents, bm25_scores(documents, "the manifest")


@pytest.mark.postgres
def test_bm25_sqlite_postgres_scoring_parity(service, principal, postgres_service):
    pg, _ = postgres_service
    sqlite_documents, sqlite_scores = populate_corpus(service, principal)
    pg_documents, pg_scores = populate_corpus(pg, actor(pg))
    assert sqlite_documents == pg_documents
    assert sqlite_scores == pytest.approx(pg_scores, abs=1e-12)


def cycle(service, config, principal):
    result = validated(service, principal)
    token = result["procedure_token"]
    command.downgrade(config, "2f5a9ec0cbaf")
    with service.db.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM evaluations")) == 2
        assert connection.scalar(text("SELECT count(*) FROM revisions")) == 1
    command.upgrade(config, "head")
    with service.db.session() as session:
        revision = session.get(Revision, (token, 1))
        assert revision.successes == 2 and revision.embedding_dimensions == 128
        assert revision.capability_signature == ["repository.inspect"]
        assert revision.step_graph["nodes"] == ["read"]


def test_sqlite_mode_migration_rollback_preserves_original_data(service, principal):
    config = Config()
    config.set_main_option("sqlalchemy.url", service.settings.database_url)
    config.set_main_option("script_location", str(ROOT / "packages/musclememory/migrations"))
    cycle(service, config, principal)


@pytest.mark.postgres
def test_postgres_mode_migration_rollback_and_other_dimensions(postgres_service):
    service, config = postgres_service
    principal = actor(service)
    cycle(service, config, principal)
    service.settings = replace(service.settings, embedding_dim=32)
    service.embeddings = Embeddings(service.settings)
    result = learned(service, principal, "other-dimension")
    with service.db.session() as session:
        revision = session.get(Revision, (result["procedure_token"], 1))
        assert revision.embedding_dimensions == 32 and len(revision.embedding) == 32
        assert revision.embedding_payload is None


@pytest.mark.postgres
def test_postgres_contrasts_and_namespace_isolation(postgres_service):
    from test_failure_learning import recall, record, train

    service, _ = postgres_service
    principal = actor(service)
    record(service, principal, "failure", success=False)
    result = train(service, principal, extra={"format": "json"})
    response = recall(service, principal)
    assert response["candidates"][0]["procedure_token"] == result["procedure_token"]
    assert response["candidates"][0]["avoidance_rules"]
    foreign = service.authenticate(
        service.issue_key("another-tenant", {"team": ["read"]})["api_key"], False
    )
    assert recall(service, foreign)["no_match"]


@pytest.mark.postgres
def test_postgres_accepts_maximum_length_capability_posting(postgres_service):
    from musclememory.core import LearnInput, ObserveInput

    service, _ = postgres_service
    principal = actor(service)
    capability = "c" * 96
    request = ObserveInput(
        run_token="long-cap",
        visibility_scope="team",
        runtime_profile={"bindings": {capability: "read"}},
        event_batch=[
            {"event_id": "intent", "sequence": 0, "kind": "intent", "intent_text": "Inspect"},
            {
                "event_id": "read",
                "sequence": 1,
                "kind": "tool",
                "capability": capability,
                "tool_name": "read",
            },
            {"event_id": "done", "sequence": 2, "kind": "completion"},
        ],
    )
    assert service.observe(principal, request)["accepted_event_count"] == 3
    assert (
        service.learn(
            principal,
            LearnInput(
                run_token="long-cap",
                completion_proof={
                    "succeeded": True,
                    "evidence_event_ids": ["read"],
                    "summary": "Checked",
                },
            ),
        )["validation_status"]
        == "provisional"
    )
