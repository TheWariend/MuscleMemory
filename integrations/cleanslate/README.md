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
@thewariend/musclememory`).

## Desktop build

The desktop source wraps the `run`, `plan`, `approvePlan` and `resumePendingQuestion`
streams ([desktop.patch](desktop.patch)) and loads the SDK only when these variables
are set. Installed apps must be rebuilt from that source first. Journals live in
CleanSlate's user-data `musclememory-private` folder.

## Cloud worker

The TheWariend development worker uses one key per user from
[worker.credentials.example.json](worker.credentials.example.json), set with
`CLEANSLATE_MUSCLEMEMORY_CREDENTIALS_FILE`, `CLEANSLATE_MUSCLEMEMORY_STATE_ROOT`,
`CLEANSLATE_MUSCLEMEMORY_MODULE` and `CLEANSLATE_MUSCLEMEMORY_NODE_MODULE`. Remote
endpoints must use HTTPS.

## What is captured

- `tool_start` / `tool_result` pairs, matched by `toolCallId`.
- All 63 tools in CleanSlate SDK 1.0.12, mapped in [config.example.json](config.example.json)
  and exported as `CLEANSLATE_DEFAULT_PROFILE` / `CLEANSLATE_DEFAULT_EFFECTS`.
  Mappings grant no permissions; plugin tools need their own mapping.
- Memory calls through `mcp_call_tool` are skipped.

To check it works, run a task and look at the dashboard's **Activity** page.

## Learning and limits

- Sessions are captured and receive memory but are **not learned automatically**:
  the host has no "tests passed" check yet. A host verifier or a person must call
  `capture.finish(true)` first. See [capture.example.mjs](capture.example.mjs).
- Spawned workers, background commands, and unmapped, overlapping or unfinished
  tools mark a session incomplete.
- Keep the key and journals outside agent workspaces and command sandboxes.

**Remove it:** unset the two variables, revoke the key on the **API keys** page,
and delete `~/.musclememory/cleanslate.json` and `~/.musclememory/keys/cleanslate.env`.
Real-SDK test: `CLEANSLATE_SDK_MODULE=/path/to/@cleanslate/sdk/dist/node/index.js
node examples/cleanslate_capture.mjs`.
