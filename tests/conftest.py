import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from musclememory.api import create_app
from musclememory.cli import migrate
from musclememory.config import Settings
from musclememory.core import EvaluateInput, LearnInput, ObserveInput, RuntimeProfile
from musclememory.service import MemoryService

PROFILE = RuntimeProfile(agent_name="python-agent", bindings={"repository.inspect": "read_file"})


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    # Unit/protocol fixtures do not download models or bill inference. The
    # release contract tests assert the production defaults separately.
    monkeypatch.setenv("MM_EMBEDDING_PROVIDER", "lexical-hash")
    monkeypatch.setenv("MM_EMBEDDING_MODEL", "local-lexical-v1")
    monkeypatch.setenv("MM_EMBEDDING_DIM", "128")


@pytest.fixture
def fixture_learner():
    """Explicit model double for protocol fixtures; never runs a user's paid CLI."""
    from musclememory.learning import Assessment, Claim, Proposal

    class FixtureLearner:
        def decide(self, evidence):
            event = next(
                e for e in evidence["events"] if e["status"] == "success" and e["verified"]
            )
            return Proposal(
                action="create",
                reason="Synthetic proposal for protocol coverage",
                title="Apply the fixture regression correction",
                principle=Claim(
                    text="Synthetic fixture invariant for regression coverage",
                    evidence_event_ids=[event["id"]],
                ),
                adaptation=Claim(
                    text="Adapt the fixture input while retaining its invariant",
                    evidence_event_ids=[event["id"]],
                ),
                trigger=Claim(
                    text="Use for this fixture's related regression",
                    evidence_event_ids=[event["id"]],
                ),
                steps=[
                    Claim(
                        text="Apply the supported correction and check its regression suite",
                        evidence_event_ids=[event["id"]],
                    )
                ],
            ), Assessment(
                accept=True,
                reason="supported",
                explanation="Synthetic assessment for protocol coverage",
            )

    return FixtureLearner()


@pytest.fixture
def service(tmp_path, fixture_learner):
    url = f"sqlite:///{tmp_path / 'memory.db'}"
    migrate(url)
    memory = MemoryService(
        Settings(database_url=url, learning_background=False, learning_agent="codex"),
        learner=fixture_learner,
    )
    yield memory
    memory.db.engine.dispose()


@pytest.fixture
def credential(service):
    return service.issue_key("tenant-a", {"team": ["read", "write", "delete", "admin"]})


@pytest.fixture
def principal(service, credential):
    return service.authenticate(credential["api_key"], rate_limit=False)


@pytest.fixture
def http(service, credential):
    with TestClient(
        create_app(service), headers={"Authorization": f"Bearer {credential['api_key']}"}
    ) as client:
        yield client


@pytest.fixture
def remote_server(service, tmp_path):
    # Exercise the real subprocess/JSON path with a deterministic CLI double.
    # No fixture may invoke a signed-in user model or bill inference.
    fake_cli = tmp_path / "bin"
    fake_cli.mkdir()
    program = fake_cli / "codex"
    program.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "body = json.loads(sys.stdin.read().split('\\nEvidence:\\n', 1)[1])\n"
        "if 'proposal' in body:\n"
        " result = dict(accept=True, reason='supported', "
        "explanation='Synthetic fixture assessment')\n"
        "else:\n"
        " event = next(e for e in body['events'] if e['status'] == 'success' and e['verified'])\n"
        " claim = dict(text='Apply the fixture invariant and regression suite', "
        "evidence_event_ids=[event['id']])\n"
        " result = dict(action='create', reason='Synthetic fixture proposal', "
        "title='Apply the fixture correction', trigger=claim, principle=claim, "
        "adaptation=claim, steps=[claim])\n"
        "pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(result))\n"
    )
    program.chmod(0o700)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "musclememory.cli", "serve", "--port", str(port)],
        env={
            **os.environ,
            "MM_DATABASE_URL": service.settings.database_url,
            "MM_LEARNING_AGENT": "codex",
            "MM_LEARNING_BACKGROUND": "false",
            "PATH": str(fake_cli) + os.pathsep + os.environ.get("PATH", ""),
            "CODEX_HOME": str(tmp_path / "codex-home"),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("HTTP server exited during startup")
            try:
                if httpx.get(url + "/health", timeout=0.2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            pytest.fail("HTTP server did not become ready")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


def trace(
    service,
    principal,
    token="training",
    *,
    profile=PROFILE,
    namespace="team",
    success=True,
    arguments=None,
):
    events = [
        {
            "event_id": "intent",
            "sequence": 0,
            "kind": "intent",
            "intent_text": "Inspect repository manifest",
        },
        {
            "event_id": "read",
            "sequence": 1,
            "kind": "tool",
            "tool_name": profile.bindings["repository.inspect"],
            "capability": "repository.inspect",
            "arguments": arguments or {"path": "package.json"},
            "response": {"found": True},
            "status": "success" if success else "failure",
            "duration_ms": 10,
            "token_usage": 50,
        },
        {
            "event_id": "done",
            "sequence": 2,
            "kind": "completion",
            "status": "success" if success else "failure",
        },
    ]
    result = service.observe(
        principal,
        ObserveInput(
            run_token=token, event_batch=events, runtime_profile=profile, visibility_scope=namespace
        ),
    )
    assert result["accepted_event_count"] == 3
    return events


def learned(service, principal, token="training", policy=None, approve=True):
    """Learn a skill from a passing trace and, like a person in the dashboard, approve it."""
    trace(service, principal, token)
    result = service.learn(
        principal,
        LearnInput(
            run_token=token,
            completion_proof={
                "succeeded": True,
                "evidence_event_ids": ["read"],
                "summary": "Manifest inspected",
            },
            learning_policy=policy or {},
        ),
    )
    if approve:
        service.approve(principal, result["procedure_token"])
    return result


def evaluate(service, principal, procedure, token, success=True, version=1, profile=PROFILE):
    trace(service, principal, token, success=success, profile=profile)
    return service.evaluate(
        principal,
        EvaluateInput(
            procedure_token=procedure,
            run_token=token,
            outcome_report={
                "succeeded": success,
                "procedure_version": version,
                "evidence_event_ids": ["read"] if success else [],
            },
            performance_metrics={
                "observed": {"tool_calls": 1, "duration_ms": 10, "tokens": 50},
                "baseline": {"tool_calls": 3, "duration_ms": 30, "tokens": 100},
            },
        ),
    )


def validated(service, principal):
    result = learned(service, principal)
    for i in range(2):
        evaluate(service, principal, result["procedure_token"], f"validation-{i}")
    return result


@pytest.fixture
def postgres_service():
    from uuid import uuid4

    from alembic import command
    from alembic.config import Config
    from musclememory.cli import ROOT
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = os.getenv("MM_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set MM_TEST_POSTGRES_URL for PostgreSQL mode/migration checks")
    schema = "mm_modes_" + uuid4().hex
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    schema_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema},public"})
    schema_url = schema_url.render_as_string(hide_password=False)
    config = Config()
    config.set_main_option("sqlalchemy.url", schema_url.replace("%", "%%"))
    config.set_main_option("script_location", str(ROOT / "packages/musclememory/migrations"))
    config.set_main_option("version_table_schema", schema)
    memory = None
    try:
        migrate(schema_url, version_table_schema=schema)
        memory = MemoryService(Settings(database_url=schema_url))
        yield memory, config
    finally:
        command.downgrade(config, "base")
        if memory:
            memory.db.engine.dispose()
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()
