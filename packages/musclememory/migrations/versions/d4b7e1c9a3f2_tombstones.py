"""Tombstones: removed skills stay removed.

Deleting or retiring a skill stores a fingerprint of what it taught (files, edits and
test), never its content, so the same skill is not learned again.
"""

import sqlalchemy as sa
from alembic import op

revision = "d4b7e1c9a3f2"
down_revision = "a9c3e5f7b2d1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tombstones",
        sa.Column("tenant", sa.String(128), primary_key=True),
        sa.Column("namespace", sa.String(128), primary_key=True),
        sa.Column("fingerprint", sa.String(64), primary_key=True),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("tombstones")
