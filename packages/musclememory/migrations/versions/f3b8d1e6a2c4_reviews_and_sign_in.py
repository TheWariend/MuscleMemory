"""Skill approval and one-time dashboard sign-in codes.

Skills learned automatically wait for a person's OK (reviewed_at) before agents get
them. Skills that already exist were learned on purpose, so they count as approved.
"""

import sqlalchemy as sa
from alembic import op

revision = "f3b8d1e6a2c4"
down_revision = "e7a4c2d9b6f1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("procedures") as batch:
        batch.add_column(sa.Column("reviewed_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE procedures SET reviewed_at = created_at")
    op.create_table(
        "sign_in_codes",
        sa.Column("code_digest", sa.String(64), primary_key=True),
        sa.Column("tenant", sa.String(128), nullable=False),
        sa.Column("grants", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_table("sign_in_codes")
    with op.batch_alter_table("procedures") as batch:
        batch.drop_column("reviewed_at")
