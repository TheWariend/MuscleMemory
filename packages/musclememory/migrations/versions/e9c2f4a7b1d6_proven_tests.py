"""Proven tests: a skill's test must have caught the bug at least once.

A session proves a skill's test when that test failed before the last code change and
passed after it. Revisions that already exist count as proven, so upgrading doesn't
hide skills people already use.
"""

import sqlalchemy as sa
from alembic import op

revision = "e9c2f4a7b1d6"
down_revision = "d4b7e1c9a3f2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("revisions") as batch:
        batch.add_column(sa.Column("test_proven_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE revisions SET test_proven_at = created_at")


def downgrade():
    with op.batch_alter_table("revisions") as batch:
        batch.drop_column("test_proven_at")
