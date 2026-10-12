import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
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
