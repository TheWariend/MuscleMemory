import {test} from 'node:test';
import assert from 'node:assert/strict';
import {MuscleMemoryClient, MuscleMemoryError} from '../dist/index.js';

test('all five tool names use v1 REST contracts', async () => {
  const requests = [];
  const client = new MuscleMemoryClient('https://memory.example/', 'test-credential', async (url, options) => {
    requests.push({url, options});
    return new Response(JSON.stringify({contract_version: '1'}));
  });
  for (const name of ['recall', 'observe', 'learn', 'evaluate', 'inspect']) {
    await client[`musclememory_${name}`]({run_token: 'run'});
    assert.equal(requests.at(-1).url, `https://memory.example/v1/${name}`);
    assert.equal(requests.at(-1).options.headers.Authorization, 'Bearer test-credential');
    assert.equal(JSON.parse(requests.at(-1).options.body).run_token, 'run');
  }
});

test('error messages never include a response body or credential', async () => {
  const client = new MuscleMemoryClient('https://memory.example', 'private-credential', async () =>
    new Response(JSON.stringify({error: {code: 'forbidden'}, input: 'private-credential'}), {status: 403}));
  await assert.rejects(client.musclememory_inspect({procedure_token: 'x'}), error =>
    error instanceof MuscleMemoryError && error.status === 403 && !error.message.includes('private-credential'));
});

