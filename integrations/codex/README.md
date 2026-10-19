# Codex

## Connect

Run this inside your project, with MuscleMemory set up and running
(`musclememory init`, `musclememory start`):

```sh
musclememory connect codex
```

## What it changes

- `~/.musclememory/keys/codex.env`: a private key for Codex, created once.
- `~/.musclememory/hooks/codex.json`: server address, workspace, key file and queue.
- `.codex/hooks.json` in your project: hooks for `UserPromptSubmit`, `PreToolUse`,
  `PostToolUse` and `Stop`. Other hooks are kept; a backup is saved if the file
  changes.
- `.codex/config.toml` in your project, empty, only if it didn't exist.

Restart Codex and approve the new hooks in its `/hooks` screen. The command never
grants trust for you.

## Check it works

Give Codex a task, then:

- `musclememory status` lists Codex under connected agents.
- The dashboard's **Activity** page shows the session.
- After a session where tests pass after the last change, **Skills** shows a new
  skill marked **Needs your OK**.

## How Codex sessions are handled

- Bash and `apply_patch` calls are recorded. Exit codes are read from Codex's
  "Process exited with code N" output.
- Memory is added at the prompt, at most twice per session.
- Codex's own background prompts, such as task suggestions, run in ephemeral threads
  without a transcript. They are not recorded and get no memory.
- On `Stop`, a session with a passing test after its last patch is verified and
  learned from. Otherwise it is closed as unverified and nothing is learned.

## Known limits

- When Codex applies a patch through a shell heredoc, it can send "tool started"
  without "tool finished". That session is marked incomplete and not learned from.
- Parallel tool calls also mark a session incomplete.

## Remove it

1. Delete the MuscleMemory entries from `.codex/hooks.json`.
2. Revoke the Codex key on the dashboard's **API keys** page.
3. Delete `~/.musclememory/keys/codex.env` and `~/.musclememory/hooks/codex.json`.

## MCP (optional)

For explicit memory tools, add the entry from [config.example.toml](config.example.toml)
with a key from `musclememory key --label codex-mcp`. MCP alone does not capture
Codex's own tools; the hooks do that. The live smoke test runs only on request:
`MM_TEST_CODEX=1 .venv/bin/python -m pytest -q tests/test_native_codex.py`.
