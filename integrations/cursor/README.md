# Cursor

## Connect

Run this inside your project, with MuscleMemory set up and running
(`musclememory init`, `musclememory start`):

```sh
musclememory connect cursor
```

## What it changes

- `~/.musclememory/keys/cursor.env`: a private key for Cursor, created once.
- `~/.musclememory/hooks/cursor.json`: server address, workspace, key file and queue.
- `.cursor/hooks.json` in your project: hooks for `beforeSubmitPrompt`,
  `preToolUse`, `postToolUse`, `postToolUseFailure` and `stop`. Other hooks are
  kept; a backup is saved if the file changes.

Restart Cursor so it loads the hooks.

## Check it works

Give Cursor's agent a task, then:

- `musclememory status` lists Cursor under connected agents.
- The dashboard's **Activity** page shows the session.
- After a session where tests pass after the last write, **Skills** shows a new
  skill marked **Needs your OK**.

## How Cursor sessions are handled

- Read, Write, Grep, Delete and Shell calls are recorded, including failures.
- Cursor's prompt hook can't return extra context, so memory arrives with the first
  tool result instead.
- The `preToolUse` hook only observes. It always answers "allow", so Cursor's own
  approvals and any other policy hooks still decide what runs.
- On `stop`, a session with a passing test after its last write is verified and
  learned from.

## Known limits

- Parallel tool calls mark a session incomplete.
- Plugin and MCP tools need a mapping under `"tools"` in
  `~/.musclememory/hooks/cursor.json`; unmapped tools mark a gap.
- Tested with vendor-shaped hook payloads, not yet with a live Cursor install.

## Remove it

1. Delete the MuscleMemory entries from `.cursor/hooks.json`.
2. Revoke the Cursor key on the dashboard's **API keys** page.
3. Delete `~/.musclememory/keys/cursor.env` and `~/.musclememory/hooks/cursor.json`.

## MCP (optional)

For explicit memory tools, use [mcp.example.json](mcp.example.json) with a key from
`musclememory key --label cursor-mcp`. MCP alone does not capture Cursor's own
tools; the hooks do that.
