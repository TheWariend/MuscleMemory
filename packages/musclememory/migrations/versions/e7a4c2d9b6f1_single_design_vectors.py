"""One retrieval design: real vectors of any dimension, structured indexes removed.

PostgreSQL keeps every vector in an unconstrained pgvector column. A partial HNSW
index per dimension (128 and the default 384 here; others via `musclememory
migrate`) keeps semantic recall indexed as the store grows. SQLite keeps JSON.
The experimental structured state index and its postings are dropped.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "e7a4c2d9b6f1"
down_revision = "c48f1a9d207e"
branch_labels = None
depends_on = None

INDEXED_DIMENSIONS = (128, 384)


def vector_index_sql(dimensions):
    return (
        f"CREATE INDEX IF NOT EXISTS ix_revisions_vector_{dimensions} ON revisions "
        f"USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops) "
        f"WITH (m = 16, ef_construction = 64) WHERE embedding_dimensions = {dimensions}"
    )


def strip_state_index(bind):
    rows = bind.execute(sa.text("SELECT procedure_token, version, definition FROM revisions"))
    for token, version, definition in rows.fetchall():
        if isinstance(definition, str):
            definition = json.loads(definition)
        if "state_index" in definition:
            definition.pop("state_index")
            bind.execute(
                sa.text(
                    "UPDATE revisions SET definition = :definition "
                    "WHERE procedure_token = :token AND version = :version"
                ).bindparams(sa.bindparam("definition", type_=sa.JSON())),
                {"definition": definition, "token": token, "version": version},
            )


def upgrade():
    bind = op.get_bind()
    strip_state_index(bind)
    op.drop_table("state_postings")
    with op.batch_alter_table("revisions") as batch:
        batch.drop_column("state_index")
    op.create_index(
        "ix_procedures_recent", "procedures", ["tenant", "namespace", "lifecycle", "updated_at"]
    )
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_revisions_vector")
        op.execute("ALTER TABLE revisions ALTER COLUMN embedding TYPE vector")
        op.execute(
            "UPDATE revisions SET embedding = replace(embedding_payload::text, ' ', '')::vector, "
            "embedding_payload = NULL WHERE embedding_payload IS NOT NULL"
        )
        for dimensions in INDEXED_DIMENSIONS:
            op.execute(vector_index_sql(dimensions))
    else:
        op.execute(
            "UPDATE revisions SET embedding = embedding_payload, embedding_payload = NULL "
            "WHERE embedding_payload IS NOT NULL"
        )


def downgrade():
    bind = op.get_bind()
    op.drop_index("ix_procedures_recent", table_name="procedures")
    if bind.dialect.name == "postgresql":
        names = bind.execute(
            sa.text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'revisions' "
                "AND indexname LIKE 'ix_revisions_vector_%'"
            )
        ).scalars()
        for name in list(names):
            op.execute(f'DROP INDEX IF EXISTS "{name}"')
        op.execute(
            "UPDATE revisions SET embedding_payload = to_json(embedding::real[]), "
            "embedding = array_fill(0::real, ARRAY[128])::vector "
            "WHERE embedding_dimensions <> 128"
        )
        op.execute("ALTER TABLE revisions ALTER COLUMN embedding TYPE vector(128)")
        op.execute(
            "CREATE INDEX ix_revisions_vector ON revisions USING hnsw "
            "(embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64) "
            "WHERE embedding_dimensions = 128"
        )
    else:
        zero = json.dumps([0.0] * 128)
        op.execute(
            sa.text(
                "UPDATE revisions SET embedding_payload = embedding, embedding = :zero "
                "WHERE embedding_dimensions <> 128"
            ).bindparams(zero=zero)
        )
    with op.batch_alter_table("revisions") as batch:
        batch.add_column(sa.Column("state_index", sa.JSON(), nullable=True))
    op.create_table(
        "state_postings",
        sa.Column("procedure_token", sa.String(36), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("feature_key", sa.String(104), primary_key=True),
        sa.Column("tenant", sa.String(128), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.ForeignKeyConstraint(
            ["procedure_token", "version"],
            ["revisions.procedure_token", "revisions.version"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_state_postings_lookup", "state_postings", ["tenant", "namespace", "feature_key"]
    )
