# How MuscleMemory works

MuscleMemory watches your agent work. When a task really succeeds, it
learns how the task was done. The next time a similar task comes up, it hands the
agent a short pointer: which files to change, how to check the result, and what
to avoid. The agent still decides what to do.

## The loop

```mermaid
flowchart LR
    A["Agent session"] -->|"hooks or SDK capture each tool call"| B["Recorded session"]
    B --> C{"Tests passed after the last change?"}
    C -- no --> D["Kept as history, teaches nothing"]
    C -- yes --> E{"Matches a skill being checked?"}
    E -- yes --> F["Counts as one check"]
    E -- no --> G["New skill, needs your OK"]
    F --> H{"Two passing checks and kept?"}
    G --> H
    H -- yes --> I["Ready"]
    I --> K{"Local hook file check allows delivery?"}
    K -- yes --> J["Compact pointer, with a warning on partial match"]
    K -- no --> L["Skip recorded locally; no pointer"]
```

1. **Capture.** Hooks for Claude Code, Codex and Cursor, the CleanSlate host
   adapter, or the Python and TypeScript SDKs record each tool call: the tool,
   its arguments, its result and its exit status. Secrets are redacted before
   anything is stored.
2. **Verify.** When a session stops, the hook checks one thing: did a test
   command pass after the last code change? Edits through the edit tools count,
   and so does any shell command that changed project files (the hook compares
   which files differ from version control before and after it). If any tool
   call is missing from the capture, or no test ran after the last change, the
   session teaches nothing.
3. **Learn or check.** A later passing session counts as one independent check
   when its observed actions match the skill and its completion evidence passes
   the skill's own stored test after the last code change. Both test commands are
   normalized with `artifacts.test_command`: pipes, redirects and shell wrappers
   are removed before exact comparison. An unrelated passing test does not count.
   Automatic checks use the final passing test as evidence, so running the stored
   test and then a different test does not check that skill. Commands explicitly
   stored as `$mm_parameter` retain argument-shape matching.
   Equivalent edit, patch or write tools can supply file-change evidence across
   agents. The original agent's read-only shell commands (`cat`, `sed -n`, `grep`,
   `git diff`) need not be repeated.
   Otherwise MuscleMemory compiles a new skill from it. The session a skill was
   learned from can never count as one of its checks.
4. **Approve.** Every skill waits for a person's OK, however it was learned
   (hooks, SDK, MCP or the API), and a new version needs a new OK. In the
   dashboard skills show as "Needs your OK" with Keep and Delete buttons.
   Approving needs admin access, which the dashboard has and agent keys don't,
   so an agent cannot approve its own skills.
5. **Ready.** A skill is handed to agents only when it is kept, has at least two
   passing checks, has a smoothed reliability of at least 0.65, has been
   validated in the last 90 days, and needs only tools and permissions the
   agent actually has. Changing a file is one ability whatever the tool is
   called (Codex's patch, Claude Code's Edit and Write, Cursor's Write), so a
   skill learned with one agent is handed to the others; every other tool must
   match exactly.
6. **Recall.** At the start of a session (Cursor: at its first tool result) the
   hook asks for the best match, checks its current files locally, and adds a
   compact pointer when the check allows delivery. A session gets the same skill
   revision at most once and at most two pointers in total. When nothing fits or
   the files no longer match, MuscleMemory adds nothing.

## Workspace scope

Skills are grouped by tenant and workspace (`namespace`), rather than repository.
The hook config's `namespace` defaults to `"default"`; `musclememory init` also
uses that default, and `connect` copies it into the shared config for each agent.
Projects connected to the same server, tenant and namespace share skills, their
checks and approval state, and recorded sessions. Repository roots make captured
paths relative and drive the local file check; they do not partition server recall.

To give a project its own workspace, use a dedicated hook JSON config with a
distinct `namespace`, an API key granting access to that namespace, and a private
queue directory. Point only that project's hooks at the dedicated config; changing
the shared `hooks/<agent>.json` affects every project using it. SDK and MCP clients
choose the same workspace through `visibility_scope`.
See [project workspace setup](install.md#workspace-scope-and-project-isolation).
Cross-repository recall is untested. A coinciding path or matching text does not
establish that a skill is appropriate for another repository.

## What a skill stores

- **Steps** compiled from the successful session, with dead ends and failed
  branches removed. Steps keep their dependency graph.
- **Files read and files changed**, the commands run, and the **test command**
  that proved the result.
- **Applicability conditions**: the capabilities, permissions and environment
  values it needs. Recall skips a skill when any condition is unknown or unmet.
- **Watch-out and recovery notes**, written in plain English from failed steps,
  retries and failure contrasts (below).
- **Evidence**: the source session, every check with its measurements, and the
  revision history.

The pointer sent to an agent is compact JSON, capped at 350 tokens by default. It
contains the title, changed files, the test command, up to four before-and-after
snippets of the key edits, files to read first, preconditions, one watch-out note,
the permissions needed and a notice that the agent must check the current files and
keep its normal approvals. When it is too long, whole optional fields and then edit
snippets are dropped; code is never cut mid-string. The full skill is available through `musclememory_inspect`.

## Checking current files in hooks

Before delivering its top candidate, a native hook checks the current project on
your machine. The root is the nearest parent with a `.git` directory or worktree
file, or the hook's `cwd` outside Git. Captured paths are normalized and normally
relative to that root; absolute paths must still be inside it.

For a skill that changes files, every file it found in place must still exist as a
regular file: files it edited with a stored `before` text, read before changing, or
patched as existing (`Update`/`Delete File`). Files the skill created may not exist
yet. For files that do exist, at least one stored edit must match its literal
`before` or `after` text. If no edit matches, the hook stays quiet. Partial matches carry a `warning` field:
"files changed since this skill was learned: <files>". The renderer never trims
that warning; if essential context cannot fit the token budget, it injects nothing.

The check follows no symlinks, opens at most 32 files, and rejects files larger
than 1 MiB. Missing roots, unreadable files, unsafe paths and other errors skip
delivery. Skills with no stored edits to compare (for example, changes made with a
write tool or from the shell) are delivered once their existing files are present;
skills with no changed files keep their existing delivery behavior. Hook delivery includes
only the checked candidate, rather than a plan containing unchecked skills.
Skip reasons are kept in the private local queue's `skips` table, alongside
`injections`, and purged with their runs. File contents stay on your machine.

This check makes no model calls and runs no commands. It runs in the hooks;
server-side recall and MCP `musclememory_recall` have no project file access and
do not perform it. An agent must still inspect the current files and adapt.

## Learning from failures

- **Failure contrasts.** When a skill is learned, MuscleMemory looks for failed
  sessions in the same workspace that took the same sequence of actions. It
  compares the action shapes (key names and types, never values) and records
  which steps only the working run had, which only the failed run had, and
  which were called differently. These become watch-out and recovery notes, and
  steps unique to the working run are marked critical.
- **Notes before trust are not shipped.** Notes reach agents with eligible,
  approved skills. Exact error lookup needs separate confirmation evidence;
  see the [error-warning design note](error-recall-design.md).
- **One failed check flags a skill.** If a later check fails, the skill is
  flagged and stops reaching agents until a new revision is learned and checked.
  Native hooks report failed checks themselves: a session that was given a skill,
  changed code, and ran the skill's own test last with a failing result is a
  failed check. A session that just stops, or whose last failing test is a
  different one, is not.
- **Did the test catch the bug?** MuscleMemory records whether some session ran the
  skill's test on the original code, saw it fail, and saw it pass after the last change
  (`test_proven` in the skill's details). A failure between edits doesn't count, since
  it may only catch the agent's own half-done change. Skills that write or change their
  own test can't show this and are marked `test_written_by_skill`. This is information
  for the person approving a skill and never slows learning. Setting
  `MM_REQUIRE_PROVEN_TEST=true` makes it a requirement: skills whose test already
  existed then reach agents only once it is proven. Skills learned before this was
  recorded count as proven.
- **Removed skills stay removed.** Deleting or retiring a skill stores a
  fingerprint of what it taught (files, edits and test), never its content.
  Learning the same skill again is refused, and a retired skill takes no new
  versions.
- **Optional refinement.** If you set `MM_BOUNDARY_LLM_URL`, the notes can be
  refined by an HTTPS model endpoint. Only value-free text is sent: commands
  become "a command" and file names become "a file".

## Retrieval

Retrieval picks candidates from several cheap signals, then ranks them.

| Signal | How it works |
| --- | --- |
| Exact | A file path or command from the task appears verbatim in a stored skill. |
| Keywords | BM25 (k1 1.5, b 0.75) over the task, title, files and test command. |
| Meaning | Local MiniLM embeddings (`all-MiniLM-L6-v2`, 384 dimensions), cosine similarity with a floor of 0.40. |
| Recent | Recently updated skills join the pool so new ones are not missed. |

The lists are combined with reciprocal rank fusion (k = 60). A candidate must
reach a relevance of 0.25. The final score is:

```text
score = 0.75 × max(exact match, fused rank) + 0.20 × reliability + 0.05 × freshness
```

Reliability is Laplace-smoothed: (successes + 1) / (checks + 2). Freshness falls
from 1 to 0 over the 90-day freshness window. Related skills are linked when one
produces a file another reads; a file touched by more than 8 skills (lockfiles,
READMEs) creates no links. Default recall makes no model calls.

## Components

```mermaid
flowchart LR
    H["Agent hooks: Claude Code, Codex, Cursor"] --> Q["Private local queue"]
    C["CleanSlate host, Python and TypeScript SDKs"] --> R
    Q --> R["REST API /v1"]
    M["MCP clients"] --> P["MCP /mcp, 5 tools"]
    D["Dashboard"] --> R
    R --> S["Memory service: auth, audit, transactions"]
    P --> S
    S --> L["Compiler and failure contrasts"]
    S --> K["Retrieval"]
    L --> DB[("SQLite or PostgreSQL + pgvector")]
    K --> DB
```

One Python service (FastAPI) serves the REST API, the MCP endpoint with five
tools (`recall`, `observe`, `learn`, `evaluate`, `inspect`) and the dashboard.
Hooks write each event to a private local queue first, so a capture survives a
stopped server and is delivered later with `musclememory hooks flush`.

## Storage

- **SQLite** is the default, in `~/.musclememory/memory.db`. SQLite has no vector
  index, so each server process keeps the current vectors of a workspace as one
  normalized matrix plus an in-memory keyword index. It rebuilds them when skills
  are added or removed. A recall is then a matrix product and a few array lookups.
- **PostgreSQL with pgvector** stores real vectors of any size. A partial HNSW
  index exists for each embedding dimension in use (128 and 384 are created by
  the migrations; `musclememory migrate` adds one for the configured dimension).
  Keyword candidates come from PostgreSQL full-text search.
- Migrations are explicit: run `musclememory migrate`. Recorded sessions expire
  after 30 days by default; purge removes their traces. Skills and evaluation
  records remain until deleted, but original trace events may no longer be available.

## Security

- **API keys** are random 256-bit values, stored only as SHA-256 digests and
  shown once. Each key grants `read`, `write`, `delete` or `admin` on specific
  workspaces (namespaces) of one tenant. Every query filters by tenant and
  workspace before returning data. There is no anonymous access.
- **Dashboard sign-in** can use a one-time code from `musclememory dashboard`.
  The code is stored hashed, works once and expires after 5 minutes; the browser
  swaps it for a dashboard key.
- **Redaction** removes values under secret-looking keys (tokens, passwords, API
  keys) before storage, and secrets cannot become skill parameters.
- **Agent context is untrusted.** Pointers tell the agent to check the current
  files and follow its normal permissions. MuscleMemory never runs anything.
- The server sends a strict Content Security Policy, rejects unknown hosts and
  origins, limits request bodies to 2 MiB and rate-limits each key.

## What it does not do

- It does not replay actions. Agents get guidance, not commands to execute.
- It does not call a model by default. Embeddings run locally; the only remote
  calls are ones you configure (`MM_EMBEDDING_URL`, `MM_BOUNDARY_LLM_URL`).
- It does not learn from sessions with missing tool calls, from sessions where
  no test passed after the last change, or from the session being checked.
- It does not hand a skill to agents before two independent checks and, for
  automatic skills, your OK.
