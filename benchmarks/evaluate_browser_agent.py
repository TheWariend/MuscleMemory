"""Real Codex browser evaluation; defaults to the six-run smoke campaign.

  .venv/bin/python benchmarks/evaluate_browser_agent.py
  .venv/bin/python benchmarks/evaluate_browser_agent.py --resume RESULTS_DIRECTORY

Only explicitly request --full after reviewing smoke results. Full mode scores 36
runs. Each score starts from the same frozen, approved store. All model calls use
codex exec over stdio MCP, never a scripted browser or a replacement agent loop.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from musclememory.cli import migrate
from musclememory.config import Settings
from musclememory.core import InspectInput
from musclememory.receipts import codex_usage
from musclememory.sdk import MuscleMemoryClient
from musclememory.service import MemoryService

try:
    from .browser_site import Portal
    from .browser_tasks import MUTATIONS, TASKS, task_prompt
except ImportError:
    from browser_site import Portal
    from browser_tasks import MUTATIONS, TASKS, task_prompt

ROOT = Path(__file__).resolve().parents[1]
ARMS = ("baseline", "skill")
ACCOUNT_ERROR = (
    "insufficient_quota",
    "usage_limit_reached",
    "usage limit",
    "credit balance",
    "insufficient credits",
    "out of credits",
    "exceeded your current quota",
    "rate_limit_exceeded",
    "authentication",
    "unauthorized",
    "401 unauthorized",
    "not supported when using codex",
    "model_not_found",
    "not have access to",
    "refresh token",
    "not logged in",
    "account deactivated",
)


class AccountStop(RuntimeError):
    pass


def public_path(value):
    """Saved results name files relative to the repository, never by local path."""
    return str(value).replace(str(ROOT) + "/", "").replace(str(ROOT), ".")


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def read_jsonl(path):
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass  # interrupted final line; the original raw file is preserved
    return rows


def account_error(directory):
    errors = (directory / "stderr.log").read_text()
    messages = read_jsonl(directory / "codex.jsonl")
    errors += json.dumps([m for m in messages if m.get("type") in {"error", "turn.failed"}])
    return any(marker in errors.lower() for marker in ACCOUNT_ERROR)


def source_hashes():
    paths = [
        *sorted((ROOT / "packages/musclememory").rglob("*.py")),
        ROOT / "pyproject.toml",
        ROOT / "requirements.lock",
        *(
            ROOT / "benchmarks" / name
            for name in ("browser_tasks.py", "browser_site.py", "evaluate_browser_agent.py")
        ),
    ]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def plan(full=False):
    forms = TASKS if full else TASKS[:1]
    runs = []
    for form_index, task in enumerate(forms):
        for index in range(3):
            runs.append(
                dict(
                    id=f"learn-{task.key}-{index + 1}",
                    form=task.key,
                    phase="learn",
                    variant="practice",
                    seed=100 + form_index * 10 + index,
                    arm="baseline",
                )
            )
    # A separate namespace with a real automatic-approval learning run. It does
    # not contaminate the manually checked/approved bank or the scored sample.
    runs.append(
        dict(
            id="auto-approve-equipment",
            form="equipment",
            phase="auto_approve",
            variant="practice",
            seed=199,
            arm="baseline",
        )
    )
    for form_index, task in enumerate(forms):
        for variant_index, variant in enumerate(MUTATIONS if full else ("renamed",)):
            for repeat in range(2 if full else 1):
                seed = 10000 + form_index * 100 + variant_index * 10 + repeat
                arms = ARMS if (form_index + variant_index + repeat) % 2 == 0 else ARMS[::-1]
                for arm in arms:
                    runs.append(
                        dict(
                            id=f"score-{task.key}-{variant}-{seed}-{arm}",
                            form=task.key,
                            phase="score",
                            variant=variant,
                            seed=seed,
                            arm=arm,
                        )
                    )
    return runs


def prompt_for(run, home):
    task = next(task for task in TASKS if task.key == run["form"])
    prompt = task_prompt(task, run["seed"], home)
    prompt += f' For browser_open task, use this task description: "{task.intent}".'
    if run["arm"] == "skill":
        prompt += "\nCheck for a proven skill before doing it by hand."
    return prompt


def mcp_result(item):
    result = item.get("result") or {}
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except (KeyError, ValueError):
                continue
    return result


def measurements(messages, calls):
    usage = codex_usage(messages)
    input_tokens, cached, output = (
        usage[k] for k in ("input_tokens", "cached_input_tokens", "output_tokens")
    )
    usage["total_tokens"] = input_tokens + output if None not in (input_tokens, output) else None
    usage["uncached_tokens"] = (
        input_tokens - cached + output if None not in (input_tokens, cached, output) else None
    )
    items = [m["item"] for m in messages if m.get("type") == "item.completed"]
    receipts = [i for i in items if i.get("type") == "mcp_tool_call"]
    counts = Counter(row["tool"] for row in calls)
    found = [mcp_result(i) for i in receipts if i.get("tool") == "musclememory_find_skills"]
    executed = [mcp_result(i) for i in receipts if i.get("tool") == "musclememory_run_skill"]
    done = [mcp_result(i) for i in receipts if i.get("tool") == "browser_done"]
    forbidden = [
        i
        for i in items
        if i.get("type") in {"command_execution", "file_change", "web_search", "collab_tool_call"}
        or (i.get("type") == "mcp_tool_call" and i.get("server") != "browser")
    ]
    return {
        "usage": usage,
        # CLI turn.completed is a user turn; agent_messages counts visible
        # model messages separately. Do not mislabel tool calls as turns.
        "turns": sum(m.get("type") == "turn.completed" for m in messages),
        "agent_messages": sum(i.get("type") == "agent_message" for i in items),
        "browser_tool_calls": len(calls),
        "tool_counts": dict(counts),
        "receipt_tool_calls": len(receipts),
        "find_skills_called": bool(counts["musclememory_find_skills"]),
        "skill_found": any(bool(result.get("skills")) for result in found),
        "run_skill_called": bool(counts["musclememory_run_skill"]),
        "run_skill_returned": executed,
        "skill_finished": any(bool(result.get("ready")) for result in executed),
        "browser_done_returned": done,
        "browser_done_called": bool(counts["browser_done"]),
        "tool_audit_pass": not forbidden and len(receipts) == len(calls),
        "forbidden_tools": forbidden,
    }


def summarize(runs, expected):
    summary = {}
    for arm in ARMS:
        sample = [r for r in runs if r["phase"] == "score" and r["arm"] == arm]

        def median(get, sample=sample):
            values = [get(r) for r in sample]
            return statistics.median(values) if values and None not in values else None

        summary[arm] = {
            "runs": len(sample),
            "planned_runs": sum(r["phase"] == "score" and r["arm"] == arm for r in expected),
            "passes": sum(r["judge"]["passed"] for r in sample),
            "pass_rate": sum(r["judge"]["passed"] for r in sample) / len(sample)
            if sample
            else None,
            "wrong_submissions": sum(r["judge"]["wrong_submissions"] for r in sample),
            "median_browser_tool_calls": median(lambda r: r["browser_tool_calls"]),
            "median_total_tokens": median(lambda r: r["usage"]["total_tokens"]),
            "median_uncached_tokens": median(lambda r: r["usage"]["uncached_tokens"]),
            "median_wall_seconds": median(lambda r: r["wall_seconds"]),
            **{
                key: sum(r[key] for r in sample)
                for key in (
                    "find_skills_called",
                    "skill_found",
                    "run_skill_called",
                    "skill_finished",
                )
            },
        }
    return summary


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def terminate(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    else:
        # MCP/browser descendants can survive an early CLI failure.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def database_copy(source, destination):
    destination.unlink(missing_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)


class Campaign:
    def __init__(self, directory, model, reasoning, full, resume, timeout):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.runtime = self.directory / ".runtime"
        self.runtime.mkdir(mode=0o700, exist_ok=True)
        self.timeout = timeout
        self.codex = shutil.which("codex")
        if not self.codex:
            raise RuntimeError("codex is not installed")
        version = subprocess.check_output([self.codex, "--version"], text=True).strip()
        metadata = self.directory / "campaign.json"
        if metadata.exists():
            if not resume:
                raise ValueError("Campaign already exists; use --resume")
            self.manifest = json.loads(metadata.read_text())
            for key, value in {
                "model": model,
                "reasoning": reasoning,
                "cli_version": version,
                "full": full,
                "source_hashes": source_hashes(),
            }.items():
                if self.manifest[key] != value:
                    raise ValueError(f"Cannot resume after changing {key}")
        else:
            self.manifest = {
                "model": model,
                "reasoning": reasoning,
                "cli_version": version,
                "full": full,
                "source_hashes": source_hashes(),
                "source_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "embedding_provider": "lexical-hash",
                "embedding_model": "local-lexical-v1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "site_port": free_port(),
                "memory_port": free_port(),
                "plan": plan(full),
                "limits": [
                    "Synthetic local service portal",
                    "One model and reasoning setting",
                    "Small sample; no generalization claims",
                ],
            }
            write_json(metadata, self.manifest)
        self.home = self.runtime / "codex-home"
        self.home.mkdir(mode=0o700, exist_ok=True)
        auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
        if auth.exists():
            shutil.copyfile(auth, self.home / "auth.json")
            (self.home / "auth.json").chmod(0o600)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("MM_", "CODEX_"))}
        self.env.update(
            CODEX_HOME=str(self.home),
            MM_EMBEDDING_PROVIDER="lexical-hash",
            MM_EMBEDDING_MODEL="local-lexical-v1",
            MM_EMBEDDING_DIM="128",
            MM_REQUESTS_PER_MINUTE="10000",
            MM_AUTO_APPROVE="false",
        )
        self.bank = self.runtime / "learn.db"
        # Materialize a fresh database via real migrations; never the user's store.
        settings = Settings(
            database_url="sqlite:///" + str(self.bank),
            embedding_provider="lexical-hash",
            requests_per_minute=10000,
        )
        migrate(settings.database_url)
        self.service = MemoryService(settings)
        credentials = self.runtime / "credentials.json"
        if credentials.exists():
            self.keys = json.loads(credentials.read_text())
        else:
            self.keys = {}
            for namespace in ("browser-eval", "auto-confirm"):
                self.keys[namespace] = {
                    role: self.service.issue_key("browser-eval", {namespace: grants})["api_key"]
                    for role, grants in {
                        "agent": ["read", "write"],
                        "admin": ["read", "write", "admin"],
                    }.items()
                }
            write_json(credentials, self.keys)
            credentials.chmod(0o600)

    @contextmanager
    def server(self, database, *, auto=False):
        env = {
            **self.env,
            "MM_DATABASE_URL": "sqlite:///" + str(database),
            "MM_AUTO_APPROVE": str(auto).lower(),
        }
        port = self.manifest["memory_port"]
        with (self.directory / "server.log").open("ab") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "musclememory.cli",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=env,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
            try:
                for _ in range(200):
                    if process.poll() is not None:
                        raise RuntimeError("MuscleMemory server exited; see server.log")
                    try:
                        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.2)
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
                else:
                    raise RuntimeError("MuscleMemory server startup timed out")
                yield f"http://127.0.0.1:{port}"
            finally:
                terminate(process)

    def execute(self, run, portal, server_url, state):
        directory = self.directory / "runs" / run["id"]
        result_path = directory / "result.json"
        if result_path.exists():
            return json.loads(result_path.read_text())
        if directory.exists():
            # Never replay an interrupted case and hide a duplicate submission.
            raise RuntimeError(f"Unfinalized run at {directory}; resume recovery must finalize it")
        directory.mkdir(parents=True)
        namespace = "auto-confirm" if run["phase"] == "auto_approve" else "browser-eval"
        config = self.runtime / (run["id"] + ".json")
        write_json(config, {"url": server_url, "namespace": namespace, "state_dir": str(state)})
        log = directory / "browser.jsonl"
        log.touch()
        command = shutil.which("musclememory", path=str(Path(sys.executable).parent))
        if not command:
            raise RuntimeError(
                "Install this checkout's musclememory CLI in the current environment"
            )
        args = ["browser", "--config", str(config), "--log", str(log)]
        if run["arm"] == "baseline":
            args.append("--no-skills")
        mcp = "{command=" + json.dumps(command) + ",args=" + json.dumps(args) + ",env={MM_API_KEY="
        mcp += (
            json.dumps(self.keys[namespace]["agent"])
            + " },startup_timeout_sec=60,tool_timeout_sec=180,required=true,"
            'default_tools_approval_mode="approve"}'
        )
        home = portal.add(
            run["id"], next(t for t in TASKS if t.key == run["form"]), run["seed"], run["variant"]
        )
        prompt = prompt_for(run, home)
        (directory / "prompt.txt").write_text(prompt + "\n")
        # Empty cwd/home: no repository sources, personal skills, hooks or other MCPs.
        with tempfile.TemporaryDirectory(prefix="browser-agent-", dir=self.runtime) as work:
            argv = [
                self.codex,
                "exec",
                "--json",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "-C",
                work,
                "-s",
                "read-only",
                "-m",
                self.manifest["model"],
                "-c",
                "model_reasoning_effort=" + json.dumps(self.manifest["reasoning"]),
                "-c",
                'web_search="disabled"',
                "--disable",
                "shell_tool",
                "--disable",
                "multi_agent",
                "-c",
                "mcp_servers.browser=" + mcp,
                "-",
            ]
            # Do not save the credential-bearing argv. This public config is reviewable.
            write_json(
                directory / "invocation.json",
                {
                    "cli_version": self.manifest["cli_version"],
                    "model": self.manifest["model"],
                    "reasoning": self.manifest["reasoning"],
                    "server_command": public_path(command),
                    "server_args": [public_path(a) for a in args],
                    "auto_approve": run["phase"] == "auto_approve",
                    "isolated_home": True,
                    "isolated_cwd": True,
                    "shell_tool": False,
                    "browser_approval_mode": "approve",
                },
            )
            started, stop_reason = time.monotonic(), None
            interrupted = None
            with (
                (directory / "codex.jsonl").open("w") as stdout,
                (directory / "stderr.log").open("w") as stderr,
                (directory / "prompt.txt").open() as stdin,
            ):
                process = subprocess.Popen(
                    argv,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    env=self.env,
                    start_new_session=True,
                )
                try:
                    next_progress = started + 20
                    while process.poll() is None:
                        time.sleep(0.25)
                        if account_error(directory):
                            stop_reason = "account_or_credit_error"
                            break
                        if time.monotonic() - started > self.timeout:
                            stop_reason = "timeout"
                            break
                        if time.monotonic() >= next_progress:
                            print(
                                f"  {run['id']}: {len(read_jsonl(log))} browser calls, "
                                f"{time.monotonic() - started:.0f}s",
                                flush=True,
                            )
                            next_progress += 20
                except KeyboardInterrupt as error:
                    stop_reason, interrupted = "interrupted", error
                finally:
                    terminate(process)
            # A CLI can exit before the polling loop observes it. Inspect its
            # final error events too, so account failures never start another run.
            if stop_reason is None and account_error(directory):
                stop_reason = "account_or_credit_error"
            result = self.finalize(
                run, portal, directory, time.monotonic() - started, process.returncode, stop_reason
            )
            if interrupted:
                raise interrupted
            if (
                stop_reason != "account_or_credit_error"
                and not result["browser_tool_calls"]
                and any(
                    i.get("item", {}).get("error") for i in read_jsonl(directory / "codex.jsonl")
                )
            ):
                raise RuntimeError("Browser MCP could not execute; inspect the saved CLI error")
            if stop_reason == "account_or_credit_error":
                raise AccountStop("Account/credit error: raw error saved; campaign stopped")
            return result

    def finalize(self, run, portal, directory, wall, exit_code, reason):
        result = {
            **run,
            **measurements(
                read_jsonl(directory / "codex.jsonl"), read_jsonl(directory / "browser.jsonl")
            ),
            "judge": portal.verdict(run["id"]),
            "wall_seconds": wall,
            "exit_code": exit_code,
            "stop_reason": reason,
        }
        # A submission pass stays separately visible even on protocol/CLI failure.
        result["protocol_pass"] = (
            result["judge"]["passed"]
            and result["browser_done_called"]
            and result["tool_audit_pass"]
            and exit_code == 0
        )
        write_json(directory / "result.json", result)
        print(
            f"{run['id']}: judge={result['judge']['passed']} "
            f"calls={result['browser_tool_calls']} time={wall if wall is not None else 0:.1f}s",
            flush=True,
        )
        return result

    def approve(self):
        receipt = self.directory / "approval.json"
        if receipt.exists():
            return
        index = self.runtime / "learn-state" / "browser-skills.json"
        tokens = {token for group in json.loads(index.read_text()).values() for token in group}
        principal = self.service.authenticate(self.keys["browser-eval"]["admin"], rate_limit=False)
        inspections = {
            t: self.service.inspect(principal, InspectInput(procedure_token=t)) for t in tokens
        }
        if len(tokens) != (3 if self.manifest["full"] else 1) or any(
            s["successes"] < 2 or s["failures"] for s in inspections.values()
        ):
            raise RuntimeError(
                "Learning did not produce one skill per form with two passing checks"
            )
        with MuscleMemoryClient(
            f"http://127.0.0.1:{self.manifest['memory_port']}", self.keys["browser-eval"]["admin"]
        ) as client:
            approvals = {t: client.http.post(f"/v1/procedures/{t}/approve").json() for t in tokens}
        if any(a.get("approved") is not True for a in approvals.values()):
            raise RuntimeError("Admin approval failed")
        write_json(receipt, {"inspections_before": inspections, "approvals": approvals})

    def automatic_confirmation(self, result):
        tokens = [r.get("learned") for r in result["browser_done_returned"] if r.get("learned")]
        if len(tokens) != 1 or not result["judge"]["passed"]:
            raise RuntimeError("Automatic-approval practice run did not learn successfully")
        principal = self.service.authenticate(self.keys["auto-confirm"]["admin"], rate_limit=False)
        inspection = self.service.inspect(principal, InspectInput(procedure_token=tokens[0]))
        if inspection["needs_review"] or inspection["successes"] != 0:
            raise RuntimeError(
                "MM_AUTO_APPROVE did not skip review while retaining independent checks"
            )
        write_json(
            self.directory / "auto-approval.json",
            {
                "MM_AUTO_APPROVE": True,
                "confirmed": True,
                "inspection": inspection,
                "limit": (
                    "One real learning run confirmed automatic review only; checks remain required"
                ),
            },
        )

    def report(self, status, error=None):
        runs = [
            json.loads(p.read_text()) for p in sorted(self.directory.glob("runs/*/result.json"))
        ]
        setup = [
            {"directory": p.parent.name, **json.loads(p.read_text())}
            for p in sorted(self.directory.glob("setup-attempt-*/summary.json"))
        ]
        summary = {
            "setup_attempts": setup,
            "status": status,
            "error": error,
            "campaign": self.manifest,
            "arms": summarize(runs, self.manifest["plan"]),
            "runs": runs,
        }
        write_json(self.directory / "summary.json", summary)
        lines = [
            f"Browser agent evaluation ({self.manifest['model']}, "
            f"{self.manifest['reasoning']}, {self.manifest['cli_version']})",
            "",
            f"Status: {status}",
            "",
            "| Arm | Pass | Wrong submissions | Median browser calls | Median tokens "
            "(total / uncached) | Median seconds | Found / called / finished |",
            "|---|---|---|---|---|---|---|",
        ]

        def fmt(value):
            return "unavailable" if value is None else f"{value:,.1f}".removesuffix(".0")

        for arm, data in summary["arms"].items():
            lines.append(
                f"| {arm} | {data['passes']}/{data['runs']} | {data['wrong_submissions']} | "
                f"{fmt(data['median_browser_tool_calls'])} | "
                f"{fmt(data['median_total_tokens'])} / {fmt(data['median_uncached_tokens'])} | "
                f"{fmt(data['median_wall_seconds'])} | {data['skill_found']} / "
                f"{data['run_skill_called']} / {data['skill_finished']} |"
            )
        lines += [
            "",
            "Failures and interrupted attempts remain in the measured denominator. "
            "Unavailable usage stays unavailable. Total = input + output; uncached = "
            "input − cached input + output. Calls count public MCP invocations; skill "
            "internals are not extra calls. Finished means run_skill returned ready for "
            "the agent's final submit. Judge compares saved payloads independently.",
            "",
            "Limits: synthetic local site, one model, small sample. Practice has 3 resource "
            "rows; scoring has 24. No claims beyond these measurements.",
        ]
        if setup:
            count = sum(len(s["runs"]) for s in setup)
            lines += [
                "",
                f"Setup failures preserved separately: {count} model invocations "
                "before any browser tool could execute, and before scoring began. "
                "Their raw logs, usage and source hashes are in setup-attempt-01/.",
            ]
        if error:
            lines += ["", "Stopped: " + error]
        (self.directory / "report.md").write_text("\n".join(lines) + "\n")
        return summary

    def run(self):
        status, error = "complete", None
        portal = Portal(self.manifest["site_port"], records=self.directory / "submissions.jsonl")
        try:
            with portal:
                # Finalize any process-crash attempt without retrying it. Its case
                # and submissions persist, and unknown time remains unavailable.
                for run in self.manifest["plan"]:
                    directory = self.directory / "runs" / run["id"]
                    if directory.exists() and not (directory / "result.json").exists():
                        portal.add(
                            run["id"],
                            next(t for t in TASKS if t.key == run["form"]),
                            run["seed"],
                            run["variant"],
                        )
                        self.finalize(run, portal, directory, None, None, "interrupted")
                learn_state = self.runtime / "learn-state"
                learn_state.mkdir(mode=0o700, exist_ok=True)
                with self.server(self.bank) as server:
                    for run in self.manifest["plan"]:
                        if run["phase"] == "learn":
                            self.execute(run, portal, server, learn_state)
                    self.approve()
                with self.server(self.bank, auto=True) as server:
                    run = next(r for r in self.manifest["plan"] if r["phase"] == "auto_approve")
                    state = self.runtime / "auto-state"
                    state.mkdir(mode=0o700, exist_ok=True)
                    result = self.execute(run, portal, server, state)
                    self.automatic_confirmation(result)
                frozen = self.runtime / "approved.db"
                if not frozen.exists():
                    database_copy(self.bank, frozen)
                for run in self.manifest["plan"]:
                    if run["phase"] != "score":
                        continue
                    if (self.directory / "runs" / run["id"] / "result.json").exists():
                        continue
                    # Each arm receives an identical bank. Its subsequent learning
                    # and evaluations cannot influence any other scored run.
                    with tempfile.TemporaryDirectory(prefix="score-", dir=self.runtime) as work:
                        work = Path(work)
                        database_copy(frozen, work / "memory.db")
                        shutil.copytree(learn_state, work / "state")
                        with self.server(work / "memory.db") as server:
                            self.execute(run, portal, server, work / "state")
                    self.report("running")
        except AccountStop as exc:
            status, error = "account_stopped", str(exc)
        except KeyboardInterrupt:
            status, error = "interrupted", "Interrupted by operator"
        except Exception as exc:
            status, error = "failed", str(exc)
        finally:
            self.service.db.engine.dispose()
            result = self.report(status, error)
        if error:
            print(error, file=sys.stderr)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--reasoning", default="medium", choices=("low", "medium", "high"))
    parser.add_argument("--full", action="store_true", help="Explicitly opt in to 36 scored runs")
    parser.add_argument("--resume", type=Path, help="Resume an existing campaign directory")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    directory = (
        args.resume
        or args.output
        or ROOT / "benchmarks/results" / ("browser-agent-" + datetime.now().strftime("%Y-%m-%d"))
    )
    campaign = Campaign(
        directory, args.model, args.reasoning, args.full, bool(args.resume), args.timeout
    )
    result = campaign.run()
    print(f"Report: {directory / 'report.md'}")
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
