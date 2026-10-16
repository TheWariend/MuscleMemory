import assert from 'node:assert/strict';
import test from 'node:test';
import {isTestCommand, pipedTest} from '../dist/verification.js';

test('real test runs count, commands that only mention tests do not', () => {
  for (const command of ['pytest -q', 'python3 -m unittest discover -s tests', 'cargo test', 'go test ./...',
    'npm run test:unit', 'uv run pytest', 'CI=1 npx jest', "/bin/zsh -lc 'python tests/runtests.py admin_views'"]) {
    assert.equal(isTestCommand(command), true, command);
  }
  for (const command of ['cargo build', 'cargo check', 'ruff check .', 'echo test', 'go build ./...', 'npm install',
    "sed -n '1,260p' tests/test_contracts.py"]) {
    assert.equal(isTestCommand(command), false, command);
  }
});

test('a pipe only matters after the test runner', () => {
  assert.equal(pipedTest('pytest 2>&1 | tail -5'), true);
  assert.equal(pipedTest('pytest -q'), false);
  assert.equal(pipedTest('cat x | grep y && pytest'), false);
});
