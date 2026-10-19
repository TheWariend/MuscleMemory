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

test('test files run directly count, typechecks and other scripts do not', () => {
  for (const command of ['python3 scripts/test_ios_signing.py', 'python tests/login_test.py',
    'node --test tests/*.test.mjs', 'node --experimental-strip-types --test', 'node tests/auth.test.mjs',
    'npx tsx src/rounding.spec.ts']) {
    assert.equal(isTestCommand(command), true, command);
  }
  for (const command of ['npm run typecheck', 'tsc --noEmit', 'npm run build',
    'node scripts/mobile-contract-check.mjs', 'node build.mjs', 'python3 scripts/generate_tests.py',
    'node tests/fixtures/test.json']) {
    assert.equal(isTestCommand(command), false, command);
  }
});

test('a pipe only matters after the test runner', () => {
  assert.equal(pipedTest('pytest 2>&1 | tail -5'), true);
  assert.equal(pipedTest('pytest -q'), false);
  assert.equal(pipedTest('cat x | grep y && pytest'), false);
});
