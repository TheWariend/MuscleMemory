# Claude Code

## Connect

Run this inside your project, with MuscleMemory set up and running
(`musclememory init`, `musclememory start`):

```sh
musclememory connect claude
```

## What it changes

- `~/.musclememory/keys/claude.env`: a private key for Claude Code, created once.
- `~/.musclememory/hooks/claude.json`: server address, workspace, key file and queue.
- `.claude/settings.json` in your project: hooks for `UserPromptSubmit`,
  `PreToolUse`, `PostToolUse`, `PostToolUseFailure` and `Stop`. Your permission
  rules and other hooks are kept; a backup is saved if the file changes.

Restart Claude Code so it loads the hooks.

## Check it works

Give Claude Code a task, then:

- `musclememory status` lists Claude Code under connected agents.
- The dashboard's **Activity** page shows the session.
- After a session where tests pass after the last edit, **Skills** shows a new
  skill marked **Needs your OK**.

## How Claude Code sessions are handled

- Read, Write, Edit, Grep, Glob and Bash calls are recorded, including failures.
- Every prompt starts a new recording, because Claude Code has no turn id.
- Memory is added at the prompt, at most twice per session.
- On `Stop`, a session with a passing test after its last Write or Edit is verified
  and learned from. Otherwise it stays unverified. You can still confirm one by hand:
  `musclememory hooks attest --client claude --config ~/.musclememory/hooks/claude.json
  --run RUN_TOKEN --succeeded`.

## Known limits

- Parallel tool calls mark a session incomplete.
- Plugin and MCP tools need a mapping under `"tools"` in
  `~/.musclememory/hooks/claude.json`; unmapped tools mark a gap.

## Remove it

1. Delete the MuscleMemory entries from `.claude/settings.json`.
2. Revoke the Claude Code key on the dashboard's **API keys** page.
3. Delete `~/.musclememory/keys/claude.env` and `~/.musclememory/hooks/claude.json`.

## MCP (optional)

For explicit memory tools, use [mcp.example.json](mcp.example.json) with a key from
`musclememory key --label claude-mcp`. MCP alone does not capture Claude Code's own
tools; the hooks do that.
