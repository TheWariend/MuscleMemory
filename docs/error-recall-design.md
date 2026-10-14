# Error-triggered warning notes

Status: design only. Standalone warnings before a skill is trusted are not shipped.
Normal skill recall and its approval gate remain in effect.

## Why the existing notes cannot be released directly

`boundaries.derive_boundaries` stores bounded prose in `avoidance_rules` and
`recovery_notes`. It includes reminders from failures without a confirmed fix.
`what_worked` also distinguishes a command correction from a test that passed
after code changed: the latter returns `FIXED` and produces no avoidance note.
These strings do not retain a per-note failure, edit and verifier relationship.

`service.learn` builds contrasts between failed and successful runs in the same
tenant and namespace using capability sequences and value-free action shapes.
That is not an exact error match or proof that a particular failure was fixed.
`retrieval` only releases notes as part of an eligible, approved skill. Bypassing
its trust filter would release unconfirmed notes as well as confirmed ones.

The hook queue knows client sessions; the server stores runs, which can be
individual turns. Counting runs would mislabel `seen_in` as a session count.
These gaps need explicit evidence and protocol contracts before implementation.

## Minimum design

Persist a separate warning record scoped by tenant and namespace. Its exact key
is `(normalized error line, test name, project-relative file)`. Use conservative
extractors for supported test runners: remove ANSI codes, normalize whitespace
and the workspace-root spelling, but retain error values. Abstain on missing,
ambiguous, redacted or truncated signatures; do not fall back to semantic matching.

Each note must retain its source procedure revision and immutable failure,
subsequent code-change and passing-test event IDs. Confirm only an ordered
fail → change → tests-pass sequence in the same workspace, with the same normalized
test command passing after the final code change. Shape contrasts and successful
completion labels alone cannot confirm a note. Never backfill confirmation from
the current prose lists. Expired or deleted evidence must make its note unavailable.

On a captured command failure, a bounded lookup may return only exact matches
whose parent skill has the person's OK. It may allow a provisional skill, while
still excluding withdrawn or flagged sources. Both `recall_enabled` and a new
hook flag, `error_notes_enabled`, must be enabled. Start the new flag disabled.
The lookup must not transmit current file contents or execute remembered actions.

Deliver only `{"warning": "...", "seen_in": N, "status": "unproven"}`. Count
distinct confirmed source sessions using an opaque, stable session identifier;
retries and turns must not inflate N. Keep a transactional local delivery marker
per identity, namespace and client session so concurrent hooks deliver at most one
warning. Steps, edits and full skill definitions must never enter this response.

Before shipping, verify each client's failed-tool context field and define a token
ledger shared with skill pointers. Warning labels and counts are essential; skip
delivery if the complete warning cannot fit the configured remaining budget.

## Required validation

- A confirmed note appears for an exact matching error; failure-only evidence and
  different error lines, tests or files produce nothing.
- Tests before the final edit, different final test commands and expired evidence
  cannot confirm notes.
- Approval, namespace isolation, both config flags and redaction fail closed.
- Repeated failures, concurrent hooks, turns and process restarts deliver at most
  once per session, keep an accurate source-session count and respect the budget.

Implement the signature extractors, evidence storage and migrations, session-count
contract and client delivery protocol together. Until those contracts are tested,
failure notes remain attached to proven skills.
