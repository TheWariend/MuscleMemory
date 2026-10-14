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
- Bash commands that change project files count as code changes too: the hook
  compares which files differ from git before and after each one. A test piped into
  another command (`… | tail`) counts only when its output shows the runner's own
  pass or fail line.
- Paths inside the project are stored relative to it, and a leading
  `cd <project> &&` is dropped, so skills match other checkouts.
- Skills learned with Codex or Cursor are handed to Claude Code too, and Claude
  Code's later sessions count as checks when their actions match and the final
  passing test matches the stored test command after normalization. Parameterized
  commands retain shape matching.
- On `Stop`, a session with a passing test after its last code change is verified
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
