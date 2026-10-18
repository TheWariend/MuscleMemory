import os
from uuid import uuid4

import pytest
from conftest import PROFILE, validated
from musclememory.cli import ROOT, migrate
from musclememory.config import Settings
from musclememory.core import RecallInput
from musclememory.service import MemoryService
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.mark.postgres
def test_postgres_migration_fulltext_vector_and_isolation():
    url = os.getenv("MM_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set MM_TEST_POSTGRES_URL to an isolated PostgreSQL+pgvector test database")
    schema = "mm_test_" + uuid4().hex
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    schema_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema},public"})
    schema_url = schema_url.render_as_string(hide_password=False)
    service = None
    try:
        migrate(schema_url, version_table_schema=schema)
        service = MemoryService(Settings(database_url=schema_url))
        credential = service.issue_key("pg-test", {"team": ["read", "write", "delete", "admin"]})
        actor = service.authenticate(credential["api_key"], False)
        result = validated(service, actor)
        response = service.recall(
            actor,
            RecallInput(
                intent_text="Inspect repository manifest",
                runtime_profile=PROFILE,
                visibility_scope="team",
            ),
        )
        assert response["candidates"][0]["procedure_token"] == result["procedure_token"]
        with service.db.session() as session:
            assert session.execute(text("SELECT '[1,0]'::vector <=> '[0,1]'::vector")).scalar() == 1
            indexes = (
                session.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE tablename='revisions' "
                        "AND schemaname=current_schema()"
                    )
                )
                .scalars()
                .all()
            )
            assert {
                "ix_revisions_vector_128",
                "ix_revisions_vector_384",
                "ix_revisions_fts",
            } <= set(indexes)
        foreign = service.authenticate(
            service.issue_key("pg-other", {"team": ["read"]})["api_key"], False
        )
        assert service.recall(
            foreign,
            RecallInput(
                intent_text="Inspect repository manifest",
                runtime_profile=PROFILE,
                visibility_scope="team",
            ),
        )["no_match"]
    finally:
        # Downgrade only our generated schema, never existing application tables.
        from alembic import command
        from alembic.config import Config

        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", schema_url.replace("%", "%%"))
        config.set_main_option("script_location", str(ROOT / "packages/musclememory/migrations"))
        config.set_main_option("version_table_schema", schema)
        command.downgrade(config, "base")
        if service:
            service.db.engine.dispose()
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()
