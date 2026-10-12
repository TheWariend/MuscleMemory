Versioned Alembic migrations are packaged at `packages/musclememory/migrations/`
so `musclememory migrate` also works from an installed wheel outside the source tree.
`alembic.ini` points at the same directory for migration development. The initial
migration contains explicit frozen table definitions, and PostgreSQL-only pgvector,
GIN full-text, and HNSW vector index setup. Application startup never creates tables.

