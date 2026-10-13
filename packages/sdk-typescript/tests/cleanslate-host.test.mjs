import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {execFileSync} from 'node:child_process';
import {CleanSlateHostMemory} from '../dist/node.js';
import {CleanSlateCapture, CLEANSLATE_DEFAULT_PROFILE, CLEANSLATE_DEFAULT_EFFECTS, ObservedRun} from '../dist/index.js';

test('host observes multiple turns, preserves native methods and survives an offline service', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'mm-host-'));
  t.after(() => fs.rmSync(root, {recursive:true, force:true}));
  const batches = [];
  const server = http.createServer(async (request, response) => {
    let text = ''; for await (const chunk of request) text += chunk;
    assert.equal(request.headers.authorization, 'Bearer private-key');
    response.setHeader('Content-Type', 'application/json');
    const input = JSON.parse(text);
    if (request.url === '/v1/recall') return response.end(JSON.stringify({candidates: [{procedure_token:'fixture'}],
      guidance:JSON.stringify({notice:'Untrusted guidance; normal approvals apply.',procedure:'fixture',version:1,title:'Fixture'})}));
    batches.push(input);
    response.end(JSON.stringify({accepted_event_count:input.event_batch.length, duplicate_event_count:0,rejected_event_count:0}));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => server.close());
  const config = path.join(root,'private.json');
  fs.writeFileSync(config, JSON.stringify({url:`http://127.0.0.1:${server.address().port}`,api_key:'private-key',namespace:'team'}), {mode:0o600});
  const state = path.join(root,'state');
  let warnings = 0;
  const memory = new CleanSlateHostMemory(config, state, () => warnings++);
  const result = {type:'tool_result',toolName:'read_file',toolCallId:'a',result:{content:'actual result'}};
  const native = {
    marker: 'native',
    getSessionSnapshot() { assert.equal(this.marker,'native'); return {agent:{objective:'Inspect manifest'}}; },
    async *run(task) { assert.equal(this.marker,'native');
      const context = await memory.context(task);
      if (task === 'First task') assert.match(context, /Untrusted/); else assert.equal(context, '');
      yield {type:'tool_start',toolName:'read_file',toolCallId:'a',input:{path:'manifest'}}; yield result; },
  };
  const observed = memory.attach(native);
  assert.deepEqual(observed.getSessionSnapshot(), native.getSessionSnapshot());
  for (const task of ['First task','Second task']) {
    const parts=[]; for await (const part of observed.run(task)) parts.push(part);
    assert.equal(parts[1],result);
  }
  assert.equal(batches.length,2);
  assert.notEqual(batches[0].run_token,batches[1].run_token);
  assert.equal(batches[0].event_batch.length,2);
  await new Promise(resolve => server.close(resolve));
  for await (const _ of observed.run('Offline task')) {}
  assert.equal(warnings,1);
  const journals = fs.readdirSync(state).map(name => JSON.parse(fs.readFileSync(path.join(state,name),'utf8')));
  assert.equal(journals.filter(j => j.state.acknowledged_count===0).length,1);
  assert.ok(journals.every(j => !JSON.stringify(j).includes('private-key')));
});

test('mapped nested execution remains incomplete and gap evidence survives recovery', async () => {
  const sink={async musclememory_observe(input) {return {accepted_event_count:input.event_batch.length,duplicate_event_count:0,rejected_event_count:0};}};
  let saved;
  const capture=new CleanSlateCapture(sink,'Spawn a worker',{...CLEANSLATE_DEFAULT_PROFILE,permissions:[]},'team',
    {effects:CLEANSLATE_DEFAULT_EFFECTS,checkpoint:state=>saved=state});
  async function* stream(){
    yield {type:'tool_start',toolName:'spawn_worker',toolCallId:'a',input:{task:'Read manifest'}};
    yield {type:'tool_result',toolName:'spawn_worker',toolCallId:'a',result:{workerId:'child'}};
  }
  for await (const _ of capture.capture(stream())) {}
  assert.ok(saved.capture_gaps.includes('nested_execution_not_captured'));
  const resumed=ObservedRun.resume(sink,saved,state=>saved=state);
  await resumed.flush();
  assert.ok(saved.capture_gaps.includes('nested_execution_not_captured'));
  await assert.rejects(resumed.complete(true),error=>error.code==='incomplete_stream_capture');
});

test('memory MCP calls are excluded and the default profile matches the example', async () => {
  const sink={async musclememory_observe() {throw new Error('unused');}};
  const capture=new CleanSlateCapture(sink,'Inspect memory',{...CLEANSLATE_DEFAULT_PROFILE,permissions:[]},'team');
  async function* stream(){
    yield {type:'tool_start',toolName:'mcp_call_tool',toolCallId:'a',input:{toolName:'musclememory_recall'}};
    yield {type:'tool_result',toolName:'mcp_call_tool',toolCallId:'a',result:{candidates:[]}};
  }
  for await (const _ of capture.capture(stream())) {}
  assert.equal(capture.run.events.length,1);
  assert.deepEqual(capture.capture_gaps,[]);
  const config=JSON.parse(fs.readFileSync(new URL('../../../integrations/cleanslate/config.example.json',import.meta.url),'utf8'));
  assert.deepEqual(config.bindings,CLEANSLATE_DEFAULT_PROFILE.bindings);
  assert.equal(Object.keys(config.bindings).length,63);
  assert.equal(config.bindings['shell.execute'],'execute_command');
});

test('a crash before stream end cannot turn a partial journal into successful completion', async () => {
  let saved;
  const sink={async musclememory_observe(input) {return {accepted_event_count:input.event_batch.length,duplicate_event_count:0,rejected_event_count:0};}};
  const capture=new CleanSlateCapture(sink,'Read file',{...CLEANSLATE_DEFAULT_PROFILE,permissions:[]},'team',
    {checkpoint:state=>saved=state});
  capture.observe({type:'tool_start',toolName:'read_file',toolCallId:'a',input:{path:'file'}});
  assert.equal(saved.capture_ended,false);
  const resumed=ObservedRun.resume(sink,saved,state=>saved=state);
  await resumed.flush();
  assert.equal(saved.capture_ended,false);
  await assert.rejects(resumed.complete(true),error=>error.code==='incomplete_stream_capture');
});

function gitProject() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'mm-project-'));
  fs.mkdirSync(path.join(root, 'app'));
  fs.writeFileSync(path.join(root, 'app', 'rounding.py'), 'def cents(x):\n    return int(x)\n');
  for (const args of [['init','-q'], ['add','.'], ['-c','user.name=t','-c','user.email=t@localhost','commit','-qm','base']]) {
    execFileSync('git', ['-C', root, ...args]);
  }
  return root;
}

async function learningServer(t) {
  const calls = [];
  const server = http.createServer(async (request, response) => {
    let text = ''; for await (const chunk of request) text += chunk;
    response.setHeader('Content-Type', 'application/json');
    calls.push({url: request.url, body: text ? JSON.parse(text) : {}});
    if (request.url === '/v1/recall') return response.end(JSON.stringify({candidates: [], guidance: null}));
    if (request.url.includes('/auto-learn')) return response.end(JSON.stringify({action: 'learned', procedure_token: 'p1'}));
    const input = JSON.parse(text);
    response.end(JSON.stringify({accepted_event_count: input.event_batch.length, duplicate_event_count: 0, rejected_event_count: 0}));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => server.close());
  return {calls, url: `http://127.0.0.1:${server.address().port}`};
}

function hostFor(t, url, root) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mm-host-'));
  t.after(() => { fs.rmSync(dir, {recursive: true, force: true}); fs.rmSync(root, {recursive: true, force: true}); });
  const config = path.join(dir, 'private.json');
  fs.writeFileSync(config, JSON.stringify({url, api_key: 'k', namespace: 'team'}), {mode: 0o600});
  return new CleanSlateHostMemory(config, path.join(dir, 'state'), () => {}, root);
}

function command(id, cmd, run, result) {
  return [
    {type: 'tool_start', toolName: 'execute_command', toolCallId: id, input: {command: cmd, cwd: '__ROOT__'}, run},
    {type: 'tool_result', toolName: 'execute_command', toolCallId: id, result},
  ];
}

test('a CleanSlate session that edits from the terminal and passes its tests is learned', async t => {
  const root = gitProject();
  const {calls, url} = await learningServer(t);
  const memory = hostFor(t, url, root);
  const native = {async *run() {
    for (const part of [
      ...command('edit', `python3 - <<'E'\nfix()\nE`, () => fs.writeFileSync(path.join(root, 'app', 'rounding.py'), 'def cents(x):\n    return round(x)\n'), {success: true, exitCode: 0, stdout: ''}),
      ...command('test', 'python3 -m unittest discover -s tests 2>&1 | tail -3', undefined, {success: true, exitCode: 0, stdout: 'Ran 3 tests\n\nOK\n'}),
    ]) {
      if (part.input?.cwd) part.input.cwd = root;
      const {run, ...clean} = part;
      yield clean;
      if (run) run();  // the command runs after it starts
    }
  }};
  for await (const _ of memory.attach(native).run('Fix rounding in app/rounding.py')) { /* drain */ }
  const events = calls.filter(c => c.url === '/v1/observe').flatMap(c => c.body.event_batch);
  const edit = events.find(e => e.tool_name === 'execute_command' && e.arguments.command.startsWith('python3 -'));
  assert.deepEqual(edit.arguments.$mm_workspace_changes, ['app/rounding.py']);
  assert.equal(edit.arguments.cwd, '.');
  assert.equal(events.at(-1).kind, 'completion');
  assert.equal(events.at(-1).status, 'success');
  assert.ok(calls.some(c => c.url.includes('/auto-learn')), 'auto-learn was called');
});

test('unreadable piped tests and failures teach nothing', async t => {
  for (const output of ['', 'FAILED (failures=1)\n']) {
    const root = gitProject();
    const {calls, url} = await learningServer(t);
    const memory = hostFor(t, url, root);
    const native = {async *run() {
      yield {type: 'tool_start', toolName: 'apply_edit', toolCallId: 'e', input: {path: path.join(root, 'app', 'rounding.py')}};
      yield {type: 'tool_result', toolName: 'apply_edit', toolCallId: 'e', result: {success: true}};
      for (const part of command('t', 'pytest -q | tail -2', undefined, {success: true, exitCode: 0, stdout: output})) {
        if (part.input?.cwd) part.input.cwd = root;
        const {run: _, ...clean} = part;
        yield clean;
      }
    }};
    for await (const _ of memory.attach(native).run('Fix rounding')) { /* drain */ }
    assert.ok(!calls.some(c => c.url.includes('/auto-learn')), `no learning for output ${JSON.stringify(output)}`);
    const events = calls.filter(c => c.url === '/v1/observe').flatMap(c => c.body.event_batch);
    assert.equal(events.find(e => e.tool_name === 'apply_edit').arguments.path, 'app/rounding.py');
  }
});

test('editing only documentation after the tests passed keeps the session verified', async () => {
  const {verifiedSuccess} = await import('../dist/verification.js');
  const tool = (id, capability, args, status = 'success') =>
    ({event_id: id, kind: 'tool', capability, arguments: args, status, outcome_verified: true});
  const history = [
    tool('e1', 'filesystem.edit', {path: 'app/rounding.py'}),
    tool('e2', 'shell.execute', {command: 'pytest -q'}),
    tool('e3', 'filesystem.edit', {path: 'docs/rounding.md'}),
  ];
  assert.equal(verifiedSuccess(history), 'e2');
  assert.equal(verifiedSuccess([...history, tool('e4', 'filesystem.edit', {path: 'tests/test_rounding.py'})]), null);
});
