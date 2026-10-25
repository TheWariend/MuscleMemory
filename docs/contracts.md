# MuscleMemory contract v1

`packages/musclememory/core.py` is the authoritative typed schema. REST OpenAPI is
available at `/openapi.json` and checked into `docs/openapi.json`. Run
`python scripts/generate_contracts.py` to regenerate the TypeScript input types
and OpenAPI snapshot. Unknown model fields are rejected. MCP advertises contract
version through tool metadata and response fields, independently of MCP version.

| MCP tool | REST operation | Inputs |
| --- | --- | --- |
| `musclememory_recall` | `POST /v1/recall` | `intent_text`, `runtime_profile`, `visibility_scope`, `match_limit` |
| `musclememory_observe` | `POST /v1/observe` | `run_token`, `event_batch`, `runtime_profile`, `visibility_scope` |
| `musclememory_learn` | `POST /v1/learn` | `run_token`, `completion_proof`, `learning_policy` |
| `musclememory_evaluate` | `POST /v1/evaluate` | `procedure_token`, `run_token`, `outcome_report`, `performance_metrics` |
| `musclememory_inspect` | `POST /v1/inspect` | `procedure_token`, `revision_selector` |

Recall returns ranked candidates, definition/steps/preconditions, relevance
explanations, capabilities, resolved tools, confidence, sample counts, and an
explicit `no_match`. Observe reports accepted, rejected, duplicate counts, field-
level errors, and trace reference. Learn returns procedure identity/revision,
steps, provisional validation state, and warnings. Evaluate returns statistics,
validation state, and regression warnings. Inspect returns revision history,
definition/provenance, bounded evaluation history, timestamps, and staleness.

Procedure definitions include optional fields with empty defaults:
`step_graph`, `capability_signature`, `applicability_conditions`, `risk_cues`,
`avoidance_rules`, `recovery_notes` and `experience` (files read and written,
commands, verification commands). Steps add `critical: false`. Graph nodes are step
tokens; edges are `[dependency, dependent]`. Multiple parents are all required, and
independent siblings represent parallel branches.

Applicability lists use enforceable strings: `capability:repository.inspect`,
`permission:workspace.read`, `environment:os`. Environment conditions test key
presence; `environment_requirements` test declared values exactly. Unsupported
conditions are never assumed true, so the skill is skipped. The file-change
capabilities `filesystem.patch`, `filesystem.edit` and `filesystem.write` satisfy one
another; every other capability must be bound exactly. Avoidance and recovery
notes are plain-English guidance, not executable conditions or permission grants.

Recall returns `vector_mode` (`semantic` for the local or remote embedding model,
`lexical-hash` for hashed vectors), an `embedding` description, related skills, an
optional multi-step `plan`, and `guidance`: the compact pointer an agent receives.
Inspect adds bounded contrast history linking failed and successful runs, with
success-only and failure-only action shapes and argument-shape differences.

The five MCP tool names and their v1 inputs are stable. Recall has no run-token input;
REST callers may bind a recall to an open run with the `X-MuscleMemory-Run` header.

## Runtime and event example

Canonical capabilities are extensible dotted identifiers such as
`repository.inspect`, `repository.modify`, `test.execute`, or `artifact.publish`.
The binding's concrete tool name must match the observed event. Environment and
permission requirements are explicit declarations, not permission grants.

```json
{
  "run_token": "manifest-check-001",
  "visibility_scope": "default",
  "runtime_profile": {
    "agent_name": "my-agent",
    "bindings": {"repository.inspect": "read_file"},
    "environment": {"os": "linux"},
    "permissions": ["repository.read"]
  },
  "event_batch": [
    {"event_id": "intent", "sequence": 0, "kind": "intent", "intent_text": "Inspect project manifest"},
    {"event_id": "read", "sequence": 1, "kind": "tool", "tool_name": "read_file", "capability": "repository.inspect", "arguments": {"path": "pyproject.toml"}, "response": {"valid": true}, "permission": "repository.read", "duration_ms": 12},
    {"event_id": "done", "sequence": 2, "kind": "completion", "status": "success"}
  ]
}
```

Tool events can declare dependencies, `retry_of`, exploratory status, effect,
condition, duration, and token usage. Dependency IDs must precede the event.
Retries must reference a preceding failed event. Omitted token usage is unknown.
The SDK captures tool calls/durations automatically; upstream token instrumentation
is required for tokens. A completion proof needs successful tool evidence IDs.

Events may include `occurred_at`, an ISO 8601 timestamp with a timezone. On the
initial intent, this preserves the session's start time across queued delivery.
Activity sorts by that start time; a future client timestamp is capped at receipt
time. Older producers that omit it keep the server's receipt time. This timestamp
does not verify the outcome or extend trace retention.

On a completion event, `capture_complete: false` closes the run as `incomplete`.
Otherwise, `outcome_verified: false` closes it as `unverified`; a complete recording
does not establish task success. Verified completions use their `success` or
`failure` status. Neither incomplete nor unverified completions can prove learning.
Run details expose `capture_reason` and, when recorded, a structured `capture_gaps`
array. Consumers should prefer the array over parsing the human-readable reason;
older recordings may only contain the reason string.

Python and TypeScript observation wrappers use this same event contract.
`$mm_capture_omitted` marks unsupported/oversized captured data; steps with omitted
arguments cannot be compiled. The marker is data, never executable code. See
[integrations](integrations.md#capture-details) for batching, durability, and lifecycle rules.

Parameters are declared under `learning_policy.parameters` using a `definition`
(kind/description) and `observed_value`. Supported kinds are string, integer,
number, boolean, object, and array. The compiler emits an internal data marker
`{"$mm_parameter":"name"}`. `materialize()` validates parameters and substitutes
data without running code. Conditions compare a declared parameter to a scalar
value. An omitted conditional dependency produces an error; agents must adapt.

Metrics contain `observed` and optional `baseline`, each with actual tool calls,
duration, and optional tokens/cost. Calls/duration and fully available token counts
are checked against the recorded trace. Baseline comparability and model cost are
client-reported. Savings are paired differences, can be negative, and are never
inferred when missing. Evaluations require the applied `procedure_version`.

## Other REST operations

`POST /v1/runs/{run}/memory` records a hook's memory-delivery report with namespace
write access. Its lookup token is idempotent; conflicting retries are rejected.
Status is `delivered`, `no_match`, `blocked`, `error`, or `disabled`. Reports carry
reason codes, an optional skill/version, and delivered context token count, never
file contents or tool arguments. Run details include the latest 20 reports;
session listings include the latest one. Skill proof and workspace readiness are
snapshots taken by the server at report time. Readiness counts describe the
workspace, not whether those candidates match the task. Reports are client-reported
delivery observations and never count as validation, approval, or application
evidence. They are deleted with their run. Hooks queue reports during outages and
retry them; missing or expired runs cannot block newer reports.

MCP inspection applies the existing proof filter before responding. When full
detail exceeds its 4,000-token budget, it returns a compact summary and the
authenticated REST detail path. Whole optional fields or methods are omitted
with an explicit truncation marker; instructions are never cut mid-sentence to
fit. Full human-review records over REST are unchanged.

`POST /v1/runs` creates an execution record; MCP observe can create one from a
sequence-zero intent. `POST /v1/runs/{run}/auto-learn` turns a session whose tests
passed into a check for a matching skill, or a new skill that waits for approval;
native hooks call it when a session stops. `POST /v1/runs/{run}/auto-flag?hint={id}`
records a failed check when that skill's own test failed last after a code change;
native hooks call it for a session that was given the skill. `POST
/v1/procedures/{id}/approve` is the dashboard's Keep and needs admin access. `POST /auth/sign-in` swaps a one-time code from
`musclememory dashboard` for a dashboard key. `GET /v1/runs` and `/v1/procedures` accept namespace, bounded
limit, and offset. Run/procedure detail, procedure revisions, retirement, memory
deletion, namespace usage, session grants, key listing, and revocation have
documented OpenAPI routes. Credential creation is an administrative CLI action.

REST failures use `{"error":{"code":"...","message":"..."}}`; invalid input
reports field paths without input values. MCP domain errors become proper
`isError` tool results through the official SDK. Authentication failures use 401,
namespace denial 403, absent tenant-owned records 404, conflicts 409, expired
traces 410, validation 422, rate limits 429, and provider failure 503.

