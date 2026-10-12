import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ObservedRun} from '../dist/index.js';
import {NodeCaptureJournal, captureIdentity} from '../dist/node.js';

const profile = {agent_name: 'node', bindings: {'repository.inspect': 'read_file'}};
test('Node journal recovers pending data and lost acknowledgements without replaying tools', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'musclememory-node-journal-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const filename = path.join(root, 'capture.json');
  const identity = captureIdentity('http://localhost:8080', 'private-api-key');
  const journal = new NodeCaptureJournal(filename, identity);
  let executions = 0;
  const offline = {async musclememory_observe() { throw new Error('offline'); }};
  const run = new ObservedRun(offline, 'Inspect manifest', profile, 'team', 'durable-run', journal.save);
  run.instrument('repository.inspect', () => { executions++; return {found: true}; })();
  await assert.rejects(run.complete(true));
  assert.equal(fs.statSync(filename).mode & 0o777, 0o600);
  assert.doesNotMatch(fs.readFileSync(filename, 'utf8'), /private-api-key/);
  const reopened = new NodeCaptureJournal(filename, identity);
  const batches = [];
  const resumed = ObservedRun.resume({async musclememory_observe(input) {
    batches.push(input.event_batch);
    return {accepted_event_count: 0, duplicate_event_count: input.event_batch.length, rejected_event_count: 0};
  }}, reopened.load(), reopened.save);
  await resumed.flush();
  assert.equal(executions, 1);
  assert.equal(resumed.pending_count, 0);
  assert.equal(reopened.load().acknowledged_count, 3);
  assert.equal(batches[0].at(-1).kind, 'completion');
  assert.throws(() => new NodeCaptureJournal(filename, captureIdentity('http://localhost:8080', 'another-key')).load(), /journal_identity_mismatch/);
});

test('Node journal rejects symlinks, public files and a stale second writer', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'musclememory-node-journal-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const filename = path.join(root, 'capture.json');
  const journal = new NodeCaptureJournal(filename, 'a'.repeat(64));
  const run = new ObservedRun({async musclememory_observe() {}}, 'Task', profile, 'team', 'run', journal.save);
  const stale = new NodeCaptureJournal(filename, 'a'.repeat(64));
  const old = stale.load();
  run.instrument('repository.inspect', () => true)();
  assert.throws(() => stale.save(old), /journal_writer_conflict/);
  fs.chmodSync(filename, 0o644);
  assert.throws(() => new NodeCaptureJournal(filename, 'a'.repeat(64)), /private_journal_file/);
  fs.chmodSync(filename, 0o600);
  const link = path.join(root, 'linked');
  fs.symlinkSync(filename, link);
  assert.throws(() => new NodeCaptureJournal(link, 'a'.repeat(64)), /private_journal_file/);
});
