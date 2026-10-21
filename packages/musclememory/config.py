import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .local import default_database_url, home

# Small local model used for the published Codex evaluations (384 dimensions).
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_EMBEDDING_DIM = 384


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=default_database_url)
    min_successes: int = 2
    min_reliability: float = 0.65
    min_match: float = 0.25
    stale_days: int = 90
    require_proven_test: bool = False
    # Skip the person's OK; skills still need their independent passing checks.
    auto_approve: bool = False
    trace_retention_days: int = 30
    requests_per_minute: int = 120
    candidate_limit: int = 80
    # Artifacts shared by more procedures than this are common infrastructure
    # (lockfiles, READMEs) and do not create related-procedure links. A fixed
    # cap keeps link work and noise bounded as a store grows.
    link_fanout_limit: int = 8
    embedding_url: str | None = None
    embedding_model: str = "local-lexical-v1"
    embedding_dim: int = 128
    embedding_key: str | None = None
    embedding_provider: str | None = None
    embedding_cache_dir: str | None = None
    semantic_min_similarity: float = 0.40
    guidance_token_budget: int = 350
    boundary_llm_url: str | None = None
    boundary_llm_key: str | None = None
    boundary_llm_model: str = "boundary-refinement"
    # Choices made on the dashboard's settings page; None turns them off (as in tests).
    preferences_path: str | None = None
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:*", "localhost:*", "testserver")
    allowed_origins: tuple[str, ...] = ("http://127.0.0.1:*", "http://localhost:*")
    deployment_mode: str = "local"
    public_url: str | None = None

    @classmethod
    def from_env(cls):
        return cls(
            deployment_mode=os.getenv("MM_DEPLOYMENT_MODE", "local"),
            public_url=os.getenv("MM_PUBLIC_URL"),
            database_url=os.getenv("MM_DATABASE_URL") or default_database_url(),
            min_successes=int(os.getenv("MM_MIN_SUCCESSES", "2")),
            min_reliability=float(os.getenv("MM_MIN_RELIABILITY", "0.65")),
            stale_days=int(os.getenv("MM_STALE_DAYS", "90")),
            require_proven_test=os.getenv("MM_REQUIRE_PROVEN_TEST", "false").lower()
            in {"1", "true", "yes"},
            auto_approve=os.getenv("MM_AUTO_APPROVE", "false").lower() in {"1", "true", "yes"},
            trace_retention_days=int(os.getenv("MM_TRACE_RETENTION_DAYS", "30")),
            requests_per_minute=int(os.getenv("MM_REQUESTS_PER_MINUTE", "120")),
            link_fanout_limit=int(os.getenv("MM_LINK_FANOUT_LIMIT", "8")),
            embedding_url=os.getenv("MM_EMBEDDING_URL"),
            embedding_model=os.getenv("MM_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            embedding_dim=int(os.getenv("MM_EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM))),
            embedding_key=os.getenv("MM_EMBEDDING_KEY"),
            embedding_provider=os.getenv("MM_EMBEDDING_PROVIDER")
            or ("remote" if os.getenv("MM_EMBEDDING_URL") else "fastembed"),
            embedding_cache_dir=os.getenv("MM_EMBEDDING_CACHE_DIR"),
            semantic_min_similarity=float(os.getenv("MM_SEMANTIC_MIN_SIMILARITY", "0.40")),
            guidance_token_budget=int(os.getenv("MM_GUIDANCE_TOKEN_BUDGET", "350")),
            boundary_llm_url=os.getenv("MM_BOUNDARY_LLM_URL"),
            boundary_llm_key=os.getenv("MM_BOUNDARY_LLM_KEY"),
            boundary_llm_model=os.getenv("MM_BOUNDARY_LLM_MODEL", "boundary-refinement"),
            preferences_path=str(home() / "preferences.json"),
            allowed_hosts=tuple(
                os.getenv("MM_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*,testserver").split(",")
            ),
            allowed_origins=tuple(
                os.getenv("MM_ALLOWED_ORIGINS", "http://127.0.0.1:*,http://localhost:*").split(",")
            ),
        )

    def __post_init__(self):
        if self.deployment_mode not in {"local", "hosted"}:
            raise ValueError("deployment mode must be local or hosted")
        if self.deployment_mode == "hosted":
            endpoint = urlsplit(self.public_url or "")
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
                or endpoint.path not in {"", "/"}
            ):
                raise ValueError("hosted mode requires a root HTTPS public URL")
            if not self.database_url.startswith("postgresql"):
                raise ValueError("hosted mode requires PostgreSQL")
            if not self.allowed_hosts or any(
                "*" in host or host == "testserver" for host in self.allowed_hosts
            ):
                raise ValueError("hosted mode requires explicit Host allowlists")
            if endpoint.netloc not in self.allowed_hosts:
                raise ValueError("public URL host must be in the Host allowlist")
            if not self.allowed_origins or any(
                "*" in origin or not origin.startswith("https://")
                for origin in self.allowed_origins
            ):
                raise ValueError("hosted mode requires explicit HTTPS Origin allowlists")
            if (self.public_url or "").rstrip("/") not in self.allowed_origins:
                raise ValueError("public URL must be in the Origin allowlist")
        if self.min_successes < 2 or not 0 <= self.min_reliability <= 1:
            raise ValueError("validation requires at least two successful independent evaluations")
        if min(self.stale_days, self.trace_retention_days, self.requests_per_minute) < 1:
            raise ValueError("retention, freshness, and rate limits must be positive")
        if self.link_fanout_limit < 1:
            raise ValueError("link fan-out limit must be positive")
        if not 1 <= self.embedding_dim <= 2000:
            raise ValueError("embedding dimension must be between 1 and 2000")
        if self.embedding_provider not in {None, "lexical-hash", "remote", "fastembed"}:
            raise ValueError("embedding provider must be lexical-hash, remote or fastembed")
        if self.embedding_provider == "remote" and not self.embedding_url:
            raise ValueError("remote embeddings require MM_EMBEDDING_URL")
        if self.embedding_provider in {"lexical-hash", "fastembed"} and self.embedding_url:
            raise ValueError("local embedding providers cannot have a remote URL")
        if not 0 <= self.semantic_min_similarity <= 1:
            raise ValueError("semantic minimum similarity must be between zero and one")
        if not 128 <= self.guidance_token_budget <= 2000:
            raise ValueError("guidance token budget must be between 128 and 2000")
        for provider_url in (self.embedding_url, self.boundary_llm_url):
            if not provider_url:
                continue
            endpoint = urlsplit(provider_url)
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError(
                    "remote inference endpoints must use HTTPS without URL credentials"
                )
