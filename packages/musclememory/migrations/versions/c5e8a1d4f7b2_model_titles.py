"""Agent titles: a skill's title from Claude Code or Codex, if the person turned it on.

Empty means none was asked for or none came back; the dashboard then names the skill
after the files it changed and its test. Search and agents never use it.
"""

import sqlalchemy as sa
from alembic import op

revision = "c5e8a1d4f7b2"
down_revision = "e9c2f4a7b1d6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("procedures") as batch:
        batch.add_column(sa.Column("name", sa.String(200), nullable=True))


def downgrade():
    with op.batch_alter_table("procedures") as batch:
        batch.drop_column("name")
