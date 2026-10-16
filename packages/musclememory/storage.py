"""One SQLAlchemy schema for SQLite local storage and PostgreSQL/pgvector."""

from datetime import datetime, timezone
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def utcnow():
    # UTC-naive database timestamps avoid SQLite/PG timezone comparison differences.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Credential(Base):
    __tablename__ = "credentials"
    key_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    tenant: Mapped[str] = mapped_column(String(128), index=True)
    label: Mapped[str] = mapped_column(String(100))
    grants: Mapped[dict] = mapped_column(JSON)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Run(Base):
    __tablename__ = "runs"
    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_token: Mapped[str] = mapped_column(String(128), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(128))
    intent: Mapped[str] = mapped_column(Text)
    runtime: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="recording")
    learned_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    capability_signature: Mapped[list | None] = mapped_column(JSON, nullable=True)
    capability_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    __table_args__ = (
        Index("ix_runs_scope_created", "tenant", "namespace", "created_at"),
        Index("ix_runs_capability", "tenant", "namespace", "status", "capability_key"),
    )


class Event(Base):
    __tablename__ = "events"
    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_token: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant", "run_token"], ["runs.tenant", "runs.run_token"], ondelete="CASCADE"
        ),
        UniqueConstraint("tenant", "run_token", "sequence"),
    )


class Procedure(Base):
    __tablename__ = "procedures"
    procedure_token: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant: Mapped[str] = mapped_column(String(128))
    namespace: Mapped[str] = mapped_column(String(128))
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    lifecycle: Mapped[str] = mapped_column(String(20), default="provisional")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # None means "learned automatically and waiting for a person's OK".
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("tenant", "procedure_token"),
        Index("ix_procedures_scope_status", "tenant", "namespace", "lifecycle"),
        Index("ix_procedures_recent", "tenant", "namespace", "lifecycle", "updated_at"),
    )


class SignInCode(Base):
    """One-time, short-lived code that swaps for a dashboard key."""

    __tablename__ = "sign_in_codes"
    code_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    grants: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class Revision(Base):
    __tablename__ = "revisions"
    procedure_token: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    definition: Mapped[dict] = mapped_column(JSON)
    search_text: Mapped[str] = mapped_column(Text)
    # PostgreSQL stores real vectors of any dimension; partial HNSW indexes cover
    # each indexed dimension (see migrations and ensure_vector_index).
    embedding: Mapped[list] = mapped_column(JSON().with_variant(Vector(), "postgresql"))
    embedding_space: Mapped[str] = mapped_column(String(200))
    embedding_dimensions: Mapped[int] = mapped_column(Integer, default=128, server_default="128")
    # Legacy JSON copy for vectors written before the any-dimension column.
    embedding_payload: Mapped[list | None] = mapped_column(JSON, nullable=True)
    step_graph: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    capability_signature: Mapped[list | None] = mapped_column(JSON, nullable=True)
    successes: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    validation_status: Mapped[str] = mapped_column(String(20), default="provisional")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When a session showed this skill's own test failing before the fix and passing after.
    test_proven_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant", "procedure_token"],
            ["procedures.tenant", "procedures.procedure_token"],
            ondelete="CASCADE",
        ),
        Index("ix_revisions_scope_text", "tenant", "search_text"),
    )


class Evaluation(Base):
    __tablename__ = "evaluations"
    evaluation_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant: Mapped[str] = mapped_column(String(128))
    procedure_token: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer)
    run_token: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[dict] = mapped_column(JSON)
    metrics: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(
            ["procedure_token", "version"],
            ["revisions.procedure_token", "revisions.version"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant", "procedure_token", "version", "run_token"),
        Index("ix_evaluations_tenant", "tenant"),
    )


class ArtifactPosting(Base):
    __tablename__ = "artifact_postings"
    procedure_token: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction: Mapped[str] = mapped_column(String(16), primary_key=True)
    value: Mapped[str] = mapped_column(String(8192), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    namespace: Mapped[str] = mapped_column(String(128))
    __table_args__ = (
        ForeignKeyConstraint(
            ["procedure_token", "version"],
            ["revisions.procedure_token", "revisions.version"],
            ondelete="CASCADE",
        ),
        Index("ix_artifact_lookup", "tenant", "namespace", "direction", "value"),
    )


class SessionUsage(Base):
    __tablename__ = "session_usage"
    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_token: Mapped[str] = mapped_column(String(128), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(128))
    measurements: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant", "run_token"], ["runs.tenant", "runs.run_token"], ondelete="CASCADE"
        ),
        Index("ix_session_usage_scope", "tenant", "namespace", "created_at"),
    )


class Contrast(Base):
    __tablename__ = "contrasts"
    contrast_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant: Mapped[str] = mapped_column(String(128))
    namespace: Mapped[str] = mapped_column(String(128))
    failed_run_token: Mapped[str] = mapped_column(String(128))
    successful_run_token: Mapped[str] = mapped_column(String(128))
    capability_signature: Mapped[list] = mapped_column(JSON)
    step_diff: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant", "failed_run_token"], ["runs.tenant", "runs.run_token"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["tenant", "successful_run_token"],
            ["runs.tenant", "runs.run_token"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant", "failed_run_token", "successful_run_token"),
        Index("ix_contrasts_scope", "tenant", "namespace", "successful_run_token"),
    )


class Audit(Base):
    __tablename__ = "audit_log"
    audit_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant: Mapped[str] = mapped_column(String(128))
    namespace: Mapped[str] = mapped_column(String(128))
    key_id: Mapped[str] = mapped_column(String(36))
    operation: Mapped[str] = mapped_column(String(64))
    reference: Mapped[str] = mapped_column(String(128))
    units: Mapped[int] = mapped_column(Integer, default=1)
    duration_ms: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (Index("ix_audit_scope_time", "tenant", "namespace", "created_at"),)


class Tombstone(Base):
    """A removed skill's fingerprint, so the same skill is not learned again."""

    __tablename__ = "tombstones"
    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    reason: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RateWindow(Base):
    __tablename__ = "rate_windows"
    key_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    minute: Mapped[int] = mapped_column(Integer)
    requests: Mapped[int] = mapped_column(Integer)


def ensure_vector_index(engine, dimensions):
    """Create the partial HNSW index for a configured embedding dimension (PostgreSQL)."""
    if engine.dialect.name != "postgresql":
        return False
    from sqlalchemy import text

    dimensions = int(dimensions)
    with engine.begin() as connection:
        connection.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_revisions_vector_{dimensions} ON revisions "
                f"USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops) "
                f"WITH (m = 16, ef_construction = 64) WHERE embedding_dimensions = {dimensions}"
            )
        )
    return True


class Database:
    def __init__(self, url: str):
        options = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url:
                options["poolclass"] = StaticPool
        else:
            options.update(pool_size=5, max_overflow=10)
        self.engine = create_engine(url, **options)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def sqlite_setup(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")

        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def transaction(self):
        """Serialize SQLite writes; PostgreSQL callers take row-level locks."""
        session = self.session()
        if self.engine.dialect.name == "sqlite":
            from sqlalchemy import text

            session.execute(text("BEGIN IMMEDIATE"))
        return session
