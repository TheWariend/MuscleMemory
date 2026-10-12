"""Portable embedding dimensions, explicit procedure DAG, scoped HNSW index.

The legacy vector(128) is retained. Non-128 embeddings use JSON exact scoring.
No old data is discarded when this migration is rolled back.
"""

import sqlalchemy as sa
from alembic import op

revision = "71c4f218a0d1"
down_revision = "2f5a9ec0cbaf"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "revisions",
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False, server_default="128"),
    )
    op.add_column("revisions", sa.Column("embedding_payload", sa.JSON(), nullable=True))
    op.add_column("revisions", sa.Column("step_graph", sa.JSON(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_revisions_vector")
        op.execute(
            "CREATE INDEX ix_revisions_vector ON revisions USING hnsw "
            "(embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64) "
            "WHERE embedding_dimensions = 128"
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_revisions_vector")
        op.execute(
            "CREATE INDEX ix_revisions_vector ON revisions USING hnsw (embedding vector_cosine_ops)"
        )
    # Native DROP COLUMN preserves dependent evaluations (SQLite >= 3.35).
    op.drop_column("revisions", "step_graph")
    op.drop_column("revisions", "embedding_payload")
    op.drop_column("revisions", "embedding_dimensions")
