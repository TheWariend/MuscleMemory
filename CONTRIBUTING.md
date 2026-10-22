# Contributing to MuscleMemory

Small, focused pull requests are welcome. Describe the problem, the change, and
how you checked it. Report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## Local checks

Use Python 3.12 or later and Node.js 24. Install the locked dependencies:

```sh
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
npm ci
```

The checks used by CI are:

```sh
ruff check packages tests scripts examples benchmarks
ruff format --check packages tests scripts examples benchmarks
npm run build
npm run test:sdk
pytest -q
python scripts/generate_contracts.py
git diff --exit-code -- docs/openapi.json packages/sdk-typescript/src/contracts.ts
```

The TypeScript SDK must be built before the Python interoperability tests run.
PostgreSQL checks require `MM_TEST_POSTGRES_URL` pointing to an isolated database
with pgvector; CI supplies its own disposable service. Account-backed model runs
are opt-in and are disabled in pull request CI.

## Pull request automation

Each pull request runs these checks, including contributions from forks:

- **lint:** Python lint and formatting, plus generated API and SDK contracts.
- **backend:** Python tests, protocol checks, and PostgreSQL integration checks.
  A JUnit report is kept as a workflow artifact for seven days.
- **frontend:** dashboard and TypeScript SDK builds, plus SDK tests.
- **secrets:** Gitleaks scans the commits introduced by the pull request. Findings
  are redacted in the logs. A manual run of the Secret scan workflow scans all
  available history.

New commits cancel older runs for the same pull request. These workflows use a
read-only repository token and do not need project secrets.

Dependabot groups weekly GitHub Actions and Node dependency updates into pull
requests. Python lockfile updates remain a maintainer task.

An AI reviewer can supplement these checks after the repository owner installs
the Cubic GitHub App for this repository. Installing a workflow file alone does
not activate that service. AI review does not replace passing checks or maintainer
review. Maintainers can require `lint`, `backend`, `frontend`, and `secrets` in a
branch ruleset after those checks have run at least once.
