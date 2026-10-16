import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {MuscleMemoryClient, ObservedRun} from '../packages/sdk-typescript/dist/index.js';

/** Execute real reads; capture, learn, independently evaluate, and recall via REST. */
export async function workflow(client, namespace = 'default', source = fileURLToPath(new URL('../pyproject.toml', import.meta.url))) {
  const profile = {agent_name: 'typescript-tool-agent', bindings: {'repository.inspect': 'fs.read_manifest'}};
  let learned;
  for (let attempt = 0; attempt < 3; attempt++) {
    const run = new ObservedRun(client, 'Inspect repository manifest', profile, namespace);
    const read = run.instrument('repository.inspect', async path => {
      const manifest = await readFile(path, 'utf8');
      if (!manifest.includes('[project]')) throw new Error('Manifest has no project section');
      return {has_project: true};
    }, {arguments: ([path]) => ({path})});
    await read(source);
    await run.complete();
    if (attempt === 0) {
      learned = await client.musclememory_learn({run_token: run.run_token,
        completion_proof: {succeeded: true, evidence_event_ids: ['e1'], summary: 'Project section present'},
        learning_policy: {parameters: {manifest_path: {definition: {kind: 'string'}, observed_value: source}}}});
    } else {
      await client.musclememory_evaluate({procedure_token: learned.procedure_token, run_token: run.run_token,
        outcome_report: {succeeded: true, procedure_version: 1, evidence_event_ids: ['e1']},
        performance_metrics: {observed: run.measurements()}});
    }
  }
  // Every skill waits for a person's OK; approving needs an admin key.
  await client.approve(learned.procedure_token);
  const recall = await client.musclememory_recall({intent_text: 'Inspect repository manifest', visibility_scope: namespace,
    runtime_profile: {agent_name: 'alternate-tool-agent', bindings: {'repository.inspect': 'workspace.read'}}, match_limit: 1});
  if (recall.no_match) throw new Error('Validated procedure was not recalled');
  return {procedure_token: learned.procedure_token, independent_successes: recall.candidates[0].successes,
    resolved_tools: recall.candidates[0].resolved_tools};
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  if (!process.env.MM_API_KEY) throw new Error('Set MM_API_KEY to a scoped credential');
  const client = new MuscleMemoryClient(process.env.MM_API_URL ?? 'http://127.0.0.1:8080', process.env.MM_API_KEY);
  console.log(JSON.stringify(await workflow(client, process.env.MM_NAMESPACE ?? 'default'), null, 2));
}
