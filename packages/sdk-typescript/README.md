# musclememory

TypeScript SDK for [MuscleMemory](https://thewariend.com/musclememory), open-source procedural
memory for AI agents. When a task really succeeds, MuscleMemory remembers how it was done and
hands that to the next agent facing a similar task.

```sh
npm install musclememory
```

The SDK talks to a MuscleMemory server. Run one with the Python package:

```sh
pipx install musclememory
musclememory init && musclememory start   # http://127.0.0.1:8080
musclememory key --label my-agent         # prints an API key once
```

## Record your agent's tool calls

Wrap each tool once. Calls are recorded with their real arguments and results, and secrets are
removed before anything is stored.

```typescript
import {readFile} from 'node:fs/promises';
import {MuscleMemoryClient, ObservedRun} from 'musclememory';

const client = new MuscleMemoryClient('http://127.0.0.1:8080', process.env.MM_API_KEY!);
const profile = {agent_name: 'my-agent', bindings: {'repository.inspect': 'read_file'}};

const run = new ObservedRun(client, 'Inspect repository manifest', profile, 'default');
const read = run.instrument('repository.inspect', (path: string) => readFile(path, 'utf8'),
  {arguments: ([path]) => ({path})});
await read('pyproject.toml');
await run.complete();
```

Runs from the SDK are not learned automatically: your code decides whether a task succeeded and
calls `client.musclememory_learn(...)`, then `client.musclememory_evaluate(...)` for later runs.

## Ask for a skill before a task

```typescript
const recall = await client.musclememory_recall({
  intent_text: 'Inspect repository manifest',
  visibility_scope: 'default',
  runtime_profile: profile,
  match_limit: 1,
});
if (!recall.no_match) console.log(recall.guidance); // the short note to add to your agent's prompt
```

`musclememory/node` adds a durable on-disk capture journal for Node agents.

## Links

- [Integrations](https://github.com/TheWariend/MuscleMemory/blob/main/docs/integrations.md)
- [API reference](https://thewariend.com/musclememory/api)
- [Source](https://github.com/TheWariend/MuscleMemory)

Apache-2.0 · TheWariend
