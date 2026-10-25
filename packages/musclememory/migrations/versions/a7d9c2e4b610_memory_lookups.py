"""Hook delivery reports, separate from test and application evidence."""

import sqlalchemy as sa
from alembic import op

revision = "a7d9c2e4b610"
down_revision = "f6a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "memory_lookups",
        sa.Column("tenant", sa.String(128), primary_key=True),
        sa.Column("run_token", sa.String(128), primary_key=True),
        sa.Column("lookup_token", sa.String(128), primary_key=True),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant", "run_token"], ["runs.tenant", "runs.run_token"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_memory_lookup_run", "memory_lookups", ["tenant", "run_token", "created_at"])


def downgrade():
    op.drop_index("ix_memory_lookup_run", table_name="memory_lookups")
    op.drop_table("memory_lookups")
