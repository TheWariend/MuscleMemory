"""Evidence-linked learning decisions and versioned semantic memory edges."""

import sqlalchemy as sa
from alembic import op

revision = "f6a1b2c3d4e5"
down_revision = "c5e8a1d4f7b2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("project_scope", sa.String(64), nullable=True))
    op.create_table(
        "learning_decisions",
        sa.Column("decision_id", sa.String(36), primary_key=True),
        sa.Column("tenant", sa.String(128), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("run_token", sa.String(128), nullable=False),
        sa.Column("pipeline", sa.String(80), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("proposal", sa.JSON(), nullable=True),
        sa.Column("assessment", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tenant", "run_token", "pipeline", "evidence_digest"),
    )
    op.create_index(
        "ix_learning_scope_created", "learning_decisions", ["tenant", "namespace", "created_at"]
    )
    op.create_table(
        "memory_edges",
        sa.Column("edge_id", sa.String(36), primary_key=True),
        sa.Column("tenant", sa.String(128), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("target_key", sa.String(64), nullable=False),
        sa.Column("relation", sa.String(32), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tenant", "namespace", "source_key", "target_key", "relation"),
    )
    for side in ("source", "target"):
        op.create_index(
            f"ix_memory_edges_{side}", "memory_edges", ["tenant", "namespace", f"{side}_key"]
        )

    op.create_table(
        "learning_jobs",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("tenant", sa.String(128), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("run_token", sa.String(128), nullable=False),
        sa.Column("pipeline", sa.String(80), nullable=False),
        sa.Column("actor_key", sa.String(36), nullable=False),
        sa.Column("hint", sa.String(128), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.UniqueConstraint("tenant", "run_token", "pipeline"),
    )
    op.create_index("ix_learning_jobs_ready", "learning_jobs", ["status", "available_at"])


def downgrade():
    op.drop_table("learning_jobs")
    op.drop_table("memory_edges")
    op.drop_table("learning_decisions")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("project_scope")
