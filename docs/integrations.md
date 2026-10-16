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
   change. Test commands include `pytest`, `unittest`, `jest`, `vitest`, `cargo`,
   `go`, `npm test` and `make test`. File changes are edits, writes and patches made
   through the agent's file tools.

## How a session becomes a skill

- **Tests passed after the last change, and the capture is complete:** the session
  counts as verified.
  - If its actions match an existing skill and its final passing test matches the
    skill's stored test command, it counts as one check. Commands are compared after
    normalizing pipes, redirects and shell wrappers; parameterized commands keep
    argument-shape matching. An unrelated final test does not count.
  - Otherwise MuscleMemory learns a new skill from it.
- **Tests failed, no test ran, or the capture has gaps:** nothing is learned.

A skill reaches agents only when both are true:

1. It passed two independent checks (the session it came from does not count).
2. A person approved it. Skills learned automatically show **Needs your OK** in the
   dashboard (`musclememory dashboard`). Choose **Keep** or **Delete**. Skills you
   learn on purpose through the SDK, MCP or `hooks learn` count as approved.

One failed check pauses a skill until it is relearned. Turning a skill off or
deleting it in the dashboard stops it reaching agents right away.

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

- **Codex and shell patches.** When Codex applies a patch through a shell heredoc, it
  can send the "tool started" hook without the "tool finished" hook. The capture is
  marked incomplete and nothing is learned from that session.
- **Parallel tools.** Overlapping tool calls make a capture incomplete, because their
  order and dependencies are unknown.
- **Unknown tools.** Tools without a mapping (plugin or MCP tools) mark a gap. Add
  them under `"tools"` in the hooks file with a capability and an effect (`read`,
  `write` or `destructive`).
- **Interrupted sessions.** A session that is interrupted rather than stopped is
  never verified.
- **No replay.** MuscleMemory only suggests. The agent reads the pointer, checks the
  current files and follows your normal approvals. Remembered steps never run on
  their own.
