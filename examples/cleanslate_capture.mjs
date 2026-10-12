// Real CleanSlate runtime and tool execution with a scripted provider for a repeatable,
// free integration check. This measures integration correctness, not model quality.
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {MuscleMemoryClient, CleanSlateCapture} from '../packages/sdk-typescript/dist/index.js';

const sdkModule = process.env.CLEANSLATE_SDK_MODULE;
const sdk = await import(sdkModule ? pathToFileURL(sdkModule).href : '@cleanslate/sdk/node');
const {CleanSlateNodeAgentRuntime, createNodeProviderConfiguration, CLEANSLATE_GENERAL_PROFILE} = sdk;
const root = await fs.mkdtemp(path.join(os.tmpdir(), 'musclememory-cleanslate-'));
const manifest = path.join(root, 'manifest.json');
await fs.writeFile(manifest, JSON.stringify({name: 'musclememory', verified: true}));
const client = new MuscleMemoryClient(process.env.MM_API_URL ?? 'http://127.0.0.1:8080', process.env.MM_API_KEY);
const namespace = process.env.MM_NAMESPACE ?? 'team';
let requests = 0, reads = 0;
const provider = options => listener => {
  requests++;
  const timer = setTimeout(() => {
    const parts = requests === 1 ? [{type: 'tool_call', call: {id: 'read-manifest-1', toolName: 'memory_read_manifest', input: {path: manifest}}}] :
      [{type: 'text', content: 'The manifest was read.', phase: 'final_answer'}];
    for (const part of parts) listener(`data: ${JSON.stringify(part)}\n\n`);
    listener(null);
  }, 0);
  return {dispose: () => clearTimeout(timer)};
};
const profile = {agent_name: 'cleanslate-native-runtime', bindings: {'repository.inspect': 'memory_read_manifest'}};
const capture = new CleanSlateCapture(client, 'Inspect repository manifest', profile, namespace,
  {effects: {memory_read_manifest: 'read'}});
const runtime = new CleanSlateNodeAgentRuntime({rootPath: root, workspaceStorageHome: path.join(root, 'state'),
  domainProfile: CLEANSLATE_GENERAL_PROFILE,
  mainService: {getModelsDevModelMetadata: async () => undefined, openAICompatibleChatStream: provider, openAIResponsesStream: provider},
  configuration: createNodeProviderConfiguration({provider: 'openai', model: 'scripted-integration-provider', apiKey: 'test', maxTurns: 3}),
  tools: [{name: 'memory_read_manifest', category: 'file', description: 'Read the test manifest.',
    parametersSchema: {type: 'object', properties: {path: {type: 'string'}}, required: ['path']},
    run: async input => { reads++; return {success: true, content: JSON.parse(await fs.readFile(input.path, 'utf8'))}; }}],
});
try {
  for await (const _ of capture.capture(runtime.run('Read the manifest with memory_read_manifest.'))) {}
  const tool = capture.run.events.find(event => event.kind === 'tool');
  assert.equal(reads, 1);
  assert.equal(tool.response.content.verified, true);
  assert.deepEqual(capture.capture_gaps, []);
  // This explicit host assertion establishes the narrow outcome, not task_complete.
  await capture.finish(true);
  const learned = await client.musclememory_learn({run_token: capture.run.run_token,
    completion_proof: {succeeded: true, evidence_event_ids: [tool.event_id], verifier: 'tool_assertion', summary: 'Actual manifest read returned verified=true'},
    learning_policy: {title: 'Inspect repository manifest'}});
  console.log(JSON.stringify({run_token: capture.run.run_token, procedure_token: learned.procedure_token,
    real_tool_reads: reads, scripted_provider_requests: requests, capture_gaps: capture.capture_gaps}));
} finally { runtime.dispose(); await fs.rm(root, {recursive: true, force: true}); }
