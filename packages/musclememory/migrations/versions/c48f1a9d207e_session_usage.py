"""Actual client session measurements, separate from procedure evaluations."""

import sqlalchemy as sa
from alembic import op

revision = "c48f1a9d207e"
down_revision = "b3d62e81a409"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "session_usage",
        sa.Column("tenant", sa.String(128), primary_key=True),
        sa.Column("run_token", sa.String(128), primary_key=True),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("measurements", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant", "run_token"], ["runs.tenant", "runs.run_token"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_session_usage_scope", "session_usage", ["tenant", "namespace", "created_at"]
    )


def downgrade():
    op.drop_index("ix_session_usage_scope", table_name="session_usage")
    op.drop_table("session_usage")
