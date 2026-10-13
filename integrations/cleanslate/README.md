# CleanSlate

CleanSlate connects through its runtime stream, not hooks. MuscleMemory reads the
stream without changing it and gives memory through the host context callback.

## Connect

With MuscleMemory set up and running (`musclememory init`, `musclememory start`):

```sh
musclememory connect cleanslate
```

It creates a key once (`~/.musclememory/keys/cleanslate.env`) and a private host
config (`~/.musclememory/cleanslate.json`), then prints two lines. Run them and
start CleanSlate from the same terminal:

```sh
export CLEANSLATE_MUSCLEMEMORY_CONFIG=~/.musclememory/cleanslate.json
export CLEANSLATE_MUSCLEMEMORY_NODE_MODULE=/path/to/musclememory/packages/sdk-typescript/dist/node.js
```

Build the SDK first if `dist/node.js` is missing (`npm run build --workspace
musclememory`).

## Desktop app and CLI

Both wrap the `run`, `plan`, `approvePlan` and `resumePendingQuestion` streams and load
the SDK only when these variables are set:

- Desktop: [desktop.patch](desktop.patch). Installed apps must be rebuilt from that
  source first. Journals live in CleanSlate's user-data `musclememory-private` folder.
- CLI (one-shot and interactive): [cli.patch](cli.patch). Journals live in
  `~/.cleanslate/musclememory-private` (or `CLEANSLATE_MUSCLEMEMORY_STATE`). The note is
  added after the CLI's own project context.

Both pass the project folder, so code changed from the terminal is seen.

## What is captured

- `tool_start` / `tool_result` pairs, matched by `toolCallId`.
- All 63 tools in CleanSlate SDK 1.0.12, mapped in [config.example.json](config.example.json)
  and exported as `CLEANSLATE_DEFAULT_PROFILE` / `CLEANSLATE_DEFAULT_EFFECTS`.
  Mappings grant no permissions; plugin tools need their own mapping.
- Memory calls through `mcp_call_tool` are skipped.

To check it works, run a task and look at the dashboard's **Activity** page.

## Learning and limits

- Sessions are **learned automatically** with the same rule as the hooks: a test
  passed after the last code change. Edits through `apply_edit`, `write_file` and the
  other file tools count, and so do terminal commands that changed project files (the
  host compares `git status` before and after each `execute_command`, paths only). A
  test piped into another command counts only when its output shows the runner's own
  pass or fail line. A passing session becomes a check on the skill it was handed, or
  a new skill that waits for your OK.
- Pass the project folder as the host's fourth argument (`workspaceRoot`); without
  it, only the file tools count as changes. Paths inside it are stored relative to it.
- The CleanSlate CLI and desktop app wire this up when `CLEANSLATE_MUSCLEMEMORY_CONFIG`
  is set. Custom hosts can call `capture.finishIfVerified(hint)` themselves, or
  `capture.finish(true)` after their own check. See [capture.example.mjs](capture.example.mjs).
- Spawned workers, background commands, and unmapped, overlapping or unfinished
  tools mark a session incomplete.
- Keep the key and journals outside agent workspaces and command sandboxes.

**Remove it:** unset the two variables, revoke the key on the **API keys** page,
and delete `~/.musclememory/cleanslate.json` and `~/.musclememory/keys/cleanslate.env`.
Real-SDK test: `CLEANSLATE_SDK_MODULE=/path/to/@cleanslate/sdk/dist/node/index.js
node examples/cleanslate_capture.mjs`.
