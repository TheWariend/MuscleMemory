"""Scoped exact artifact/command index with legacy-revision backfill."""

import sqlalchemy as sa
from alembic import op

revision = "b3d62e81a409"
down_revision = "92ad7c530b11"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "artifact_postings",
        sa.Column("procedure_token", sa.String(36), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("direction", sa.String(16), primary_key=True),
        sa.Column("value", sa.String(8192), primary_key=True),
        sa.Column("tenant", sa.String(128), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.ForeignKeyConstraint(
            ["procedure_token", "version"],
            ["revisions.procedure_token", "revisions.version"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_artifact_lookup", "artifact_postings", ["tenant", "namespace", "direction", "value"]
    )
    from musclememory.artifacts import extract_experience

    connection = op.get_bind()
    metadata = sa.MetaData()
    revisions = sa.Table("revisions", metadata, autoload_with=connection)
    procedures = sa.Table("procedures", metadata, autoload_with=connection)
    postings = sa.Table("artifact_postings", metadata, autoload_with=connection)
    for row in connection.execute(
        sa.select(revisions, procedures.c.namespace).join(
            procedures,
            (revisions.c.procedure_token == procedures.c.procedure_token)
            & (revisions.c.tenant == procedures.c.tenant),
        )
    ).mappings():
        experience = extract_experience(row["definition"]["steps"])
        for direction, field in (("read", "reads"), ("write", "writes"), ("command", "commands")):
            for value in experience[field]:
                # PostgreSQL B-tree entries must fit its index page. Long shell
                # bodies remain in inspect; only bounded commands enter exact lookup.
                if len(value.encode()) > 1024:
                    continue
                connection.execute(
                    postings.insert().values(
                        procedure_token=row["procedure_token"],
                        version=row["version"],
                        tenant=row["tenant"],
                        namespace=row["namespace"],
                        direction=direction,
                        value=value,
                    )
                )


def downgrade():
    op.drop_index("ix_artifact_lookup", table_name="artifact_postings")
    op.drop_table("artifact_postings")
