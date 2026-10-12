"""Account-backed Codex coding benchmark against persistent MuscleMemory stores.

Run explicitly; this consumes the logged-in user's Codex allowance. Source,
protocol, independent checks, all model receipts, and every result are preserved.
No fixture is a production repository, but the tools/model/capture/learning and
databases are real. Both arms (memory off, MuscleMemory) share task prompts, source
and model settings. Each campaign uses its own database and local server.
"""

import argparse
import asyncio
import difflib
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from codex_fixtures import FAMILIES, PROMPTS, fixture_files, prepare, verify
from musclememory import client_hooks
from musclememory.cli import migrate
from musclememory.client_hooks import HookQueue, run_hook
from musclememory.config import Settings
from musclememory.core import (
    EvaluateInput,
    InspectInput,
    LearnInput,
    ObserveInput,
    SessionUsageInput,
)
from musclememory.delivery import token_count
from musclememory.receipts import audit_codex_tools, codex_tool_counts, codex_usage
from musclememory.sdk import MuscleMemoryClient
from musclememory.service import MemoryService
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = Path.home() / "Library/Application Support/MuscleMemory/private"
PYTHON = str(ROOT / ".venv/bin/python")
ARMS = ("off", "musclememory")
MODE = "musclememory"


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def measured_sum(values):
    values = list(values)
    return sum(values) if all(value is not None for value in values) else None


def hook(config, receipts):
    payload = json.loads(sys.stdin.buffer.read(2 * 1024 * 1024))
    searches = []

    class RecordedClient(MuscleMemoryClient):
        def musclememory_recall(self, **arguments):
            started = time.monotonic()
            try:
                result = super().musclememory_recall(**arguments)
            except Exception as error:
                searches.append(
                    {
                        "latency_ms": (time.monotonic() - started) * 1000,
                        "candidate_tokens": [],
                        "error_type": type(error).__name__,
                    }
                )
                raise
            searches.append(
                {
                    "latency_ms": (time.monotonic() - started) * 1000,
                    "candidate_tokens": [
                        c["procedure_token"] for c in result.get("candidates", [])
                    ],
                }
            )
            return result

    client_hooks.MuscleMemoryClient = RecordedClient
    output = run_hook(config, "codex", payload)
    context = output.get("hookSpecificOutput", {}).get("additionalContext", "")
    recall = {}
    if context:
        try:
            recall = json.loads(context)
        except ValueError:
            recall = {"truncated": True}
    with Path(receipts).open("a") as log:
        fcntl.flock(log, fcntl.LOCK_EX)
        log.write(
            json.dumps(
                {
                    "event": payload.get("hook_event_name"),
                    "tool": payload.get("tool_name"),
                    "context": context,
                    "context_characters": len(context),
                    "candidate_tokens": [recall["procedure"]] if recall.get("procedure") else [],
                    "candidate_titles": [recall["title"]] if recall.get("title") else [],
                    "context_tokens": token_count(context),
                    "truncated": recall.get("truncated", False),
                    "searches": searches,
                }
            )
            + "\n"
        )
    print(json.dumps(output))


def audit_tools(trace, messages):
    """Use actual CLI exit codes; hook stdout alone omits some failure statuses."""
    try:
        return audit_codex_tools(trace, messages)
    except ValueError as error:
        raise RuntimeError(str(error)) from error


def snapshot(service, namespace=None):
    with service.db.transaction() as session:
        bank = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT p.procedure_token,p.current_version,r.validation_status,r.successes,"
                    "r.failures,r.definition,r.capability_signature,r.embedding_space,"
                    "r.embedding_dimensions,r.embedding,r.search_text "
                    "FROM procedures p JOIN revisions r ON "
                    "p.procedure_token=r.procedure_token AND p.current_version=r.version "
                    + ("WHERE p.namespace=:namespace " if namespace else "")
                    + "ORDER BY p.procedure_token"
                ),
                {"namespace": namespace} if namespace else {},
            ).mappings()
        ]
    return hashlib.sha256(json.dumps(bank, sort_keys=True).encode()).hexdigest()


class Campaign:
    def __init__(self, directory, model, seed_campaign=None, port=8098):
        self.directory, self.model = directory, model
        self.codex = os.getenv("MM_CODEX_BIN", shutil.which("codex"))
        self.metadata = directory / "campaign.json"
        self.manifest = json.loads(self.metadata.read_text()) if self.metadata.exists() else None
        self.arms = ARMS
        seeded = self.manifest.get("seed_campaign") if self.manifest else seed_campaign
        if not seeded:
            raise ValueError("Campaigns require --seed-campaign with verified archived traces")
        self.seed = PRIVATE / "codex-evaluations" / seeded
        self.namespace = (
            self.manifest.get("namespace")
            if self.manifest
            else ("eval-" + directory.name.replace("_", "-")[:100])
        )
        # A campaign-local database and server: the user's live memory is never touched.
        database = directory / "memory.db"
        url = "sqlite:///" + str(database)
        migrate(url)
        service = MemoryService(replace(Settings.from_env(), database_url=url))
        secret = directory / "musclememory.env"
        if not secret.exists():
            credential = service.issue_key(
                "local",
                {self.namespace: ["read", "write", "admin"]},
                "Codex evaluation benchmark",
            )
            secret.write_text("MM_API_KEY=" + credential["api_key"] + "\n")
            secret.chmod(0o600)
        key = secret.read_text().split("=", 1)[1].strip()
        server = f"http://127.0.0.1:{port}"
        self.start_server(server, database, port)
        self.services = {MODE: service}
        self.keys = {MODE: key}
        self.principals = {MODE: service.authenticate(key, rate_limit=False)}
        self.clients = {MODE: MuscleMemoryClient(server, key, timeout=10)}
        self.home = directory / "codex-home"
        self.home.mkdir(mode=0o700, exist_ok=True)
        auth = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
        shutil.copyfile(auth, self.home / "auth.json")
        (self.home / "auth.json").chmod(0o600)

    def start_server(self, url, database, port):
        metadata = self.directory / "server.json"
        if metadata.exists():
            prior = json.loads(metadata.read_text())
            if prior.get("database") != str(database):
                raise RuntimeError("Existing campaign listener uses a different database")
            try:
                if httpx.get(url + "/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
        environment = {key: value for key, value in os.environ.items() if not key.startswith("MM_")}
        environment["MM_DATABASE_URL"] = "sqlite:///" + str(database)
        with (self.directory / "server.log").open("ab") as log:
            process = subprocess.Popen(
                [PYTHON, "-m", "musclememory.cli", "serve", "--host", "127.0.0.1"]
                + ["--port", str(port)],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        write_json(metadata, {"pid": process.pid, "url": url, "database": str(database)})
        for _ in range(100):
            try:
                if httpx.get(url + "/health", timeout=0.5).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("Campaign MuscleMemory server did not start")

    def freeze(self, repeats):
        if self.manifest:
            if self.manifest["model"] != self.model or self.manifest["repeats"] != repeats:
                raise ValueError("Cannot change a frozen campaign's model or repetitions")
            return
        hashes = {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                ROOT / "benchmarks/evaluate_codex.py",
                ROOT / "benchmarks/codex_fixtures.py",
                ROOT / "packages/musclememory/client_hooks.py",
                ROOT / "packages/musclememory/retrieval.py",
                ROOT / "packages/musclememory/service.py",
                ROOT / "packages/musclememory/delivery.py",
                ROOT / "packages/musclememory/artifacts.py",
                ROOT / "packages/musclememory/shapes.py",
                ROOT / "packages/musclememory/receipts.py",
            ]
        }
        self.manifest = {
            "campaign": self.directory.name,
            "seed_campaign": self.seed.name,
            "training_source_protocol": json.loads((self.seed / "campaign.json").read_text()),
            "namespace": self.namespace,
            "created_at": datetime.now(UTC).isoformat(),
            "model": self.model,
            "reasoning_effort": "medium",
            "repeats": repeats,
            "codex_version": subprocess.check_output([self.codex, "--version"], text=True).strip(),
            "codex_binary": self.codex,
            "python": platform.python_version(),
            "git_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "source_sha256": hashes,
            "families": list(FAMILIES),
            "arms": list(self.arms),
            "training": "One actual successful Codex trace per family, two independently "
            "verified applications; identical evidence supplied to both banks.",
            "evaluation": "Frozen banks, fresh sessions and fresh workspace copies; two "
            "held-out paraphrases per family with repeated disjoint verification seeds; "
            "paired identical fixture bytes per arm.",
            "native_memory": "Fresh isolated CODEX_HOME, no pre-existing skills or memories "
            "in any arm. This is not a head-to-head against native Codex memory.",
            "provider": self.services[MODE].embeddings.metadata(),
            "persistent_databases": {
                mode: service.settings.database_url for mode, service in self.services.items()
            },
            "warmup_cost_included_in_scored_totals": False,
            "timeout_seconds": 240,
            "judge": "Independent executable behavioral assertions, immutable public tests, "
            "protected-file hashes; failures counted in the denominator.",
            "exit_code_audit": "Native capture reconciled with CLI JSON receipts. Status "
            "corrections stored as a derived, linked evidence run.",
        }
        self.manifest.update(
            max_paid_sessions=len(FAMILIES) * len(self.arms) * repeats,
            workload="Repeated repair workflows with stable relative paths; "
            "held-out paraphrases and verification seeds. Not renamed-path transfer.",
            training="Recompile the first three complete, independently verified archived "
            "traces per family into a new namespace; no new model training calls.",
            evaluation=f"Each fixture family, {repeats} repetition(s), memory off and "
            "MuscleMemory with identical fixture bytes per pair; arm order rotates.",
            inspection="Identical musclememory_inspect schema in both arms; read-only "
            "scoped credentials; off arm uses an empty namespace.",
            delivery="Native prompt hook only: at most one pointer per session; tool hooks "
            "capture without injecting.",
            seed_source_model=json.loads((self.seed / "campaign.json").read_text())["model"],
            cross_model_memory=json.loads((self.seed / "campaign.json").read_text())["model"]
            != self.model,
        )
        write_json(self.metadata, self.manifest)

    def seed_archived_evidence(self):
        """Recompile actual archived evidence; this never invokes the model."""
        if not self.seed:
            return
        learned_paths = [self.directory / f"learned-{family}.json" for family in FAMILIES]
        if (self.directory / "seed-evidence.json").exists() and all(
            path.exists() and json.loads(path.read_text()).get("completed")
            for path in learned_paths
        ):
            return
        imports = []
        for family in FAMILIES:
            path = self.directory / f"learned-{family}.json"
            if path.exists():
                raise RuntimeError("Seed banks are immutable; use a fresh campaign")
            eligible = []
            for directory in sorted((self.seed / "runs").glob(f"warmup-{family}-*")):
                record = json.loads((directory / "result.json").read_text())
                if not record["passed"] or not record["capture_ok"]:
                    continue
                trace = json.loads((directory / "trace.json").read_text())
                messages = [
                    json.loads(line)
                    for line in (directory / "codex.jsonl").read_text().splitlines()
                ]
                checked = verify(
                    directory / "workspace",
                    json.loads((directory / "fixture-hashes.json").read_text()),
                    800000 + record["variant"],
                )
                if not checked["passed"] or trace["events"][-1]["kind"] != "completion":
                    raise RuntimeError("Archived seed lost its independent verification")
                audit_tools(trace, messages)
                eligible.append((directory, trace, checked))
            if len(eligible) < 3:
                raise RuntimeError("Seed requires training plus two independent complete traces")
            learned = {}
            for index, (directory, trace, checked) in enumerate(eligible[:3]):
                events = deepcopy(trace["events"])
                origin = {
                    "archive": self.seed.name,
                    "label": directory.name,
                    "source_run": trace["run_token"],
                    "trace_sha256": hashlib.sha256(
                        (directory / "trace.json").read_bytes()
                    ).hexdigest(),
                    "receipt_sha256": hashlib.sha256(
                        (directory / "codex.jsonl").read_bytes()
                    ).hexdigest(),
                    "historical_evidence": True,
                }
                events[0]["response"] = {"archive_import": origin}
                token = str(uuid4())
                successful = [e for e in events if e["kind"] == "tool" and e["status"] == "success"]
                from musclememory.artifacts import operation_categories

                evidence = [
                    e["event_id"]
                    for e in successful
                    if "verify" in operation_categories(e["arguments"], e["capability"])
                ][-1:] or [successful[-1]["event_id"]]
                for mode, service in self.services.items():
                    principal = self.principals[mode]
                    service.observe(
                        principal,
                        ObserveInput(
                            run_token=token,
                            event_batch=events,
                            runtime_profile=trace["runtime_profile"],
                            visibility_scope=self.namespace,
                        ),
                    )
                    if index == 0:
                        learned[mode] = service.learn(
                            principal,
                            LearnInput(
                                run_token=token,
                                completion_proof={
                                    "succeeded": True,
                                    "evidence_event_ids": evidence,
                                    "summary": "Archived CLI receipts and behavior checks passed.",
                                },
                            ),
                        )
                    else:
                        service.evaluate(
                            principal,
                            EvaluateInput(
                                procedure_token=learned[mode]["procedure_token"],
                                run_token=token,
                                outcome_report={
                                    "succeeded": True,
                                    "procedure_version": 1,
                                    "evidence_event_ids": evidence,
                                },
                                performance_metrics={
                                    "observed": {
                                        "tool_calls": sum(e["kind"] == "tool" for e in events),
                                        "duration_ms": sum(
                                            e["duration_ms"] for e in events if e["kind"] == "tool"
                                        ),
                                    }
                                },
                            ),
                        )
                imports.append({**origin, "imported_run": token, "verification": checked})
            write_json(path, {**learned, "completed": True})
        write_json(self.directory / "seed-evidence.json", imports)

    def inspection_config(self, arm, run_dir):
        """Same tool schema for every arm, with scoped read-only credentials."""
        mode = MODE
        scope = self.namespace + "-off" if arm == "off" else self.namespace
        secret = self.directory / f"inspect-{arm}.env"
        if not secret.exists():
            issued = self.services[mode].issue_key("local", {scope: ["read"]}, "Codex inspection")
            secret.write_text("MM_API_KEY=" + issued["api_key"] + "\n")
            secret.chmod(0o600)
        config = run_dir / "inspection-config.json"
        write_json(
            config,
            {
                "url": str(self.clients[mode].http.base_url),
                "namespace": scope,
                "env_file": str(secret),
                "state_dir": str(run_dir / "inspection-state"),
            },
        )
        return config

    def preflight(self):
        """Historical-prefix replay and real MCP handshake, without model calls."""
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters

        results = []
        for family in FAMILIES:
            sources = sorted((self.seed / "runs").glob(f"warmup-{family}-*"))
            complete = [
                d for d in sources if json.loads((d / "result.json").read_text())["capture_ok"]
            ]
            source = complete[1]  # Independent validation trace, not the compiler's training trace.
            trace = json.loads((source / "trace.json").read_text())
            learned = json.loads((self.directory / f"learned-{family}.json").read_text())
            for mode in self.services:
                for prompt_index in (1, 2):
                    token = str(uuid4())
                    prompt = PROMPTS[family][prompt_index]
                    events = [deepcopy(trace["events"][0])]
                    events[0]["intent_text"] = prompt
                    events[0]["response"] = {"offline_replay": source.name}
                    profile = trace["runtime_profile"]
                    client = self.clients[mode]
                    client.musclememory_observe(
                        run_token=token,
                        event_batch=events,
                        runtime_profile=profile,
                        visibility_scope=self.namespace,
                    )
                    matches, emitted = [], []
                    try:
                        for prefix in range(9):
                            result = client.musclememory_recall(
                                observation_run=token,
                                intent_text=prompt,
                                runtime_profile=profile,
                                visibility_scope=self.namespace,
                                match_limit=1,
                            )
                            candidates = result.get("candidates", [])
                            if candidates:
                                matches.append(candidates[0]["procedure_token"])
                            if result.get("guidance"):
                                emitted.append({"prefix": prefix, "context": result["guidance"]})
                            tools = [e for e in trace["events"] if e["kind"] == "tool"]
                            if prefix == 8 or prefix >= len(tools):
                                break
                            event = deepcopy(tools[prefix])
                            event["sequence"] = prefix + 1
                            events.append(event)
                            client.musclememory_observe(
                                run_token=token,
                                event_batch=[event],
                                runtime_profile=profile,
                                visibility_scope=self.namespace,
                            )
                    finally:
                        client.musclememory_observe(
                            run_token=token,
                            event_batch=[
                                {
                                    "event_id": "replay-end",
                                    "sequence": len(events),
                                    "kind": "completion",
                                    "status": "failure",
                                    "capture_complete": False,
                                    "response": {
                                        "offline_replay": True,
                                        "no_current_task_outcome": True,
                                    },
                                }
                            ],
                            runtime_profile=profile,
                            visibility_scope=self.namespace,
                        )
                    target = learned[mode]["procedure_token"]
                    correct = any(
                        json.loads(x["context"]).get("procedure") == target for x in emitted
                    )
                    wrong = any(t != target for t in matches)
                    results.append(
                        {
                            "family": family,
                            "mode": mode,
                            "prompt_index": prompt_index,
                            "target": target,
                            "correct_guidance": correct,
                            "wrong_candidates": wrong,
                            "contexts": emitted,
                            "historical_prefix_source": source.name,
                        }
                    )

        async def inspect():
            checks = []
            for arm in self.arms:
                config = self.inspection_config(arm, self.directory)
                parameters = StdioServerParameters(
                    command=PYTHON,
                    args=["-m", "musclememory.cli", "stdio", "--config", str(config)],
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                async with Client(parameters) as client:
                    listed = await client.list_tools()
                    assert len(listed.tools) == 5
                    for family in FAMILIES:
                        mode = MODE
                        token = json.loads((self.directory / f"learned-{family}.json").read_text())[
                            mode
                        ]["procedure_token"]
                        response = await client.call_tool(
                            "musclememory_inspect", {"procedure_token": token}
                        )
                        if bool(response.is_error) != (arm == "off"):
                            raise RuntimeError("Scoped inspection preflight failed")
                        checks.append({"arm": arm, "family": family, "is_error": response.is_error})
            return checks

        inspection = asyncio.run(inspect())
        passed = all(r["correct_guidance"] and not r["wrong_candidates"] for r in results)
        current = self.directory / "preflight.json"
        if current.exists():
            attempt = len(list(self.directory.glob("preflight-attempt-*.json"))) + 1
            current.rename(self.directory / f"preflight-attempt-{attempt:03}.json")
        write_json(
            current,
            {
                "passed": passed,
                "replays": results,
                "inspection": inspection,
                "model_calls": 0,
                "source_sha256": {
                    str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted((ROOT / "packages/musclememory").glob("*.py"))
                },
            },
        )
        if not passed:
            raise RuntimeError("Historical replay failed; no paid runs allowed")

    def run(self, label, family, variant, arm, prompt_index):
        if (self.directory / "stop-after-run").exists():
            raise RuntimeError("Evaluation paused at a completed run boundary")
        run_dir = self.directory / "runs" / label
        result_path = run_dir / "result.json"
        if result_path.exists():
            return json.loads(result_path.read_text())
        if run_dir.exists():
            raise RuntimeError(f"Incomplete run {label}; preserve it and use a new campaign")
        if (
            len(list((self.directory / "runs").glob("scored-*")))
            >= self.manifest["max_paid_sessions"]
        ):
            raise RuntimeError("Planned paid sessions reached; no additional model calls allowed")
        run_dir.mkdir(parents=True, mode=0o700)
        project = run_dir / "workspace"
        original = prepare(project, family, variant)
        before = verify(project, original, 1000 + variant)
        assert not before["passed"], "Fixture must fail before Codex edits it"
        prompt = PROMPTS[family][prompt_index]
        write_json(run_dir / "fixture-hashes.json", original)
        (run_dir / "prompt.txt").write_text(prompt + "\n")
        mode = MODE
        client, service = self.clients[mode], self.services[mode]
        principal = self.principals[mode]
        state = run_dir / "state"
        env_file = run_dir / "memory.env"
        env_file.write_text("MM_API_KEY=" + self.keys[mode] + "\n")
        env_file.chmod(0o600)
        config = run_dir / "hook-config.json"
        write_json(
            config,
            {
                "url": str(client.http.base_url),
                "namespace": self.namespace,
                "env_file": str(env_file),
                "state_dir": str(state),
                "recall_enabled": arm != "off",
            },
        )
        receipts = run_dir / "hooks.jsonl"
        command = shlex.join(
            [
                PYTHON,
                str(Path(__file__).resolve()),
                "hook",
                "--config",
                str(config),
                "--receipts",
                str(receipts),
            ]
        )
        definitions = {
            event: [{"hooks": [{"type": "command", "command": command, "timeout": 15}]}]
            for event in ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
        }
        (project / ".codex").mkdir()
        (project / ".codex/config.toml").write_text("# Local benchmark configuration.\n")
        write_json(project / ".codex/hooks.json", {"hooks": definitions})
        (self.home / "config.toml").write_text(
            f'[projects.{json.dumps(str(project))}]\ntrust_level="trusted"\n'
        )
        if self.seed:
            inspection = self.inspection_config(arm, run_dir)
            with (self.home / "config.toml").open("a") as configuration:
                configuration.write(
                    "\n[mcp_servers.musclememory]\n"
                    + "command="
                    + json.dumps(PYTHON)
                    + "\n"
                    + "args="
                    + json.dumps(["-m", "musclememory.cli", "stdio", "--config", str(inspection)])
                    + "\n"
                    + 'enabled_tools=["musclememory_inspect"]\nrequired=true\n'
                    + "startup_timeout_sec=20\ntool_timeout_sec=15\n"
                )
        environment = {key: value for key, value in os.environ.items() if not key.startswith("MM_")}
        environment["CODEX_HOME"] = str(self.home)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        argv = [
            self.codex,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--dangerously-bypass-hook-trust",
            "--json",
            "--model",
            self.model,
            "-c",
            'model_reasoning_effort="medium"',
            "-C",
            str(project),
            prompt,
        ]
        started = time.monotonic()
        with (
            (run_dir / "codex.jsonl").open("w") as stdout,
            (run_dir / "codex.stderr").open("w") as stderr,
        ):
            process = subprocess.Popen(
                argv,
                env=environment,
                cwd=project,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                returncode = process.wait(timeout=240)
            except subprocess.TimeoutExpired:
                import signal

                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=15)
                returncode = -124
        elapsed = (time.monotonic() - started) * 1000
        messages = [
            json.loads(line)
            for line in (run_dir / "codex.jsonl").read_text().splitlines()
            if line.startswith("{")
        ]
        used = codex_usage(messages)
        cli_counts = codex_tool_counts(messages)
        usage_available = all(value is not None for value in used.values())
        validation = verify(project, original, 100000 + variant)
        hooks = (
            [json.loads(line) for line in receipts.read_text().splitlines()]
            if receipts.exists()
            else []
        )
        usage_details = {
            "model": self.model,
            "elapsed_ms": elapsed,
            "memory_context_tokens": sum(h.get("context_tokens", 0) for h in hooks),
            "phase": "scoring"
            if label.startswith("scored-")
            else "training"
            if not (self.directory / f"learned-{family}.json").exists()
            else "validation",
            "comparison_group": f"{self.directory.name}-{family}-{variant}"
            if label.startswith("scored-")
            else None,
            "comparison_arm": arm if label.startswith("scored-") else None,
        }
        queue = HookQueue(state, client.capture_identity)
        capture_error, discrepancies = None, []
        token, raw_token, trace, capture_ok = None, None, None, False
        try:
            for item in queue.listing():
                while queue.deliver(client, item["token"]):
                    pass
            captured = queue.listing()
            token = raw_token = captured[0]["token"] if len(captured) == 1 else None
            capture_ok = (
                bool(token)
                and captured[0]["ended"] == 1
                and not (set(json.loads(captured[0]["gaps"])) - {"unverified_exit_status"})
                and captured[0]["pending"] == 0
            )
            trace = service.get_run(principal, token) if token else None
            if capture_ok:
                try:
                    _, discrepancies = audit_tools(trace, messages)
                    token = queue.reconcile(
                        client,
                        raw_token,
                        messages,
                        validation["passed"] and returncode == 0,
                        usage_details,
                    )
                    trace = service.get_run(principal, token)
                except (RuntimeError, ValueError) as error:
                    capture_ok, capture_error = False, str(error)
            if raw_token and captured[0]["ended"]:
                queue.close_incomplete(
                    raw_token, "Finished benchmark capture lacks audited outcome"
                )
                while queue.deliver(client, raw_token):
                    pass
                if not capture_ok:
                    trace = service.get_run(principal, raw_token)
            if raw_token and not capture_ok and capture_error is None:
                capture_error = "Native capture has gaps or pending actions"
        finally:
            queue.close()
        write_json(
            run_dir / "capture-audit.json",
            {
                "native_run_token": raw_token,
                "audited_run_token": token,
                "status_corrections": discrepancies,
            },
        )
        tools = [event for event in trace["events"] if event["kind"] == "tool"] if trace else []
        hooks = (
            [json.loads(line) for line in receipts.read_text().splitlines()]
            if receipts.exists()
            else []
        )
        diff = ""
        # Preserve the complete changed source, paired original hashes, and a
        # unified patch; no agent-editable verifier decides success.
        originals = fixture_files(family, variant)
        for name, original_source in originals.items():
            actual = (project / name).read_text()
            if actual != original_source:
                diff += "".join(
                    difflib.unified_diff(
                        original_source.splitlines(True), actual.splitlines(True), name, name
                    )
                )
        (run_dir / "changes.patch").write_text(diff)
        write_json(run_dir / "verification.json", validation)
        write_json(run_dir / "trace.json", trace)
        result = {
            "label": label,
            "family": family,
            "variant": variant,
            "arm": arm,
            "prompt_index": prompt_index,
            "returncode": returncode,
            "passed": validation["passed"] and returncode == 0,
            "capture_ok": capture_ok,
            "capture_error": capture_error,
            "run_token": token,
            "usage_available": usage_available,
            "usage": used,
            "uncached_input_tokens": used["input_tokens"] - used["cached_input_tokens"]
            if usage_available
            else None,
            **cli_counts,
            "captured_tool_calls": len(tools),
            "duration_ms": round(elapsed),
            "verifier_checks": validation["behavioral_checks"],
            "context_characters": sum(h["context_characters"] for h in hooks),
            "recall_injections": sum(bool(h["context"]) for h in hooks),
            "contexts_truncated": sum(h["truncated"] for h in hooks),
            "candidate_tokens": sorted({t for h in hooks for t in h["candidate_tokens"]}),
            "status_corrections": len(discrepancies),
            "search_latencies_ms": [
                search["latency_ms"] for h in hooks for search in h.get("searches", [])
            ],
        }
        write_json(result_path, result)
        if token and not capture_ok:
            service.record_session_usage(
                principal,
                token,
                SessionUsageInput(
                    source="codex-cli",
                    **usage_details,
                    **used,
                    tool_calls=cli_counts["tool_calls"],
                    capture_complete=False,
                ),
            )
        print(json.dumps(result), flush=True)
        return result

    def evaluate(self, repeats):
        frozen = self.directory / "frozen-banks.json"
        if not frozen.exists():
            self.manifest["source_sha256_at_scoring"] = {
                str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in [
                    ROOT / "benchmarks/evaluate_codex.py",
                    ROOT / "benchmarks/codex_fixtures.py",
                    *sorted((ROOT / "packages/musclememory").glob("*.py")),
                ]
            }
            write_json(self.metadata, self.manifest)
        for name, expected in self.manifest["source_sha256_at_scoring"].items():
            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
                raise RuntimeError("Scored implementation changed; use a new campaign")
        banks = {mode: snapshot(service, self.namespace) for mode, service in self.services.items()}
        if frozen.exists():
            assert json.loads(frozen.read_text()) == banks, (
                "Procedure bank changed during evaluation"
            )
        else:
            write_json(frozen, banks)
        for repetition in range(repeats):
            for family_index, family in enumerate(self.manifest["families"]):
                # Rotate arms to counterbalance cache, load, and time-order effects.
                offset = (family_index + repetition) % len(self.arms)
                order = self.arms[offset:] + self.arms[:offset]
                for arm in order:
                    for name, expected in self.manifest["source_sha256_at_scoring"].items():
                        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
                            raise RuntimeError("Scored implementation changed; use a new campaign")
                    result = self.run(
                        f"scored-{repetition}-{family}-{arm}",
                        family,
                        100 + 10 * family_index + repetition,
                        arm,
                        1 + repetition % 2,
                    )
                    assert {
                        m: snapshot(s, self.namespace) for m, s in self.services.items()
                    } == banks
                    target = json.loads((self.directory / f"learned-{family}.json").read_text())
                    delivered = (
                        arm == "off"
                        or target[MODE]["procedure_token"] in result["candidate_tokens"]
                    )
                    # Memory-off sessions use no capture in scoring: an incomplete
                    # capture there is recorded, not fatal. MuscleMemory sessions must
                    # have a complete capture and the target procedure delivered.
                    if arm == MODE and (not result["capture_ok"] or not delivered):
                        self.export()
                        raise RuntimeError(
                            "Invalid capture or missing guidance; scored result retained"
                        )

    def export(self):
        output = ROOT / "benchmarks/results/codex" / self.directory.name
        output.mkdir(parents=True, exist_ok=True)
        replacements = [
            (str(self.directory), "$CAMPAIGN"),
            (str(PRIVATE), "$PRIVATE_INSTALL"),
            (str(ROOT), "$MUSCLEMEMORY"),
            (str(Path.home()), "$HOME"),
        ]

        def sanitize(content):
            for old, new in replacements:
                content = content.replace(old, new)
            return content

        banks = {}
        for mode, service in self.services.items():
            banks[mode] = {}
            for family in FAMILIES:
                learned = json.loads((self.directory / f"learned-{family}.json").read_text())
                banks[mode][family] = service.inspect(
                    self.principals[mode],
                    InspectInput(
                        procedure_token=learned[mode]["procedure_token"], revision_selector="latest"
                    ),
                )
        write_json(output / "banks.json", json.loads(sanitize(json.dumps(banks))))
        write_json(
            output / "frozen-banks.json",
            json.loads((self.directory / "frozen-banks.json").read_text()),
        )
        results = []
        cases = []
        directories = list((self.directory / "runs").iterdir())
        directories.extend(
            d for d in (self.seed / "runs").iterdir() if d.name.startswith("warmup-")
        )
        for directory in sorted(directories):
            if not (directory / "result.json").exists():
                continue
            record = json.loads((directory / "result.json").read_text())
            messages = [
                json.loads(line) for line in (directory / "codex.jsonl").read_text().splitlines()
            ]
            record.setdefault("captured_tool_calls", record["tool_calls"])
            record.update(codex_tool_counts(messages))
            hooks = [
                json.loads(line) for line in (directory / "hooks.jsonl").read_text().splitlines()
            ]
            searches = [s for h in hooks for s in h["searches"]]
            contexts = {}
            for receipt in hooks:
                context = receipt.pop("context")
                context_id = hashlib.sha256(context.encode()).hexdigest() if context else None
                receipt["context_sha256"] = context_id
                if context_id:
                    contexts[context_id] = context
            record["returned_candidate_tokens"] = sorted(
                {token for search in searches for token in search["candidate_tokens"]}
            )
            if searches:
                target_token = banks[MODE][record["family"]]["procedure_token"]
                record["prompt_top1_correct"] = (
                    bool(searches[0]["candidate_tokens"])
                    and searches[0]["candidate_tokens"][0] == target_token
                )
                prompt_context = next(iter(contexts.values()), "")
                titles = re.findall(r'"title":\s*"([^"]+)"', prompt_context)
                record["correct_title_emitted_at_prompt"] = (
                    banks[MODE][record["family"]]["definition"]["title"] in titles
                )
            results.append(record)
            case = {
                "result": record,
                "hook_receipts": hooks,
                "contexts": contexts,
                "model_events": [
                    json.loads(line)
                    for line in (directory / "codex.jsonl").read_text().splitlines()
                ],
                "model_stderr": (directory / "codex.stderr").read_text(),
                "trace": json.loads((directory / "trace.json").read_text()),
                "verification": json.loads((directory / "verification.json").read_text()),
                "capture_audit": json.loads((directory / "capture-audit.json").read_text()),
                "fixture_hashes": json.loads((directory / "fixture-hashes.json").read_text()),
                "prompt": (directory / "prompt.txt").read_text(),
                "patch": (directory / "changes.patch").read_text(),
            }
            with sqlite3.connect(directory / "state/hooks.sqlite") as queue:
                case["capture_gaps"] = [
                    json.loads(row[0])
                    for row in queue.execute("SELECT gaps FROM runs")
                    if json.loads(row[0])
                ]
            cases.append(json.loads(sanitize(json.dumps(case))))
        with (output / "cases.jsonl").open("w") as file:
            for case in cases:
                serialized = json.dumps(case)
                if any(key in serialized for key in self.keys.values()):
                    raise RuntimeError("Credential detected in an export")
                file.write(serialized + "\n")
        scored = [r for r in results if r["label"].startswith("scored-")]
        totals = {}
        for arm in self.arms:
            rows = [r for r in scored if r["arm"] == arm]
            if rows:
                totals[arm] = {
                    "runs": len(rows),
                    "passed": sum(r["passed"] for r in rows),
                    "tool_calls": sum(r["tool_calls"] for r in rows),
                    "failed_tool_calls": measured_sum(r["failed_tool_calls"] for r in rows),
                    "complete_captures": sum(r["capture_ok"] for r in rows),
                    "measured_token_runs": sum(r["usage_available"] for r in rows),
                    "input_tokens": measured_sum(r["usage"]["input_tokens"] for r in rows),
                    "cached_input_tokens": measured_sum(
                        r["usage"]["cached_input_tokens"] for r in rows
                    ),
                    "uncached_input_tokens": measured_sum(r["uncached_input_tokens"] for r in rows),
                    "output_tokens": measured_sum(r["usage"]["output_tokens"] for r in rows),
                    "duration_ms": sum(r["duration_ms"] for r in rows),
                    "median_duration_ms": statistics.median(r["duration_ms"] for r in rows),
                    "context_characters": sum(r["context_characters"] for r in rows),
                    "contexts_truncated": sum(r["contexts_truncated"] for r in rows),
                    "accuracy": sum(r["passed"] for r in rows) / len(rows),
                    "prompt_top1_correct": sum(r.get("prompt_top1_correct", False) for r in rows)
                    if arm != "off"
                    else None,
                    "correct_title_emitted_at_prompt": sum(
                        r.get("correct_title_emitted_at_prompt", False) for r in rows
                    )
                    if arm != "off"
                    else None,
                    "search_latency_p50_ms": statistics.median(
                        latency for r in rows for latency in r["search_latencies_ms"]
                    )
                    if any(r["search_latencies_ms"] for r in rows)
                    else None,
                }
        protocol = deepcopy(self.manifest)
        protocol["integration"] = "Native Codex hooks over local loopback REST"
        protocol["source_sha256_at_campaign_creation"] = protocol.pop("source_sha256")
        protocol["source_sha256_at_export"] = {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in protocol["source_sha256_at_campaign_creation"]
        }
        warmup = [r for r in results if r["label"].startswith("warmup-")]
        report = {
            "protocol": protocol,
            "totals": totals,
            "results": results,
            "warmup": {
                "runs": len(warmup),
                "passed": sum(r["passed"] for r in warmup),
                "incomplete_captures": sum(not r["capture_ok"] for r in warmup),
                "input_tokens": measured_sum(r["usage"]["input_tokens"] for r in warmup),
                "cached_input_tokens": measured_sum(
                    r["usage"]["cached_input_tokens"] for r in warmup
                ),
                "output_tokens": measured_sum(r["usage"]["output_tokens"] for r in warmup),
                "duration_ms": sum(r["duration_ms"] for r in warmup),
            },
        }
        write_json(output / "results.json", json.loads(sanitize(json.dumps(report))))
        for filename in (
            "seed-evidence.json",
            *(p.name for p in self.directory.glob("preflight*.json")),
        ):
            if (self.directory / filename).exists():
                write_json(
                    output / filename, json.loads(sanitize((self.directory / filename).read_text()))
                )
        hashes = {
            str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(output.rglob("*"))
            if p.is_file() and p.name != "SHA256.json"
        }
        write_json(output / "SHA256.json", hashes)
        print(json.dumps({"report": str(output), "totals": totals}), flush=True)

    def close(self):
        for client in self.clients.values():
            client.close()
        # Keep databases, receipts, workspaces and services; remove only the
        # copied account credentials from the isolated benchmark Codex home.
        (self.home / "auth.json").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "prepare", "preflight", "hook", "export"])
    parser.add_argument("--campaign")
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--port", type=int, default=8098)
    parser.add_argument(
        "--seed-campaign", help="Recompile archived verified traces in a new namespace"
    )
    parser.add_argument("--config")
    parser.add_argument("--receipts")
    args = parser.parse_args()
    if args.action == "hook":
        hook(args.config, args.receipts)
        return
    directory = (
        PRIVATE
        / "codex-evaluations"
        / (args.campaign or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    campaign = Campaign(directory, args.model, args.seed_campaign, args.port)
    try:
        campaign.freeze(args.repeats)
        if args.action in {"run", "prepare", "preflight"}:
            campaign.seed_archived_evidence()
            if args.action == "preflight" or not (campaign.directory / "preflight.json").exists():
                campaign.preflight()
            readiness = json.loads((campaign.directory / "preflight.json").read_text())
            if not readiness["passed"]:
                raise RuntimeError("Failed preflight; no paid calls allowed")
            for name, expected in readiness["source_sha256"].items():
                if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
                    raise RuntimeError("Preflight implementation changed; replay again first")
        if args.action == "run":
            campaign.evaluate(args.repeats)
        if args.action not in {"prepare", "preflight"}:
            campaign.export()
    finally:
        campaign.close()


if __name__ == "__main__":
    main()
