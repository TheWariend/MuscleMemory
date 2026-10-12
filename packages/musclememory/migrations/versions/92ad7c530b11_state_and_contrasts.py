"""Value-free state postings, run signatures and scoped failure contrasts."""

import hashlib
import json

import sqlalchemy as sa
from alembic import op
from musclememory.core import DomainError, ProcedureDefinition
from pydantic import ValidationError

revision = "92ad7c530b11"
down_revision = "71c4f218a0d1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column("capability_signature", sa.JSON(), nullable=True))
    op.add_column("runs", sa.Column("capability_key", sa.String(64), nullable=True))
    op.create_index(
        "ix_runs_capability", "runs", ["tenant", "namespace", "status", "capability_key"]
    )
    op.add_column("revisions", sa.Column("capability_signature", sa.JSON(), nullable=True))
    op.add_column("revisions", sa.Column("state_index", sa.JSON(), nullable=True))
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
    op.create_table(
        "contrasts",
        sa.Column("contrast_id", sa.String(36), primary_key=True),
        sa.Column("tenant", sa.String(128), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("failed_run_token", sa.String(128), nullable=False),
        sa.Column("successful_run_token", sa.String(128), nullable=False),
        sa.Column("capability_signature", sa.JSON(), nullable=False),
        sa.Column("step_diff", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant", "failed_run_token"], ["runs.tenant", "runs.run_token"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tenant", "successful_run_token"],
            ["runs.tenant", "runs.run_token"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant", "failed_run_token", "successful_run_token"),
    )
    op.create_index(
        "ix_contrasts_scope", "contrasts", ["tenant", "namespace", "successful_run_token"]
    )
    backfill()


def signature_key(capabilities):
    return hashlib.sha256(json.dumps(capabilities, separators=(",", ":")).encode()).hexdigest()


def backfill():
    # Reconstruct safe v1 shapes from definitions, including typed parameters.
    # Legacy traces and definitions are never rewritten or revalidated here.
    connection = op.get_bind()
    metadata = sa.MetaData()
    runs = sa.Table("runs", metadata, autoload_with=connection)
    events = sa.Table("events", metadata, autoload_with=connection)
    revisions = sa.Table("revisions", metadata, autoload_with=connection)
    procedures = sa.Table("procedures", metadata, autoload_with=connection)
    for row in connection.execute(sa.select(runs.c.tenant, runs.c.run_token)).all():
        payloads = connection.execute(
            sa.select(events.c.payload)
            .where(events.c.tenant == row.tenant, events.c.run_token == row.run_token)
            .order_by(events.c.sequence)
        )
        capabilities = [
            payload["capability"] for (payload,) in payloads if payload["kind"] == "tool"
        ]
        connection.execute(
            runs.update()
            .where(runs.c.tenant == row.tenant, runs.c.run_token == row.run_token)
            .values(capability_signature=capabilities, capability_key=signature_key(capabilities))
        )
    rows = (
        connection.execute(
            sa.select(revisions, procedures.c.namespace).join(
                procedures,
                (revisions.c.procedure_token == procedures.c.procedure_token)
                & (revisions.c.tenant == procedures.c.tenant),
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        try:
            definition = ProcedureDefinition.model_validate(row["definition"])
        except (ValidationError, DomainError, KeyError):
            continue
        graph = {
            "nodes": [step.step_token for step in definition.steps],
            "edges": [
                [parent, step.step_token]
                for step in definition.steps
                for parent in step.dependencies
            ],
        }
        connection.execute(
            revisions.update()
            .where(
                revisions.c.procedure_token == row["procedure_token"],
                revisions.c.version == row["version"],
            )
            .values(
                capability_signature=[step.capability for step in definition.steps],
                step_graph=graph,
            )
        )


def downgrade():
    op.drop_index("ix_contrasts_scope", table_name="contrasts")
    op.drop_table("contrasts")
    op.drop_index("ix_state_postings_lookup", table_name="state_postings")
    op.drop_table("state_postings")
    op.drop_column("revisions", "state_index")
    op.drop_column("revisions", "capability_signature")
    op.drop_index("ix_runs_capability", table_name="runs")
    op.drop_column("runs", "capability_key")
    op.drop_column("runs", "capability_signature")
