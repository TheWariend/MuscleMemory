# Evidence-linked procedural learning

Automatic learning separates capture, optional model curation, and validation. The default local compiler creates provisional skills from verified recordings without an LLM account. A successful source recording never establishes independent reuse.

Optionally choose **Codex** or **Claude Code** under **Connect & settings → Learn reusable procedures**, or set MM_LEARNING_AGENT=codex (or claude) on a local server. This sends bounded, redacted captured requests, code changes, verification commands, adjacent procedures, and relevant later recorded changes to the selected signed-in agent. It uses that agent's subscription or API billing. **Local only** (stored as `off`) uses the original local compiler and reuse checks. Hosted deployments do not invoke local CLIs. An opted-in curator's rejection or outage never silently falls back to admitting a raw recipe.

The proposer must produce an applicability trigger, a reusable rule, an adaptation for a different related task, and essential actions with source event identifiers. A separate assessment can reject unsupported, temporary, duplicated, obsolete, or insufficiently abstract material. Valid identifiers are enforced in code, but identifier validity alone does not establish semantic truth. Accepted procedures remain provisional: their source episode does not count as independent reuse, and ordinary review and validation gates still apply.

**Proof before generated guidance**

Canonical titles and intent signatures remain derived from the original request. Curation never assigns a display name: its suggested title remains proposal data. Display naming follows the separate Skill names setting. Manual only (stored as `off`) makes no automatic naming calls; the existing skill's **Name with…** button invokes the chosen agent explicitly. Selecting Codex or Claude Code opts into naming new skills automatically. Built file/test labels remain placeholders and do not hide that button; it hides only after a custom name is saved.

Before model-written guidance reaches recall, MCP inspection, or an exported skill, the revision needs at least two passing checks. Every claim must cite a literal source change, and every cited change must clear the existing same-file line-reproduction filter. Missing/pruned evidence, unknown shell changes, test-only citations, or unreproduced lines withhold the entire method rather than dropping an essential step. Generated preconditions and cautions are withheld with it. Human REST inspection retains the complete proposal and source. This checks evidence coverage; it cannot prove semantic entailment or that a repeated change is correct. Review and controlled transfer evaluation remain necessary.

**Workspace policy**

Tenant and namespace remain the default sharing boundary. Source repository identity is retained as provenance for interpreting later corrections, not an implicit recall restriction. Set `MM_LEARNING_PROJECT_ISOLATION=true` to require matching repository identity for newly curated skills. Explicit caller-authored environment requirements are always enforced.

The Codex adapter uses ephemeral execution in an empty working directory, isolated user configuration, disabled shell/app/delegation features, read-only sandboxing, and a structured output schema. Authentication still uses the selected user's CLI login. Prompts and evidence are passed over stdin. Remembered commands are not replayed. The Claude adapter runs without tools or persistent sessions.

**Background work**

Native hooks receive a durable queue acknowledgement within their existing timeout. Curation runs in a background worker. Jobs have leases, survive a server restart, check the owning credential again before processing, and back off after temporary failures. Three failed attempts leave an inspectable error that can be requeued. MM_LEARNING_BACKGROUND=false selects synchronous execution for controlled integrations and tests.

Learning decisions distinguish rejection from model failure. A rejection is cached for the same source evidence and pipeline version. A model outage leaves the source retryable. New later corrections change the evidence identity and allow reconsideration. An admission reserves its source identity transactionally before creating a revision, preventing duplicate admission of concurrent requests.

**Graph**

Memory graph in the dashboard displays versioned skills, lesson claims, recordings, and cited source events. Typed relationships include derived_from, supported_by, corroborates, refines, supersedes, and redistills. A proposal can also supply cited requires or related_to links to scoped existing revisions. Retrieval follows those links only when the target independently passes the normal current-version, validation, review, runtime compatibility, and freshness gates. A link does not grant trust to its target.

Supersession requires explicit correction evidence; recency alone is insufficient. Old versions and source evidence remain inspectable. Semantic corroboration is marked separately from an execution-verified reuse check and does not increment successful execution counts.

Read the bounded graph at GET /v1/graph?visibility_scope=default, optionally adding root=<procedure-id>. GET /v1/learning?visibility_scope=default lists decisions and queued work. These routes require scoped read access. POST /v1/runs/<run-id>/distill?dry_run=true produces a proposal without modifying skills. The existing MCP tool set remains stable; inspection of a distilled skill returns its assessed method and source references within the agent detail budget, while REST retains original tool evidence.

Once a distilled skill passes the usual independent checks, is kept after review, and its cited changes pass the proof filter, exporting it writes its applicability, rule, essential actions, adaptation, and supported cautions into SKILL.md. Export refuses unproved generated guidance. Historical patch hunks remain source evidence rather than becoming the exported instructions.

**Backfill**

Run from this source checkout:

    .venv/bin/python scripts/distill_recorded_skills.py --agent codex --report reports/learning-preview.json

The default preview uses a temporary database copy. It indexes project scope from identity-matched native recordings, revalidates old execution checks, and processes successful recordings in source-time order. It includes later requests and recorded writes so a reversed implementation choice is not confidently restored. Unverified later writes are cautionary evidence, not proof of a new procedure.

To apply the same pipeline to the local installation:

    .venv/bin/python scripts/distill_recorded_skills.py --apply --agent codex --report reports/learning-applied.json

Apply first creates a private SQLite backup and verifies its integrity, then applies the schema migration. Original event payloads remain unchanged. Invalid check rows are retained with an invalidation reason and excluded from validation counts and savings. Legacy trace-derived skills are paused pending semantic curation; accepted refinements receive new versions with fresh validation counters. Rejected and deferred material remains available for inspection or retry. Retired or removed skills are not silently restored.

Paused legacy records without an admitted method cannot be approved or gain new validation credit until curation succeeds. The Skills list previews the curated rule and applicability, while original requests and patch summaries remain under source evidence. Paused records do not count as candidates awaiting approval.

Reports contain private learned text and are written with owner-only permissions. Oversized or unverifiable episodes remain deferred rather than being silently truncated into confident guidance. The backfill is not a benchmark: accepted candidates still need fresh related tasks to demonstrate useful transfer.

To repair metadata from the initial semantic-learning-v3 rollout without model calls or re-curation, preview `scripts/distill_recorded_skills.py --align-contract --report reports/contract-preview.json`, then add `--apply` after reviewing it. Apply backs up first. It restores canonical source titles and removes only implicitly generated project requirements; display names, proposals, source events, versions, trust counters, approvals, and tombstones are preserved.

For names assigned by that rollout's curator bypass, use the same preview/apply workflow with `--align-names`. It clears only stored names equal to the proposal title on old revisions without an explicit naming audit. Separately named skills are preserved. This repair makes no model calls and changes no skill content, source evidence, versions, checks, or review state.

**Activity pagination**

Activity now pages through all retained sessions with Previous and Next. Search and status filters run on the server across the full retained scope. The response includes total, has_more, and status_counts; Home uses the total instead of the first page's length. The per-page API limit remains 100.

**Performance comparison**

Run `benchmarks/compare_learning_contract.py --output reports/learning-comparison.json` with the project's Python environment to compare the committed local baseline with the current checkout. Both arms use separate temporary databases, identical synthetic procedures, cached local embeddings, and alternating run order. It reports local learning latency, recall latency, expected candidate selection, decoy candidate matches, native pointer tokens, and storage. Synthetic approvals are fixture setup, not evidence that real skills are useful. API plan gaps are recorded separately from native pointer sizes.

This offline comparison measures service cost and retrieval behavior. It cannot establish curation quality, coding-task success, paid model cost, or agent time/token savings. Those require related held-out tasks with memory enabled and disabled, matching model/settings, repeated paired runs, and failure/abstention results alongside cost.

CLI isolation and structured output follow the [official Codex command reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli), [configuration feature flags](https://learn.chatgpt.com/docs/config-file/config-basic), and [non-interactive structured output guidance](https://learn.chatgpt.com/docs/non-interactive-mode).
