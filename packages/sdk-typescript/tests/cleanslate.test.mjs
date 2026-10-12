import {test} from 'node:test';
import assert from 'node:assert/strict';
import {CleanSlateCapture} from '../dist/index.js';

const profile = {agent_name: 'cleanslate', bindings: {'repository.inspect': 'read_file'}};
function client() {
  return {batches: [], async musclememory_observe(input) {
    this.batches.push(structuredClone(input));
    return {accepted_event_count: input.event_batch.length, rejected_event_count: 0, duplicate_event_count: 0};
  }, async musclememory_recall() { return {candidates: []}; }};
}
async function* stream(parts) { yield* parts; }

test('CleanSlate stream preserves original objects and captures real tool boundaries', async () => {
  const sink = client();
  const capture = new CleanSlateCapture(sink, 'Inspect manifest', profile, 'team', {effects: {read_file: 'read'}});
  const start = {type: 'tool_start', toolName: 'read_file', toolCallId: 'a', input: {path: 'manifest', password: 'private'}};
  const result = {type: 'tool_result', toolName: 'read_file', toolCallId: 'a', result: {success: true, content: 'manifest'}};
  const parts = [start, {type: 'chat_text', content: 'Looking'}, result, {type: 'task_complete', result: {success: true}}];
  const observed = [];
  for await (const part of capture.capture(stream(parts))) observed.push(part);
  parts.forEach((part, index) => assert.equal(observed[index], part));
  assert.equal(capture.run.events.length, 2, 'Agent completion does not attest task correctness');
  assert.equal(capture.run.events[1].arguments.password, '[REDACTED]');
  assert.equal(capture.run.events[1].effect, 'read');
  await capture.finish(true);
  assert.equal(sink.batches[0].event_batch.at(-1).kind, 'completion');
});

test('CleanSlate unknown or parallel tools prevent learning and preserve stream exceptions', async () => {
  const capture = new CleanSlateCapture(client(), 'Task', profile, 'team');
  for await (const _ of capture.capture(stream([
    {type: 'tool_start', toolName: 'read_file', toolCallId: 'a', input: {}},
    {type: 'tool_start', toolName: 'unknown', toolCallId: 'b', input: {}},
    {type: 'tool_result', toolName: 'unknown', toolCallId: 'b', result: {}},
  ]))) { /* drain */ }
  assert.ok(capture.capture_gaps.includes('parallel_dependencies_unknown'));
  assert.ok(capture.capture_gaps.includes('unmapped_tool:unknown'));
  assert.ok(capture.capture_gaps.includes('unfinished_tool'));
  await assert.rejects(capture.finish(true), error => error.code === 'incomplete_stream_capture');
  const failed = new Error('original stream error');
  const other = new CleanSlateCapture(client(), 'Task', profile, 'team');
  async function* broken() { throw failed; }
  await assert.rejects(async () => { for await (const _ of other.capture(broken())) {} }, error => error === failed);
});

test('CleanSlate result failure is captured without modifying the result', async () => {
  const capture = new CleanSlateCapture(client(), 'Task', profile, 'team');
  for await (const _ of capture.capture(stream([
    {type: 'tool_start', toolName: 'read_file', toolCallId: 'a', input: {}},
    {type: 'tool_result', toolName: 'read_file', toolCallId: 'a', result: {success: false, message: 'failed'}},
  ]))) {}
  assert.equal(capture.run.events[1].status, 'failure');
  await capture.finish(false);
});
