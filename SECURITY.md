# Security and privacy

MuscleMemory persists sanitized execution data and returns untrusted guidance.
Agents must keep their system-level authorization, tool permissions, and human
approval rules. Memory never grants execution authority. There is no replay engine,
shell executor, expression evaluator, or automatic cross-organization sharing.

The persistence boundary and Python/TypeScript observation SDKs redact sensitive field names,
Authorization/Cookie values, common API credential patterns, bearer tokens, JWTs,
private keys, credential assignments, and URL passwords. Compilation refuses
steps depending on redacted values. Prefer allowlisted response summaries and
credential references instead of secrets in arguments. Redaction cannot reliably
recognize arbitrary secrets embedded in innocuous prose; do not submit them.
Payloads are bounded by batch, event, depth, and HTTP size limits. REST validation
errors omit input values. Provider error bodies are not surfaced.

Optional Python journals contain sanitized execution data in private mode-0600
SQLite files; plaintext API credentials are never journaled. Recovery is bound to
the same endpoint and credential identity. Protect parent directories and encrypted
storage/backups, and apply a separate local retention policy. Server memory deletion
does not remove client journals. TypeScript pending events are currently in memory.

The scoped credential determines tenant identity and namespace grants. Retrieval
queries apply tenant/namespace/current-revision filters before returning data.
Ownership is checked for inspection, outcomes, deletion, revisions, and traces.
Tokens are generated with cryptographic randomness and stored as SHA-256 digests;
listing never exposes plaintext or digests. Credential revocation takes effect on
the next request, including stdio calls. Namespace administrators cannot revoke
keys spanning namespaces they do not administer.

Trace evidence, capability/effect declarations, outcomes, and baselines are
caller-provided. Structural consistency checks do not turn them into independent
external proof. Writers can poison memory in their authorized namespace. Scope
write credentials narrowly, review provenance, and use independent task validators
before expanding trust. Parameter materialization uses structured substitution,
not eval or shell interpolation. Treat the resulting arguments as untrusted input.

Use TLS remotely, host/origin allowlists, restricted database roles, encrypted
storage/backups, secret management, and a protected deployment. No application
encryption-at-rest/key management or OAuth/SSO is built in. The dashboard keeps
credentials in tab memory and renders procedure text as escaped React text; it
does not render procedure HTML or inject retrieved instructions into system prompts.

Deletion removes active database records; backups and retained audit metadata
need explicit operator retention policies. Schedule `musclememory purge` for trace
deletion. See [deployment guidance](docs/install.md) and [known limitations](docs/roadmap.md).

For a private vulnerability report, contact TheWariend through its established
private security channel. A dedicated security reporting address is not configured
in this repository; do not publish secrets in a public issue.

