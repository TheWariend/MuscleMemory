import {test} from 'node:test';
import assert from 'node:assert/strict';
import {ObservedRun, CaptureError, MuscleMemoryClient, redact} from '../dist/index.js';

const profile = {agent_name: 'typescript-agent', bindings: {'repository.inspect': 'read_file'}};
function sink() {
  return {batches: [], async musclememory_observe(input) {
    this.batches.push(structuredClone(input));
    return {accepted_event_count: input.event_batch.length, rejected_event_count: 0, duplicate_event_count: 0};
  }};
}

test('sync capture preserves results, exceptions, this, and sanitized argument snapshots', async () => {
  const destination = sink();
  const run = new ObservedRun(destination, 'Inspect manifest', profile, 'team');
  const result = {authorization: 'Bearer sk-privatekey123456', valid: true};
  const object = {value: result, read: run.instrument('repository.inspect', function(args) {
    args.path = 'mutated'; return this.value;
  }, {arguments: ([args]) => args})};
  const args = {path: 'manifest', password: 'never-send-this'};
  assert.equal(object.read(args), result);
  const failure = new Error('password=never-send-that');
  const fail = run.instrument('repository.inspect', () => {throw failure;});
  assert.throws(fail, error => error === failure);
  await run.complete(false);
  const events = destination.batches[0].event_batch;
  assert.equal(events[1].arguments.path, 'manifest');
  assert.equal(events[2].status, 'failure');
  assert.doesNotMatch(JSON.stringify(events), /never-send|sk-privatekey/);
  assert.deepEqual(run.measurements().tool_calls, 2);
  assert.equal(run.measurements().tokens, null);
  assert.throws(() => object.read({}), CaptureError);
});

test('async capture prevents overlaps, early completion, and early flush', async () => {
  const run = new ObservedRun(sink(), 'Inspect manifest', profile, 'team');
  let finish;
  const read = run.instrument('repository.inspect', () => new Promise(resolve => {finish = resolve;}));
  const call = read();
  assert.throws(read, error => error.code === 'serial_capture_busy');
  await assert.rejects(run.complete(), error => error.code === 'serial_capture_busy');
  await assert.rejects(run.flush(), error => error.code === 'serial_capture_busy');
  finish({found: true});
  assert.deepEqual(await call, {found: true});
  const failed = new Error('Bearer sk-othersecret1234');
  await assert.rejects(run.instrument('repository.inspect', async () => {throw failed;})(), error => error === failed);
  await run.complete(false);
  assert.equal(run.events[2].status, 'failure');
  assert.doesNotMatch(JSON.stringify(run.events), /sk-othersecret/);
});

test('lost acknowledgements retain exact events and concurrent flushes share one upload', async () => {
  const batches = [];
  let attempts = 0;
  const run = new ObservedRun({async musclememory_observe(input) {
    batches.push(structuredClone(input.event_batch));
    if (++attempts === 1) throw new Error('offline');
    return {accepted_event_count: 0, rejected_event_count: 0, duplicate_event_count: input.event_batch.length};
  }}, 'Inspect manifest', profile, 'team');
  run.instrument('repository.inspect', path => ({found: path}), {arguments: ([path]) => ({path})})('manifest');
  await assert.rejects(run.complete());
  assert.equal(run.pending_count, 3);
  const first = run.flush(), second = run.flush();
  assert.equal(first, second);
  await first;
  assert.deepEqual(batches[0], batches[1]);
  assert.equal(run.pending_count, 0);
  const snapshot = run.events;
  snapshot[0].intent_text = 'tampered';
  assert.equal(run.events[0].intent_text, 'Inspect manifest');
});

test('partial rejection or incomplete acknowledgement never drops events', async () => {
  for (const response of [
    {accepted_event_count: 1, rejected_event_count: 1, duplicate_event_count: 0},
    {accepted_event_count: 0, rejected_event_count: 0, duplicate_event_count: 0},
    {accepted_event_count: -1, rejected_event_count: 0, duplicate_event_count: 2},
  ]) {
    const run = new ObservedRun({async musclememory_observe() {return response;}}, 'Inspect', profile, 'team');
    await assert.rejects(run.flush(), error => error.code === 'upload_not_acknowledged');
    assert.equal(run.pending_count, 1);
  }
});

test('retry metadata preserves failures and invalid dependencies prevent effects', async () => {
  const run = new ObservedRun(sink(), 'Inspect', profile, 'team');
  let effects = 0;
  assert.throws(() => run.instrument('repository.inspect', () => effects++, {dependencies: ['missing']})());
  assert.equal(effects, 0);
  assert.throws(run.instrument('repository.inspect', () => {throw Error('missing');}));
  run.instrument('repository.inspect', () => effects++, {retry_of: 'e1'})();
  await run.complete();
  assert.equal(run.events[2].retry_of, 'e1');
  assert.equal(effects, 1);
});

test('byte bounds split large batches and omit oversized responses without changing tool results', async () => {
  const destination = sink();
  const run = new ObservedRun(destination, 'Inspect', profile, 'team');
  const result = {text: 'x'.repeat(40000)};
  const read = run.instrument('repository.inspect', () => result);
  for (let index = 0; index < 35; index++) assert.equal(read(), result);
  const oversized = {text: 'x'.repeat(100000)};
  assert.equal(run.instrument('repository.inspect', () => oversized)(), oversized);
  assert.equal(run.events.at(-1).response.capture_note, 'Response omitted to fit event limit');
  await run.complete();
  assert.equal(destination.batches.length, 2);
  assert.ok(destination.batches.every(batch => JSON.stringify(batch).length < 1100000));
});

test('non-JSON results, cycles and accessors are omitted without invoking accessors', async () => {
  const run = new ObservedRun(sink(), 'Inspect', profile, 'team');
  let accessed = false;
  const object = {get password() {accessed = true; return 'secret';}};
  const cyclic = {}; cyclic.self = cyclic;
  for (const result of [object, cyclic, 5n, undefined]) {
    assert.equal(run.instrument('repository.inspect', () => result)(), result);
    assert.match(run.events.at(-1).response.capture_note, /omitted/);
  }
  assert.equal(accessed, false);
  await run.complete();
});

test('TypeScript redacts SDK requests and rejects secret-bearing error codes', async () => {
  let sent;
  const client = new MuscleMemoryClient('https://memory.example', 'own-test-key', async (_url, options) => {
    sent = options.body;
    return new Response(JSON.stringify({error: {code: 'Bearer private-body-key'}}), {status: 403});
  });
  await assert.rejects(client.musclememory_observe({run_token: 'test', runtime_profile: profile,
    visibility_scope: 'team', event_batch: [{password: 'not-on-wire', response: 'Bearer sk-privatekey987654'}]}),
    error => error.code === 'request_failed' && !error.message.includes('private-body-key'));
  assert.doesNotMatch(sent, /not-on-wire|sk-privatekey/);
  const data = {token: 'secret', url: 'postgres://user:password@host/database'};
  assert.doesNotMatch(JSON.stringify(redact(data)), /secret|user:password/);
  assert.equal(data.token, 'secret');
});

test('runtime and capture metadata are checked before any tool effect', () => {
  assert.throws(() => new ObservedRun(sink(), 'Inspect', {...profile, extra: 'unknown'}, 'team'), CaptureError);
  assert.throws(() => new ObservedRun(sink(), 'Inspect', {bindings: {'repository.inspect': 'not a tool'}}, 'team'), CaptureError);
  const run = new ObservedRun(sink(), 'Inspect', profile, 'team');
  let effects = 0;
  assert.throws(() => run.instrument('repository.inspect', () => effects++, {effect: 'execute'}), CaptureError);
  assert.throws(() => run.instrument('repository.inspect', () => effects++, {arguments: () => null})(), CaptureError);
  assert.equal(effects, 0);
  assert.deepEqual(JSON.parse(JSON.stringify(redact({optional: undefined, included: true}))), {included: true});
});

test('trace capacity reserves completion before allowing another tool effect', async () => {
  const run = new ObservedRun(sink(), 'Inspect', profile, 'team');
  let effects = 0;
  const read = run.instrument('repository.inspect', () => effects++);
  for (let index = 0; index < 1022; index++) read();
  assert.throws(read, error => error.code === 'trace_limit');
  await run.complete();
  assert.equal(effects, 1022);
  assert.equal(run.events.length, 1024);
});
