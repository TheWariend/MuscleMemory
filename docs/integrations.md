# Integrations

MuscleMemory learns from what your agent actually does. It records the agent's tool
calls, notices when a session ends with passing tests, and turns that session into a
skill. Later sessions on the same work get a short pointer to that skill.

| Agent or runtime | How it connects | What is captured | Memory delivered |
| --- | --- | --- | --- |
| Claude Code | Native hooks (`musclememory connect claude`) | Prompt, Read/Write/Edit/Grep/Glob/Bash calls, failures, stop | At the prompt |
| Codex | Native hooks (`musclememory connect codex`) | Prompt, Bash and `apply_patch` calls, stop | At the prompt |
| Cursor | Native hooks (`musclememory connect cursor`) | Prompt, Read/Write/Grep/Delete/Shell calls, failures, stop | With the first tool result |
| CleanSlate | Host stream (`musclememory connect cleanslate`) | Native runtime stream, 63 mapped tools | Through the host's context callback |
| Any MCP client | MCP over HTTP or stdio | Only what the agent reports with `musclememory_observe` | When the agent calls `musclememory_recall` |
| Python agent | Python SDK (`ObservedRun`) | Calls to tools you wrap | When you call recall |
| Python browser host | `BrowserMemorySession` | Actual actions with before/after browser readback | Grounded against each current observation |
| TypeScript agent | TypeScript SDK (`ObservedRun`) | Calls to tools you wrap | When you call recall |

## Set up once

```sh
musclememory init     # creates ~/.musclememory, the database and the search model
musclememory start    # runs the server in the background on 127.0.0.1:8080
```

Everything private lives in `~/.musclememory` (or `$MM_HOME`) with owner-only
permissions. `musclememory status` shows whether the server runs and which agents
are connected.

## Connect an agent

Run the command inside the project you want the agent to learn from:

```sh
cd /path/to/your/project
musclememory connect codex      # or: claude, cursor, cleanslate
```

It does three things:

1. Creates a key for that agent, once, in `~/.musclememory/keys/<agent>.env`
   (read and write access to your workspace). Later runs reuse it.
2. Writes the hook settings to `~/.musclememory/hooks/<agent>.json`: server address,
   workspace, key file and a private queue folder.
3. Adds MuscleMemory to the project's hook file. Existing hooks and permission rules
   are kept. If the file changes, a private backup is saved next to it.

| Agent | Project file changed |
| --- | --- |
| Claude Code | `.claude/settings.json` |
| Codex | `.codex/hooks.json` (and an empty `.codex/config.toml` if missing) |
| Cursor | `.cursor/hooks.json` |

Restart the agent so it loads the hooks. Codex asks you to review new hooks in its
`/hooks` screen; the command never grants trust for you. CleanSlate is different:
see [its setup](../integrations/cleanslate/README.md).

## What happens during a session

1. **Prompt.** The hook records your request and asks for a matching skill. If one
   is ready, a short pointer (about 350 tokens or less) is added to the agent's
   prompt: the skill, files to change, how to check it worked, and what to avoid.
   A session gets at most two pointers and never the same skill twice. Cursor has no
   prompt output, so it receives the pointer with its first tool result.
2. **Tool calls.** Each call is recorded with its arguments, result, success or
   failure, and timing. Events wait in a private local queue and are uploaded in
   order. If the server is down, they stay queued; `musclememory hooks flush
   --client <agent> --config ~/.musclememory/hooks/<agent>.json` sends them later.
3. **Stop.** MuscleMemory checks whether a test command passed after the last file
   change. Test commands are real test runs: `pytest`, `python -m unittest`,
   Django's `runtests.py` and `manage.py test`, `jest`, `vitest`, `mocha`,
   `cargo test`, `go test`, `npm test` or `npm run test:*`, `make test`/`check`,
   `mvn test`, `gradle test`, `dotnet test`, `rspec` and similar, also behind
   `uv run`, `npx`, `timeout` or environment variables. `node --test` and a test
   file run directly count too (`python3 test_login.py`, `node auth.test.mjs`).
   Commands that only mention tests (`cargo build`, `ruff check`, `echo test`) and
   type checks (`tsc`, `npm run typecheck`) don't count. File changes are edits, writes and patches made
   through the agent's file tools.

## How a session becomes a skill

- **Tests passed after the last change, and the capture is complete:** the session
  counts as verified.
  - If its actions match an existing skill and its final passing test matches the
    skill's stored test command, it counts as one check. Commands are compared after
    normalizing pipes, redirects and shell wrappers; parameterized commands keep
    argument-shape matching. An unrelated final test does not count.
  - Otherwise, when a learning agent is selected in **Connect & settings**, the
    recording is queued for semantic distillation. A proposer and a separate
    assessment can reject weak material, refine an existing procedure, or admit
    a new provisional procedure with cited evidence. The hook returns before
    inference starts. With model curation off, the local compiler still learns
    provisional skills without model calls. Neither path bypasses verification,
    independent checks, approval, or removed-skill fingerprints.
- **Tests failed:** nothing is learned. If the session was given a skill, changed
  code, and that skill's own test was the last test and failed, it counts as a
  failed check for that skill.
- **No verified outcome:** the recording closes as **Not verified**; nothing is learned.
  A new prompt also closes an interrupted previous turn in the same session.
- **Capture gaps:** the recording closes as **Incomplete** and shows the capture reason;
  nothing is learned.

For older hook queues with sessions stuck **In progress**, run:

```sh
musclememory hooks repair --client claude --config ~/.musclememory/hooks/claude.json
musclememory hooks repair --client codex --config ~/.musclememory/hooks/codex.json
musclememory hooks repair --client cursor --config ~/.musclememory/hooks/cursor.json
```

These commands close ended turns and turns
superseded by a newer prompt in the same session, then delivers pending events.
It preserves existing events, never infers success, and leaves the latest open
turn in each session alone. Delivery can be retried with `hooks flush`.

New native and SDK recordings preserve the original start time when uploaded
after an outage. Timestamp recovery for older local SQLite backlog records is a
source-checkout utility, not part of the pip/pipx distribution. From the repository
root of a checkout installed with `pip install -e .`, preview recovery with:

```sh
python scripts/repair_activity_timestamps.py
python scripts/repair_activity_timestamps.py --apply
```

The second command creates a private database backup and restores start times
from matching local hook queue records. It checks the endpoint, credential,
workspace, runtime and intent, and preserves events, outcomes and retention.

To recover skills from older Codex recordings that were left incomplete or not
verified, preview native receipt recovery from the editable source checkout:

```sh
python scripts/recover_recorded_skills.py --report /tmp/skill-recovery-preview.json
python scripts/recover_recorded_skills.py --apply --report /tmp/skill-recovery-applied.json
```

Apply creates a private, integrity-checked SQLite backup first. Original recordings
remain unchanged. Recovered copies link to their source run and use stable IDs,
so retrying does not create duplicate skills or independent checks. Actual native
session, turn, tool results, execution directories and timing must establish that
tests passed after the recorded code changes in the same project. Only the proven
coding segment is learned; later browser or publishing actions are not included.
External actions inside that segment, missing results and untested changes remain
unrecovered, with counts in the report. Deleted skills remain deleted. Recovered
skills follow the usual approval and independent-check requirements.

Native hooks also attempt this recovery at Stop and at the next prompt, because
Codex can write the final native completion after the Stop hook has already run.
No old commands are executed by recovery.

A skill reaches agents only when both are true:

1. It passed two independent checks (the session it came from does not count).
2. A person approved it. Every skill shows **Needs your OK** in the dashboard
   (`musclememory dashboard`) until you choose **Keep** or **Delete**, whether it
   was learned by the hooks, the SDK, MCP or `hooks learn`. Agent keys cannot
   approve.

One failed check pauses a skill until it is relearned. Turning a skill off or
deleting it in the dashboard stops it reaching agents right away, and the same
skill is not learned again.

Native hooks also check the current project before delivery: files the skill found
in place must still exist (files it created may not yet), and at least one stored
edit must match its before or after text.
Complete mismatches stay quiet; partial matches carry an untrimmed warning.
File-check skips are recorded locally. Server, MCP, SDK and CleanSlate recall do
not run this probe. See [the exact file-check rules](how-it-works.md#checking-current-files-in-hooks)
and [workspace isolation](install.md#workspace-scope-and-project-isolation).

Set `"auto_learn": false` in `~/.musclememory/hooks/<agent>.json` to capture
without learning, or `"recall_enabled": false` to capture without injecting memory.

## Export skills to your agents

Codex and Claude Code read the same skill format: a folder with a `SKILL.md` in
`~/.codex/skills` or `~/.claude/skills`. A Ready MuscleMemory skill can be written
there, so the agent can use it even without MuscleMemory connected.

```sh
musclememory export SKILL_ID --to codex
```

The dashboard has **Add to Codex** / **Add to Claude Code** on every Ready skill.

- Exported skills are named `musclememory-…` and contain the original request, the
  files to look at, the exact edits, the test command and the watch-out notes.
- Exporting never replaces a different skill with the same name in an agent's folder.
- Exporting needs an admin key and works only on a local installation. `CODEX_HOME`
  and `CLAUDE_CONFIG_DIR` are honoured.

## MCP

MCP gives an agent five memory tools: `musclememory_recall`, `musclememory_observe`,
`musclememory_learn`, `musclememory_evaluate` and `musclememory_inspect`. Their
inputs match the Python and TypeScript SDKs (see [contracts](contracts.md)).

Connecting MCP alone does **not** capture the agent's other tools. Use the hooks
above for automatic capture, or have the agent report steps with
`musclememory_observe`.

- HTTP: `http://127.0.0.1:8080/mcp` with `Authorization: Bearer <key>`.
- Stdio: `musclememory stdio` with `MM_API_KEY` set, or `musclememory stdio --url
  http://127.0.0.1:8080` to forward to the running server.

Create a key for an MCP client with `musclememory key --label my-agent`. Example
client files are in `integrations/claude-code`, `integrations/codex` and
`integrations/cursor` (`mcp.example.json`, `config.example.toml`).

## SDKs

Use the SDKs when you own the agent's tools. You wrap each tool once; calls are
captured with their real arguments and results.

Python (`examples/durable_capture.py`):

```python
from musclememory.observer import ObservedRun

run = ObservedRun(client, "Inspect repository manifest", profile, "default",
                  spool_path=".capture-spool/manifest.sqlite")
read = run.instrument("repository.inspect")(read_file)
try:
    read(path="pyproject.toml")
    run.complete()   # saves the completion, then uploads
finally:
    run.close()
```

TypeScript (`examples/typescript_capture.mjs`):

```typescript
import {MuscleMemoryClient, ObservedRun} from '@thewariend/musclememory';

const run = new ObservedRun(client, 'Inspect repository manifest', {
  agent_name: 'my-agent', bindings: {'repository.inspect': 'read_file'},
}, 'default');
const read = run.instrument('repository.inspect', readFile, {arguments: ([path]) => ({path})});
await read('pyproject.toml');
await run.complete();
```

SDK runs are not learned automatically. Your code decides whether a task succeeded
and calls `musclememory_learn`, then `musclememory_evaluate` for later runs.

## Browser agents

MuscleMemory ships a browser MCP server for agents that browse: Claude Code, Codex,
Cursor or any MCP client. It is a normal browser for the agent, and it learns while
the agent works.

```sh
pip install "thewariend-musclememory[browser]"
playwright install chromium   # skip if Google Chrome is installed
musclememory connect claude --browser   # or codex, cursor
```

`--browser` adds the server next to the hooks: `.mcp.json` for Claude Code,
`.codex/config.toml` for Codex, `.cursor/mcp.json` for Cursor. Existing entries are
kept and a backup is saved. Restart the agent.

**Tools**

| Tool | What it does |
| --- | --- |
| `browser_open(url, task)` | Opens a page. The first call of a task needs a one-line `task`. |
| `browser_read()` | Reads the page: text, notices and controls with fresh refs. |
| `browser_act(ref, action, value)` | Fills text, selects an option by label, or clicks. |
| `musclememory_find_skills()` | Proven skills for the task, with their start page and inputs. |
| `musclememory_run_skill(skill, inputs)` | Carries out a proven skill on the open page. |
| `browser_done(succeeded)` | Finishes the task, so MuscleMemory can learn or count a check. |

**How a browser skill is learned and trusted**

1. The agent does a task. Every action is recorded with the page before and after.
2. `browser_done(succeeded=True)` counts only if the final click visibly changed the
   site: a new message appeared or a new page opened. Otherwise nothing is learned.
3. The run becomes a skill. Every value the agent entered becomes an input, named
   after its field. A value used twice (an email and its confirmation) is one input.
4. Two later runs of the same task that end with the same result count as its checks,
   whether the agent did them by hand or with the skill.
5. A person approves it in the dashboard, unless approval is automatic
   (`MM_AUTO_APPROVE=true`). The two checks are required either way.

One failed run with a skill flags it, as with coding skills. Deleted or retired
skills are not learned again.

**Running a skill**

`musclememory_run_skill` does the skill's steps on the current page and checks each
one. It only enters the inputs the agent passed, never values from an earlier visit.
It needs the skill's page and exactly one matching control for every step, reads back
every value, and confirms each click moved the page on. It never does the final
step: the agent checks the page and submits. A field it can't find (maybe renamed)
is left for the agent with the value it needs; after filling it, the agent calls the
skill again and it carries on. Another page, two matching controls, a value that
doesn't stick or a click that doesn't move the page on stops it, with the reason.

**Limits**

Pages are read from their visible form controls and `aria-label` sections. Iframes,
shadow DOM, canvas-only pages and screenshots aren't supported. A site's success
message is the evidence a run worked; a message alone can't prove the saved record
is right, so approve skills for important forms with care.

### From your own browser code

`musclememory.browser_session.BrowserMemorySession` connects a browser controller you
own to the Python SDK: record actions with before and after `BrowserState`
observations, get current-page guidance, and learn after your own domain check.
`musclememory.browser.carry_out(definition, values, observe, act)` runs a proven
skill through your controller with the same checks as the MCP server.

## Capture details

- **Redaction.** Hooks and SDKs redact secrets locally before anything is stored or
  sent. The server sanitizes again. Prefer short result summaries: secrets inside
  ordinary prose can't always be recognized.
- **Limits.** A session holds up to 1,024 events of at most 64 KiB each. Data that is
  too large or not JSON is replaced with `$mm_capture_omitted`, and steps with
  omitted arguments are never learned.
- **Durable queues.** Hooks keep events in a private SQLite queue per agent. The
  Python SDK can write a private journal (`spool_path`), and Node can use
  `NodeCaptureJournal` from `@thewariend/musclememory/node`. Journals are bound to
  one server and key; recovery resends saved events and **never repeats tool
  actions**.
- **Cleanup.** `musclememory hooks purge --client <agent> --config <hooks file>
  --older-days 7` removes old uploaded sessions from the local queue and keeps
  anything not yet sent.

## Known limits

- **Codex command results.** Desktop hooks sometimes omit a command's exit status or
  its finish hook. MuscleMemory reads completed native execution records from the
  local transcript, matching the session, turn, call ID, command, and working directory.
  Codex may publish that record after the finish hook. The result stays in the
  private queue until a later hook can finalize it, before its first upload.
  A missing finish hook can be recovered before the next action or at Stop, without
  running the command again. Missing or mismatched native records remain unverified;
  an unfinished action without a matching result still makes capture incomplete.
- **Parallel tools.** Reads and edits of distinct known files can overlap. When
  potentially conflicting actions overlap and all results arrive, the recording is
  complete but not verified: their dependencies are unknown, so it cannot teach a
  skill from the hook record alone. Native recovery can recover a linked coding
  segment when complete native results prove that the final test followed its
  changes without overlapping any potential mutation. An action whose result is
  missing still makes capture incomplete.
- **Desktop browser scripts.** CUA scripts are recorded as potentially destructive
  browser actions, including their inputs and results. A successful script or a
  passing local test does not verify a browser task, so these sessions remain not
  verified and do not automatically teach coding skills. Use the browser adapter
  for browser skill learning and task verification.
- **Unknown tools.** Tools without a mapping (plugin or MCP tools) mark a gap. Add
  them under `"tools"` in the hooks file with a capability and an effect (`read`,
  `write` or `destructive`). Known GitHub reads and client context helpers are
  excluded from coding workflows. Unknown mutations are never assumed to be reads.
- **Interrupted sessions.** A session that is interrupted rather than stopped is
  never verified.
- **No replay of old sessions.** For coding, MuscleMemory only suggests: the agent reads
  the pointer, checks the current files and follows your normal approvals. In the
  browser, a proven skill runs only when the agent calls it, step by step with
  checks, and the agent always does the final submit.
