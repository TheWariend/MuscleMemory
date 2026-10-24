"""Advisory native client hooks with a private, cross-process delivery queue.

Hooks preserve the client's approval settings and never run remembered actions
or infer task success from an agent stopping. Unknown tools and overlapping calls
prevent attestation.
"""

import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from . import workspace
from .artifacts import (
    PATH_KEYS,
    WORKSPACE_CHANGES,
    command_parts,
    documentation_only,
    inspection_only,
    operation_categories,
)
from .core import ExecutionEvent, RuntimeProfile
from .delivery import render_pointer
from .native_receipts import command_receipt, completed_tools
from .sdk import MuscleMemoryClient
from .security import redact
from .shapes import FILE_CHANGE

DEFAULT_TOOLS = {
    "claude": {
        "Read": ("filesystem.read", "read"),
        "Write": ("filesystem.write", "write"),
        "Edit": ("filesystem.edit", "write"),
        "Grep": ("filesystem.search", "read"),
        "Glob": ("filesystem.list", "read"),
        "Bash": ("shell.execute", "destructive"),
    },
    "codex": {
        "Bash": ("shell.execute", "destructive"),
        "apply_patch": ("filesystem.patch", "write"),
        "mcp__cua_repl__js": ("browser.interact", "destructive"),
    },
    "cursor": {
        "Read": ("filesystem.read", "read"),
        "Write": ("filesystem.write", "write"),
        "Grep": ("filesystem.search", "read"),
        "Delete": ("filesystem.delete", "destructive"),
        "Shell": ("shell.execute", "destructive"),
    },
}

# Native client utilities and known read-only lookups are context for a coding
# task, not actions to teach. Keep this an exact allowlist: an unknown MCP tool
# may change project files or external state. Explicit user mappings take priority.
CODEX_CONTEXT_TOOLS = {
    "webrun",
    "view_image",
    "clocksleep",
    "request_user_input",
    "request_user_input_async",
    "update_plan",
    # Attaching a PR changes chat metadata, not the project or the PR itself.
    "mcp__codex_app__attach_artifact",
    "mcp__codex_app__list_threads",
    "mcp__codex_app__read_thread",
    "mcp__codex_app__list_artifacts",
    "mcp__codex_app__read_thread_terminal",
    "mcp__codex_app__get_usage_limits",
    "mcp__codex_app__open_in_codex",
    "mcp__codex_app__load_workspace_dependencies",
    "mcp__codex_apps__plugin_management__search_plugins",
} | {
    f"mcp__codex_apps__github__{name}"
    for name in (
        "fetch",
        "fetch_file",
        "fetch_issue",
        "fetch_issue_comments",
        "fetch_pr_comments",
        "fetch_workflow_run_jobs",
        "fetch_workflow_job_logs",
        "fetch_pr",
        "fetch_commit",
        "fetch_blob",
        "fetch_commit_workflow_runs",
        "fetch_pr_file_patch",
        "fetch_pr_patch",
        "fetch_workflow_job_steps",
        "fetch_workflow_run_artifacts",
        "search",
        "search_issues",
        "search_prs",
        "search_commits",
        "search_branches",
        "compare_commits",
        "list_pr_changed_filenames",
        "list_repositories_by_affiliation",
        "list_repositories",
        "list_recent_issues",
        "list_pull_request_review_threads",
        "list_pull_request_reviews",
        "get_commit_combined_status",
    )
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def private_file(path, *, create=False):
    flags = (os.O_RDWR if create else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Hook files must be private regular files (0600)")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ValueError("Hook files must belong to the current user")
        if info.st_size > 256 * 1024 * 1024:
            raise ValueError("Hook queue exceeds 256 MiB; export and purge old runs")
    finally:
        os.close(descriptor)


def load_config(path, client):
    config = json.loads(Path(path).read_text())
    # The config is data. It may reference a private env file; never source a shell.
    env = dict(os.environ)
    if config.get("env_file"):
        private_file(config["env_file"])
        for line in Path(config["env_file"]).read_text().splitlines():
            name, separator, value = line.partition("=")
            if separator and name in {"MM_API_KEY", "MM_API_URL", "MM_NAMESPACE"}:
                env.setdefault(name, value.strip())
    key = env.get("MM_API_KEY")
    if not key:
        raise ValueError("Set MM_API_KEY or reference a private env_file")
    tools = dict(DEFAULT_TOOLS[client])
    for name, specification in config.get("tools", {}).get(client, {}).items():
        tools[name] = (specification["capability"], specification["effect"])
    profile = RuntimeProfile.model_validate(
        redact(
            RuntimeProfile(
                agent_name=client,
                bindings={capability: name for name, (capability, _) in tools.items()},
                environment=config.get("environment", {}),
                permissions=config.get("permissions", []),
            ).model_dump(mode="json")
        )
    )
    if len(profile.bindings) != len(tools):
        raise ValueError("Each tool needs a distinct canonical capability")
    if any(effect not in {"read", "write", "destructive"} for _, effect in tools.values()):
        raise ValueError("Tool effects must be read, write, or destructive")
    url = env.get("MM_API_URL", config.get("url", "http://127.0.0.1:8080"))
    endpoint = urlsplit(url)
    if (
        endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or (
            endpoint.scheme != "https"
            and not (
                endpoint.scheme == "http" and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        )
    ):
        raise ValueError("Use HTTPS or a local loopback endpoint without URL credentials")
    return (
        config,
        tools,
        profile,
        MuscleMemoryClient(url, key, timeout=2),
    )


class HookQueue:
    """SQLite transactions serialize separate hook processes and preserve exact retries."""

    def __init__(self, directory, identity):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
            raise ValueError("Hook state directory must be private (0700), without symlinks")
        self.path = self.directory / "hooks.sqlite"
        private_file(self.path, create=True)
        self.db = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.identity = identity
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                token TEXT PRIMARY KEY, identity TEXT NOT NULL, session TEXT NOT NULL,
                turn TEXT NOT NULL, scope TEXT NOT NULL, profile TEXT NOT NULL,
                ended INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0,
                gaps TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS hook_session ON runs(identity,session,created);
            CREATE TABLE IF NOT EXISTS events (
                run TEXT NOT NULL REFERENCES runs(token), sequence INTEGER NOT NULL,
                source TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                ack INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(run,sequence), UNIQUE(run,source));
            CREATE TABLE IF NOT EXISTS active (
                run TEXT NOT NULL, source TEXT NOT NULL, started REAL NOT NULL,
                PRIMARY KEY(run,source));
            CREATE TABLE IF NOT EXISTS pending_calls (
                run TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(run,source));
            CREATE TABLE IF NOT EXISTS claims (
                run TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL, path TEXT,
                PRIMARY KEY(run,source));
            CREATE TABLE IF NOT EXISTS overlaps (
                run TEXT NOT NULL, source TEXT NOT NULL, path TEXT NOT NULL,
                PRIMARY KEY(run,source,path));
            CREATE TABLE IF NOT EXISTS snapshots (
                run TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL,
                PRIMARY KEY(run,source));
            CREATE TABLE IF NOT EXISTS injections (
                identity TEXT NOT NULL, session TEXT NOT NULL, procedure TEXT NOT NULL,
                version INTEGER NOT NULL, context_hash TEXT NOT NULL, tokens INTEGER NOT NULL,
                PRIMARY KEY(identity,session,procedure,version));
            CREATE TABLE IF NOT EXISTS skips (
                identity TEXT NOT NULL, run TEXT NOT NULL, procedure TEXT NOT NULL,
                version INTEGER NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(identity,run,procedure,version));
            CREATE TABLE IF NOT EXISTS reconciliations (
                identity TEXT NOT NULL, run TEXT NOT NULL, derived TEXT NOT NULL,
                fingerprint TEXT NOT NULL, events TEXT NOT NULL,
                PRIMARY KEY(identity,run));
            CREATE TABLE IF NOT EXISTS attestations (
                identity TEXT NOT NULL, run TEXT NOT NULL, derived TEXT NOT NULL,
                succeeded INTEGER NOT NULL, PRIMARY KEY(identity,run));
            CREATE TABLE IF NOT EXISTS native_recoveries (
                identity TEXT NOT NULL, run TEXT NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY(identity,run));
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _drop_refused(self, token, transcript_path):
        """Forget calls the agent's transcript shows were refused before they ran.

        A call that never finished but already has an error result was stopped before it
        ran, by a permission prompt in a headless session, say. It changed nothing.
        """
        sources = {
            row[0] for row in self.db.execute("SELECT source FROM claims WHERE run=?", (token,))
        }
        if not sources or not isinstance(transcript_path, str):
            return
        try:
            lines = Path(transcript_path).read_text(errors="ignore").splitlines()[-4000:]
        except OSError:
            return
        refused = set()
        for line in lines:
            if '"tool_result"' not in line or '"is_error"' not in line:
                continue
            try:
                content = (json.loads(line).get("message") or {}).get("content")
            except ValueError:
                continue
            for item in content if isinstance(content, list) else []:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "tool_result"
                    and item.get("is_error") is True
                    and item.get("tool_use_id") in sources
                ):
                    refused.add(item["tool_use_id"])
        for source in refused:
            for table in ("claims", "active", "pending_calls", "snapshots", "overlaps"):
                self.db.execute(f"DELETE FROM {table} WHERE run=? AND source=?", (token, source))

    def _recover_codex_calls(self, payload, tools, profile, scope):
        """Recover a missing command finish hook only from its native execution receipt."""
        turn = str(payload.get("turn_id", ""))
        receipts = completed_tools(payload.get("transcript_path"), turn, payload.get("session_id"))
        if not receipts:
            return
        session = digest(["codex", payload.get("session_id")])
        pending = self.db.execute(
            "SELECT p.payload FROM pending_calls p JOIN runs r ON r.token=p.run "
            "JOIN active a ON a.run=p.run AND a.source=p.source "
            "WHERE r.identity=? AND r.session=? AND r.turn=? AND r.closed=0 ORDER BY p.rowid",
            (self.identity, session, turn),
        ).fetchall()
        for row in pending:
            call = json.loads(row[0])
            if call.get("tool_name") != "Bash":
                continue
            item = command_receipt(
                receipts.get(call.get("tool_use_id")), call.get("tool_input"), call.get("cwd")
            )
            if not item:
                continue
            self.handle(
                "codex",
                {
                    **call,
                    "hook_event_name": "PostToolUse",
                    "transcript_path": payload.get("transcript_path"),
                    "tool_response": {
                        "output": item.get("aggregated_output", ""),
                        "exit_code": item["exit_code"],
                        "capture_source": "codex_transcript",
                    },
                },
                tools,
                profile,
                scope,
                recovered=True,
            )

    def _settle_codex_results(self, payload):
        """Finalize queued stdout-only results once Codex publishes native metadata.

        A PostToolUse can run before item_completed is written. These events stay
        private and unacknowledged until the next hook or Stop can settle them.
        Uploaded events are never changed.
        """
        turn = str(payload.get("turn_id", ""))
        receipts = completed_tools(payload.get("transcript_path"), turn, payload.get("session_id"))
        if not receipts:
            return
        session = digest(["codex", payload.get("session_id")])
        with self.transaction():
            rows = self.db.execute(
                "SELECT p.run,p.source,p.payload,e.payload AS event FROM pending_calls p "
                "JOIN runs r ON r.token=p.run JOIN events e ON e.run=p.run AND e.source=p.source "
                "WHERE r.identity=? AND r.session=? AND r.turn=? AND r.closed=0 AND e.ack=0",
                (self.identity, session, turn),
            ).fetchall()
            for row in rows:
                pending = json.loads(row["payload"])
                if not pending.get("post"):
                    continue
                receipt = command_receipt(
                    receipts.get(row["source"]), pending.get("tool_input"), pending.get("cwd")
                )
                if not receipt:
                    continue
                event = json.loads(row["event"])
                response = event["response"]
                event["response"] = {
                    **(response if isinstance(response, dict) else {"output": response}),
                    "exit_code": receipt["exit_code"],
                    "capture_source": "codex_transcript",
                }
                if receipt["exit_code"] != 0 or receipt["status"] == "failed":
                    event["status"] = "failure"
                event["outcome_verified"] = not pending.get("overlapped")
                command = event["arguments"].get("command")
                if "verify" in operation_categories(event["arguments"], event["capability"]) and (
                    workspace.piped_test(command_parts(command), str(command))
                ):
                    verdict = workspace.test_output_verdict(workspace.output_text(response))
                    if verdict is False:
                        event["status"] = "failure"
                    elif verdict is None:
                        event["outcome_verified"] = False
                        self.gap(row["run"], "unverified_exit_status")
                self.db.execute(
                    "UPDATE events SET payload=? WHERE run=? AND source=? AND ack=0",
                    (json.dumps(event), row["run"], row["source"]),
                )
                self.db.execute(
                    "DELETE FROM pending_calls WHERE run=? AND source=?",
                    (row["run"], row["source"]),
                )

    def _release_pending_results(self, token):
        """Release received results whose native metadata never became available."""
        rows = self.db.execute(
            "SELECT e.payload FROM pending_calls p JOIN events e "
            "ON e.run=p.run AND e.source=p.source WHERE p.run=?",
            (token,),
        ).fetchall()
        if any(not json.loads(row[0]).get("outcome_verified", True) for row in rows):
            self.gap(token, "unverified_exit_status")
        self.db.execute(
            "DELETE FROM pending_calls WHERE run=? AND source IN "
            "(SELECT source FROM events WHERE run=?)",
            (token, token),
        )

    def _settle_previous_codex_turns(self, payload, tools):
        session = digest(["codex", payload.get("session_id")])
        rows = self.db.execute(
            "SELECT turn,profile,scope FROM runs WHERE identity=? AND session=? AND closed=0 "
            "AND turn!=? ORDER BY created DESC LIMIT 8",
            (self.identity, session, str(payload.get("turn_id", ""))),
        ).fetchall()
        for row in rows:
            previous = {**payload, "turn_id": row["turn"]}
            self._settle_codex_results(previous)
            self._recover_codex_calls(
                previous, tools, RuntimeProfile.model_validate_json(row["profile"]), row["scope"]
            )

    @staticmethod
    def _claim(name, arguments, tools, cwd=None):
        """What a call may touch: nothing (a read, a search, `ls`), one file (an edit or
        write), or anything (a shell command, a test run, a tool we don't know)."""
        if name not in tools:
            return "any", None
        capability, effect = tools[name]
        arguments = arguments if isinstance(arguments, dict) else {"input": arguments}
        if inspection_only(arguments, capability):
            return None, None
        operations = set(operation_categories(arguments, capability))
        if effect == "read" and not operations & {"modify", "verify"}:
            return None, None
        path = arguments.get("file_path")
        if capability in FILE_CHANGE and isinstance(path, str) and path:
            target = workspace.edit_target(path, cwd)
            if target is not None:
                return "file", target
        return "any", None

    def gap(self, run, reason):
        gaps = json.loads(
            self.db.execute("SELECT gaps FROM runs WHERE token=?", (run,)).fetchone()[0]
        )
        if reason not in gaps:
            gaps.append(reason)
        self.db.execute("UPDATE runs SET gaps=? WHERE token=?", (json.dumps(gaps[:100]), run))

    def append(self, run, source, fields, *, fingerprint=None):
        fingerprint = fingerprint or digest(fields)
        prior = self.db.execute(
            "SELECT fingerprint FROM events WHERE run=? AND source=?", (run, source)
        ).fetchone()
        if prior:
            if prior[0] != fingerprint:
                self.gap(run, "conflicting_duplicate")
            return
        row = self.db.execute("SELECT closed FROM runs WHERE token=?", (run,)).fetchone()
        sequence = self.db.execute("SELECT count(*) FROM events WHERE run=?", (run,)).fetchone()[0]
        if row[0] or sequence >= (1024 if fields["kind"] == "completion" else 1023):
            self.gap(run, "late_event_or_trace_limit")
            return
        event = redact(
            ExecutionEvent(event_id=f"e{sequence}", sequence=sequence, **fields).model_dump(
                mode="json"
            )
        )
        if len(json.dumps(event)) > 65536:
            event["response"] = {"$mm_capture_omitted": True}
        if len(json.dumps(event)) > 65536:
            event["arguments"] = {"$mm_capture_omitted": True}
            self.gap(run, "oversized_arguments")
        self.db.execute(
            "INSERT INTO events(run,sequence,source,fingerprint,payload) VALUES (?,?,?,?,?)",
            (run, sequence, source, fingerprint, json.dumps(event)),
        )

    def handle(self, client, payload, tools, profile, scope, *, recovered=False):
        event_name = payload.get("hook_event_name", "")
        if client == "codex" and event_name == "UserPromptSubmit":
            self._settle_previous_codex_turns(payload, tools)
        if client == "codex" and event_name in {"PreToolUse", "Stop", "Interrupt", "SessionEnd"}:
            # Recovery must happen outside the queue transaction; each recovered
            # result follows the same idempotent capture path as a normal hook.
            self._settle_codex_results(payload)
            self._recover_codex_calls(payload, tools, profile, scope)
        receipt = None
        if client == "codex" and payload.get("tool_name") == "Bash" and event_name == "PostToolUse":
            receipts = completed_tools(
                payload.get("transcript_path"),
                str(payload.get("turn_id", "")),
                payload.get("session_id"),
            )
            receipt = command_receipt(
                receipts.get(payload.get("tool_use_id")),
                payload.get("tool_input"),
                payload.get("cwd"),
            )
        # The session id alone names a session. The working directory is not part of it:
        # Claude Code reports the directory a `cd` moved to on that tool's PostToolUse.
        session = digest([client, payload.get("session_id", payload.get("conversation_id"))])
        if not payload.get("session_id", payload.get("conversation_id")):
            raise ValueError("Hooks require a client session identifier")
        turn = str(payload.get("turn_id", payload.get("generation_id", "")))
        # Shell commands can change code without an edit tool: note which project files
        # differ from version control around each one. Taken outside the queue lock.
        shell = str(tools.get(payload.get("tool_name"), ("", ""))[0]).startswith("shell.")
        tool_event = event_name in {
            "PreToolUse",
            "preToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "postToolUse",
            "postToolUseFailure",
        }
        include_paths = ()
        if shell and event_name in {
            "PostToolUse",
            "PostToolUseFailure",
            "postToolUse",
            "postToolUseFailure",
        }:
            previous_view = self.db.execute(
                "SELECT s.state FROM snapshots s JOIN runs r ON r.token=s.run "
                "WHERE r.identity=? AND r.session=? AND r.turn=? AND s.source=?",
                (self.identity, session, turn, payload.get("tool_use_id")),
            ).fetchone()
            if previous_view:
                include_paths = json.loads(previous_view[0])["files"]
        view = (
            workspace.snapshot(payload.get("cwd"), include_paths) if shell and tool_event else None
        )
        project = (
            view["root"]
            if view
            else workspace.project_root(payload.get("cwd"))[0]
            if tool_event
            else None
        )
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM runs WHERE identity=? AND session=? "
                + ("AND turn=? " if turn else "")
                + "ORDER BY created DESC LIMIT 1",
                (self.identity, session, turn) if turn else (self.identity, session),
            ).fetchone()
            if event_name in {"UserPromptSubmit", "beforeSubmitPrompt"}:
                prompt = payload.get("prompt", "")
                if not isinstance(prompt, str) or not prompt:
                    raise ValueError("Prompt hook requires prompt text")
                # Codex runs its own background prompts (task suggestions, safety checks)
                # in ephemeral threads, which keep no transcript. Nobody asked for them.
                if client == "codex" and "transcript_path" in payload:
                    if payload["transcript_path"] is None:
                        return None, "ephemeral_skipped"
                # Claude has no turn id. Every prompt starts a fresh recording, including
                # repeated identical prompts; the previous trace remains unverified.
                if not turn or not row:
                    # A new prompt proves the previous turn is no longer active,
                    # even when an interrupted client never emitted its Stop hook.
                    for prior in self.db.execute(
                        "SELECT token FROM runs WHERE identity=? AND session=? AND closed=0",
                        (self.identity, session),
                    ).fetchall():
                        self._end_unverified(
                            prior["token"],
                            "A new prompt arrived before this turn recorded an outcome",
                        )
                    token = str(uuid4())
                    started = time.time()
                    self.db.execute(
                        "INSERT INTO runs(token,identity,session,turn,scope,profile,created) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            token,
                            self.identity,
                            session,
                            turn,
                            scope,
                            json.dumps(profile.model_dump(mode="json")),
                            started,
                        ),
                    )
                    self.append(
                        token,
                        "intent",
                        {
                            "kind": "intent",
                            "intent_text": prompt[:8000],
                            "occurred_at": datetime.fromtimestamp(
                                started, timezone.utc
                            ).isoformat(),
                        },
                    )
                else:
                    token = row["token"]
                return token, "prompt"
            if not row:
                return None, "no_prompt"
            token = row["token"]
            if (
                row["profile"] != json.dumps(profile.model_dump(mode="json"))
                or row["scope"] != scope
            ):
                self.gap(token, "configuration_changed")
                return token, "configuration_changed"
            if event_name in {"Stop", "stop", "Interrupt", "SessionEnd", "sessionEnd"}:
                if row["closed"]:
                    return token, "ended"
                # A refused or abandoned read never ran or changed nothing; only an
                # unfinished change or command leaves the session's effects unknown.
                self._drop_refused(token, payload.get("transcript_path"))
                # A result was received but no matching native metadata appeared.
                # Release its saved output as unverified; never block the queue forever.
                self._release_pending_results(token)
                if self.db.execute("SELECT 1 FROM claims WHERE run=?", (token,)).fetchone():
                    self.gap(token, "unfinished_tool")
                self.db.execute("DELETE FROM active WHERE run=?", (token,))
                self.db.execute("UPDATE runs SET ended=1 WHERE token=?", (token,))
                passed = self._tests_passed(token) if event_name in {"Stop", "stop"} else None
                if passed and not row["closed"]:
                    self.append(
                        token,
                        "tests-passed",
                        {
                            "kind": "completion",
                            "status": "success",
                            "response": {
                                "verification": "Tests passed after the last change",
                                "verifier_event": passed,
                            },
                        },
                    )
                    self.db.execute("UPDATE runs SET closed=1 WHERE token=?", (token,))
                    return token, "verified"
                failed = self._tests_failed(token) if event_name in {"Stop", "stop"} else None
                if failed and not row["closed"]:
                    self.append(
                        token,
                        "tests-failed",
                        {
                            "kind": "completion",
                            "status": "failure",
                            "response": {
                                "verification": "Tests failed after the last change",
                                "verifier_event": failed,
                            },
                        },
                    )
                    self.db.execute("UPDATE runs SET closed=1 WHERE token=?", (token,))
                    return token, "failed"
                self._finish_unverified(token)
                return token, "ended"
            source = payload.get("tool_use_id")
            name = payload.get("tool_name")
            if not source or not isinstance(source, str):
                self.gap(token, "missing_tool_call_id")
                return token, "capture_gap"
            if name and "musclememory_" in name:
                return token, "memory_tool_skipped"
            if client == "codex" and name not in tools and name in CODEX_CONTEXT_TOOLS:
                return token, "context_tool_skipped"
            if event_name in {"PreToolUse", "preToolUse"}:
                if self.db.execute(
                    "SELECT 1 FROM events WHERE run=? AND source=?", (token, source)
                ).fetchone():
                    return token, "duplicate_start"
                # Reads overlap anything, and edits to different files overlap each other.
                # Two commands at once, or two edits of one file, leave the order unknown.
                # A command alongside edits is kept, but can't prove the tests passed.
                kind, path = self._claim(name, payload.get("tool_input"), tools, payload.get("cwd"))
                if kind:
                    self._drop_refused(token, payload.get("transcript_path"))
                rivals = (
                    self.db.execute(
                        "SELECT source, kind, path FROM claims WHERE run=? AND source!=?",
                        (token, source),
                    ).fetchall()
                    if kind
                    else []
                )
                for rival in rivals:
                    if kind == rival["kind"] and (
                        kind == "any" or workspace.overlapping_paths(path, rival["path"])
                    ):
                        self.gap(token, "parallel_tool_dependencies_unknown")
                    elif kind == "any":
                        self.db.execute(
                            "INSERT OR IGNORE INTO overlaps VALUES (?,?,?)",
                            (token, source, rival["path"]),
                        )
                    elif rival["kind"] == "any":
                        self.db.execute(
                            "INSERT OR IGNORE INTO overlaps VALUES (?,?,?)",
                            (token, rival["source"], path),
                        )
                self.db.execute(
                    "INSERT OR IGNORE INTO active VALUES (?,?,?)", (token, source, time.time())
                )
                if client == "codex":
                    # Store sanitized inputs so a later native receipt can close
                    # a missing PostToolUse without executing the command again.
                    saved = {
                        key: payload[key]
                        for key in (
                            "session_id",
                            "turn_id",
                            "cwd",
                            "tool_name",
                            "tool_use_id",
                            "tool_input",
                            "transcript_path",
                        )
                        if key in payload
                    }
                    if len(json.dumps(saved)) <= 65536:
                        self.db.execute(
                            "INSERT OR IGNORE INTO pending_calls VALUES (?,?,?)",
                            (token, source, json.dumps(redact(saved))),
                        )
                if kind:
                    self.db.execute(
                        "INSERT OR REPLACE INTO claims VALUES (?,?,?,?)",
                        (token, source, kind, path),
                    )
                if view:
                    self.db.execute(
                        "INSERT OR REPLACE INTO snapshots VALUES (?,?,?)",
                        (token, source, json.dumps(view)),
                    )
                return token, "started"
            if event_name not in {
                "PostToolUse",
                "PostToolUseFailure",
                "postToolUse",
                "postToolUseFailure",
            }:
                return token, "ignored"
            start = self.db.execute(
                "SELECT started FROM active WHERE run=? AND source=?", (token, source)
            ).fetchone()
            prior = self.db.execute(
                "SELECT payload,fingerprint FROM events WHERE run=? AND source=?", (token, source)
            ).fetchone()
            if recovered and prior:
                # A normal PostToolUse can win the race with receipt recovery.
                # Its already recorded result must not be replaced or duplicated.
                return token, "captured"
            # Derived exit metadata may arrive between retries. Idempotence is
            # based on the original hook, not on when that metadata became visible.
            fingerprint = digest(
                [
                    name,
                    payload.get("tool_input", {}),
                    payload.get("tool_response", payload.get("tool_output")),
                    event_name in {"PostToolUseFailure", "postToolUseFailure"},
                    payload.get("error", payload.get("error_message")),
                ]
            )
            if prior and prior["fingerprint"] == fingerprint:
                return token, "captured"
            if prior and receipt:
                previous = json.loads(prior["payload"]).get("response")
                if (
                    isinstance(previous, dict)
                    and previous.get("capture_source") == "codex_transcript"
                    and previous.get("exit_code") == receipt["exit_code"]
                ):
                    return token, "captured"  # a native recovery preceded this late hook
            measured_duration = (
                json.loads(prior[0])["duration_ms"]
                if prior
                else max(0, (time.time() - start[0]) * 1000)
                if start
                else 0
            )
            self.db.execute("DELETE FROM active WHERE run=? AND source=?", (token, source))
            self.db.execute("DELETE FROM pending_calls WHERE run=? AND source=?", (token, source))
            claimed = self.db.execute(
                "SELECT kind FROM claims WHERE run=? AND source=?", (token, source)
            ).fetchone()
            self.db.execute("DELETE FROM claims WHERE run=? AND source=?", (token, source))
            overlapped = [
                item[0]
                for item in self.db.execute(
                    "SELECT path FROM overlaps WHERE run=? AND source=?", (token, source)
                )
            ]
            self.db.execute("DELETE FROM overlaps WHERE run=? AND source=?", (token, source))
            if row["ended"]:
                self.gap(token, "tool_after_stop")
            if name not in tools:
                self.gap(token, "unmapped_tool:" + str(name)[:128])
                return token, "capture_gap"
            arguments = payload.get("tool_input", {})
            if not isinstance(arguments, dict):
                arguments = {"input": arguments}
            spellings = workspace.roots(payload.get("cwd"), project) if project else []
            if spellings:
                arguments = {
                    key: workspace.relative_path(value, spellings) if key in PATH_KEYS else value
                    for key, value in arguments.items()
                }
                if "command" in arguments:
                    arguments["command"] = workspace.relative_command(
                        arguments["command"], spellings
                    )
            before = self.db.execute(
                "SELECT state FROM snapshots WHERE run=? AND source=?", (token, source)
            ).fetchone()
            self.db.execute("DELETE FROM snapshots WHERE run=? AND source=?", (token, source))
            changed = workspace.changed_files(json.loads(before[0]) if before else None, view)
            if changed and claimed and claimed[0] == "file":
                # An edit changes its own file; another file changed meanwhile was another call.
                own = arguments.get("file_path")
                changed = [item for item in changed if item == own]
            if changed and overlapped:
                # Files that edits running alongside this command changed were theirs.
                theirs = {
                    workspace.relative_path(item, spellings) if spellings else item
                    for item in overlapped
                }
                changed = [item for item in changed if item not in theirs]
            if changed:
                arguments[WORKSPACE_CHANGES] = changed[:64]
            response = payload.get("tool_response", payload.get("tool_output"))
            if isinstance(response, str):
                try:
                    response = json.loads(response)
                except ValueError:
                    pass
            if receipt:
                # Native execution metadata settles the process result even when
                # the hook only contains stdout. Keep the original tool output.
                explicit_code = (
                    response.get("exitCode", response.get("exit_code"))
                    if isinstance(response, dict)
                    else None
                )
                if type(explicit_code) is int and explicit_code != receipt["exit_code"]:
                    self.gap(token, "conflicting_duplicate")
                response = {
                    **(response if isinstance(response, dict) else {"output": response}),
                    "exit_code": receipt["exit_code"],
                    "capture_source": "codex_transcript",
                }
                if receipt.get("status") == "failed":
                    response["isError"] = True
            output = workspace.output_text(response)
            failed = event_name in {"PostToolUseFailure", "postToolUseFailure"}
            outcome_verified = True
            deferred = False
            browser = tools[name][0] == "browser.interact"
            if browser:
                # A successful UI script is recorded, but is not proof that a
                # browser task achieved its goal. Browser skills use their adapter.
                outcome_verified = False
            if isinstance(response, dict):
                failed |= response.get("isError") is True or response.get("success") is False
                failed |= response.get("exitCode", response.get("exit_code", 0)) not in (None, 0)
            if client == "codex" and name == "Bash":
                deferred = (
                    not receipt
                    and isinstance(payload.get("transcript_path"), str)
                    and bool(payload["transcript_path"])
                    and not (
                        isinstance(payload.get("tool_response"), dict)
                        and type(response.get("exitCode", response.get("exit_code"))) is int
                    )
                )
                if deferred:
                    pending = {
                        "post": True,
                        "tool_input": payload.get("tool_input", {}),
                        "cwd": payload.get("cwd"),
                        "overlapped": bool(overlapped),
                    }
                    if len(json.dumps(pending)) <= 65536:
                        self.db.execute(
                            "INSERT OR REPLACE INTO pending_calls VALUES (?,?,?)",
                            (token, source, json.dumps(redact(pending))),
                        )
                    else:
                        deferred = False
                code = re.search(r"(?:Process exited with code|Exit code:)\s*(-?\d+)", output)
                failed |= bool(code and int(code[1]) != 0)
                explicit = (
                    isinstance(response, dict)
                    and type(response.get("exitCode", response.get("exit_code"))) is int
                    and (bool(receipt) or isinstance(payload.get("tool_response"), dict))
                )
                outcome_verified = bool(code or explicit or failed)
                if not outcome_verified and "verify" in operation_categories(
                    arguments, tools[name][0]
                ):
                    # Codex hooks often send output without the exit code; a test
                    # runner's own summary line ("OK", "FAILED (...)") settles it.
                    verdict = workspace.test_output_verdict(output)
                    if verdict is not None:
                        outcome_verified, failed = True, failed or not verdict
                if not outcome_verified and not deferred:
                    self.gap(token, "unverified_exit_status")
            command = arguments.get("command")
            if (
                shell
                and not failed
                and "verify" in operation_categories(arguments, tools[name][0])
                and workspace.piped_test(command_parts(command), str(command))
            ):
                # `tests | tail` exits with tail's status; trust the runner's own summary.
                verdict = workspace.test_output_verdict(output)
                if verdict is False:
                    failed = True
                elif verdict is None:
                    outcome_verified = False
                    if not deferred:
                        self.gap(token, "unverified_exit_status")
            if overlapped:
                # It ran while files were being edited, so it proves nothing about them.
                outcome_verified = False
            self.append(
                token,
                source,
                {
                    "kind": "tool",
                    "tool_name": name,
                    "capability": tools[name][0],
                    "effect": tools[name][1],
                    "arguments": arguments,
                    "response": payload.get("error", payload.get("error_message"))
                    if failed and response is None
                    else response,
                    "status": "failure" if failed else "success",
                    "outcome_verified": outcome_verified,
                    "exploratory": browser,
                    "duration_ms": max(
                        0,
                        payload.get("duration_ms", payload.get("duration", measured_duration)) or 0,
                    ),
                },
                fingerprint=fingerprint,
            )
            return token, "captured"

    def _tests_passed(self, token):
        """A session counts as verified when a test command passed after the last code change."""
        final = self._final_test(token)
        return final["event_id"] if final and final["status"] == "success" else None

    def _tests_failed(self, token):
        """A session failed when its last test command after the last code change failed."""
        final = self._final_test(token)
        return final["event_id"] if final and final["status"] == "failure" else None

    def _final_test(self, token):
        """The last test run with a known outcome after the last code change, or None."""

        gaps = set(
            json.loads(
                self.db.execute("SELECT gaps FROM runs WHERE token=?", (token,)).fetchone()[0]
            )
        )
        if gaps - {"unverified_exit_status"}:
            return None
        events = [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT payload FROM events WHERE run=? ORDER BY sequence", (token,)
            )
        ]
        last_change, final_test = None, None
        for index, event in enumerate(events):
            if event["kind"] != "tool":
                continue
            if event["capability"] == "browser.interact":
                # Local tests cannot verify an external browser workflow.
                return None
            operations = operation_categories(event["arguments"], event["capability"])
            # A shell command that changed files changed them even if it then failed.
            if (
                "modify" in operations
                and (event["status"] == "success" or event["arguments"].get(WORKSPACE_CHANGES))
                # Editing only documentation after the tests passed can't break them.
                and not documentation_only(event["arguments"], event["effect"], event["capability"])
            ):
                last_change, final_test = index, None
            # One command may change files and then run the tests (`script && tests`).
            if "verify" in operations:
                final_test = event if event.get("outcome_verified", True) else None
        return final_test if last_change is not None else None

    def injected(self, token):
        """The skill handed to this session at its prompt, if any."""
        row = self.db.execute(
            "SELECT i.procedure FROM injections i JOIN runs r ON r.session=i.session "
            "AND r.identity=i.identity WHERE r.token=? ORDER BY i.rowid DESC LIMIT 1",
            (token,),
        ).fetchone()
        return row[0] if row else None

    def deliver(self, client, token):
        # Serialize delivery too: a lost acknowledgment may safely repeat a batch;
        # a second sender cannot upload later events before the first batch.
        with self.transaction():
            run = self.db.execute(
                "SELECT * FROM runs WHERE token=? AND identity=?", (token, self.identity)
            ).fetchone()
            if not run:
                raise ValueError("Run does not belong to this endpoint and credential")
            rows = self.db.execute(
                "SELECT * FROM events WHERE run=? AND ack=0 ORDER BY sequence LIMIT 256", (token,)
            ).fetchall()
            batch, size = [], 0
            awaiting = {
                row[0]
                for row in self.db.execute("SELECT source FROM pending_calls WHERE run=?", (token,))
            }
            for row in rows:
                if row["source"] in awaiting:
                    break  # preserve ordering and finalize before the first upload
                size += len(row["payload"].encode())
                if size > 1024 * 1024:
                    break
                batch.append(json.loads(row["payload"]))
            if batch:
                result = client.musclememory_observe(
                    run_token=token,
                    runtime_profile=json.loads(run["profile"]),
                    visibility_scope=run["scope"],
                    event_batch=batch,
                )
                if result.get("rejected_event_count") != 0 or result.get(
                    "accepted_event_count", -1
                ) + result.get("duplicate_event_count", -1) != len(batch):
                    raise ValueError("Hook delivery did not acknowledge every event")
                self.db.executemany(
                    "UPDATE events SET ack=1 WHERE run=? AND sequence=?",
                    [(token, event["sequence"]) for event in batch],
                )
            return len(batch)

    def pending(self):
        return [
            row[0]
            for row in self.db.execute(
                "SELECT DISTINCT e.run FROM events e JOIN runs r ON e.run=r.token "
                "WHERE e.ack=0 AND r.identity=?",
                (self.identity,),
            )
        ]

    def pending_endings(self, token):
        """Retry closed turns in this session without uploading unrelated work."""
        return [
            row[0]
            for row in self.db.execute(
                "SELECT r.token FROM runs r JOIN runs current ON r.session=current.session "
                "AND r.identity=current.identity WHERE current.token=? AND r.identity=? "
                "AND r.closed=1 AND EXISTS (SELECT 1 FROM events e WHERE e.run=r.token AND ack=0) "
                "ORDER BY r.created LIMIT 8",
                (token, self.identity),
            )
        ]

    def _finish_incomplete(self, token, reason):
        run = self.db.execute("SELECT closed,gaps FROM runs WHERE token=?", (token,)).fetchone()
        if run["closed"]:
            return
        self.append(
            token,
            "incomplete-closure",
            {
                "kind": "completion",
                "status": "failure",
                "capture_complete": False,
                "response": {"reason": reason, "capture_gaps": json.loads(run["gaps"])},
            },
        )
        self.db.execute("UPDATE runs SET closed=1 WHERE token=?", (token,))

    def _finish_unverified(self, token, reason="No verified task outcome was recorded"):
        """A complete recording without task proof is not a missing recording."""
        run = self.db.execute("SELECT closed,gaps FROM runs WHERE token=?", (token,)).fetchone()
        if run["closed"]:
            return
        gaps = json.loads(run["gaps"])
        if set(gaps) - {"unverified_exit_status", "parallel_tool_dependencies_unknown"}:
            self._finish_incomplete(token, "Capture gaps: " + ", ".join(gaps))
            return
        self.append(
            token,
            "unverified-closure",
            {
                "kind": "completion",
                "status": "failure",
                "outcome_verified": False,
                "response": {"reason": reason, "capture_gaps": gaps},
            },
        )
        self.db.execute("UPDATE runs SET closed=1 WHERE token=?", (token,))

    def _end_unverified(self, token, reason):
        if self.db.execute("SELECT 1 FROM claims WHERE run=?", (token,)).fetchone():
            self.gap(token, "unfinished_tool")
        self.db.execute("DELETE FROM active WHERE run=?", (token,))
        self._release_pending_results(token)
        self.db.execute("UPDATE runs SET ended=1 WHERE token=?", (token,))
        self._finish_unverified(token, reason)

    def repair_endings(self):
        """Close legacy ended turns and turns demonstrably superseded in one session.

        Never infer success, rewrite delivered events, or expire a live turn by age.
        """
        with self.transaction():
            rows = self.db.execute(
                "SELECT r.token,r.ended FROM runs r WHERE r.identity=? AND r.closed=0 "
                "AND (r.ended=1 OR EXISTS (SELECT 1 FROM runs newer WHERE "
                "newer.identity=r.identity AND newer.session=r.session "
                "AND newer.created>r.created)) "
                "ORDER BY r.created",
                (self.identity,),
            ).fetchall()
            for row in rows:
                self._end_unverified(
                    row["token"],
                    (
                        "Session ended without a verified task outcome"
                        if row["ended"]
                        else "A new prompt arrived before this turn recorded an outcome"
                    ),
                )
            return [row["token"] for row in rows]

    def close_incomplete(self, token, reason):
        with self.transaction():
            row = self.db.execute(
                "SELECT ended FROM runs WHERE token=? AND identity=?", (token, self.identity)
            ).fetchone()
            if not row or not row["ended"]:
                raise ValueError("Incomplete closure requires an ended owned run")
            self._finish_incomplete(token, reason)

    def attest(self, token, succeeded):
        with self.transaction():
            run = self.db.execute(
                "SELECT * FROM runs WHERE token=? AND identity=?", (token, self.identity)
            ).fetchone()
            if not run or not run["ended"] or json.loads(run["gaps"]):
                raise ValueError("Attestation requires an ended, complete, unverified capture")
            prior = self.db.execute(
                "SELECT derived,succeeded FROM attestations WHERE identity=? AND run=?",
                (self.identity, token),
            ).fetchone()
            if prior:
                if bool(prior["succeeded"]) != succeeded:
                    raise ValueError("Attestation cannot change an existing outcome")
                return prior["derived"]
            source = token
            if run["closed"]:
                events = [
                    json.loads(row[0])
                    for row in self.db.execute(
                        "SELECT payload FROM events WHERE run=? ORDER BY sequence",
                        (token,),
                    )
                ]
                completion = events[-1]
                if completion.get("outcome_verified", True):
                    if completion["status"] != ("success" if succeeded else "failure"):
                        raise ValueError("Attestation cannot change an existing outcome")
                    return token
                # Delivered closures are immutable. A person's later attestation
                # gets a linked run; it cannot silently rewrite the original.
                token = str(uuid4())
                self.db.execute(
                    "INSERT INTO runs(token,identity,session,turn,scope,profile,ended,created) "
                    "VALUES (?,?,?,?,?,?,1,?)",
                    (
                        token,
                        self.identity,
                        digest(["attestation", token]),
                        token,
                        run["scope"],
                        run["profile"],
                        time.time(),
                    ),
                )
                for index, event in enumerate(events[:-1]):
                    event.pop("sequence")
                    event.pop("event_id")
                    self.append(token, f"attested-{index}", event)
                self.db.execute(
                    "INSERT INTO attestations VALUES (?,?,?,?)",
                    (self.identity, source, token, int(succeeded)),
                )
            self.append(
                token,
                "completion",
                {
                    "kind": "completion",
                    "status": "success" if succeeded else "failure",
                    "response": {"verifier": "human_attestation", "source_run": source},
                },
            )
            self.db.execute("UPDATE runs SET closed=1 WHERE token=?", (token,))
            return token

    def listing(self):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT token,ended,closed,gaps,created,(SELECT count(*) FROM events e "
                "WHERE e.run=r.token AND e.ack=0) AS pending FROM runs r WHERE identity=? "
                "ORDER BY created DESC LIMIT 100",
                (self.identity,),
            )
        ]

    def recall_once(self, client, token, intent, profile, scope, budget, cwd):
        from .delivery import token_count
        from .hook_files import check_files

        with self.transaction():
            run = self.db.execute(
                "SELECT * FROM runs WHERE token=? AND identity=?", (token, self.identity)
            ).fetchone()
            if not run or set(json.loads(run["gaps"])) - {"unverified_exit_status"}:
                return ""
            if (
                self.db.execute(
                    "SELECT count(*) FROM injections WHERE identity=? AND session=?",
                    (self.identity, run["session"]),
                ).fetchone()[0]
                >= 2
            ):
                return ""
            result = client.musclememory_recall(
                observation_run=token,
                intent_text=intent[:8000],
                runtime_profile=profile,
                visibility_scope=scope,
                match_limit=1,
            )
            if not result.get("candidates"):
                return ""
            candidate = result["candidates"][0]
            if self.db.execute(
                "SELECT 1 FROM injections WHERE identity=? AND session=? "
                "AND procedure=? AND version=?",
                (
                    self.identity,
                    run["session"],
                    candidate["procedure_token"],
                    candidate["procedure_version"],
                ),
            ).fetchone():
                return ""
            reason, changed = check_files(candidate["definition"], cwd)
            # Hooks deliver only the candidate checked here, not an unchecked plan.
            result = {**result, "plan": None, "related_procedures": []}
            if changed:
                result["file_warning"] = "files changed since this skill was learned: " + ", ".join(
                    changed
                )
            if reason:
                self.record_skip(run, candidate, reason)
                return ""
            context = render_pointer(result, budget)
            if not context:
                self.record_skip(run, candidate, "pointer_budget")
                return ""
            self.db.execute(
                "INSERT INTO injections VALUES (?,?,?,?,?,?)",
                (
                    self.identity,
                    run["session"],
                    candidate["procedure_token"],
                    candidate["procedure_version"],
                    digest(context),
                    token_count(context),
                ),
            )
            return context

    def record_skip(self, run, candidate, reason):
        self.db.execute(
            "INSERT OR REPLACE INTO skips VALUES (?,?,?,?,?,?)",
            (
                self.identity,
                run["token"],
                candidate["procedure_token"],
                candidate["procedure_version"],
                reason,
                time.time(),
            ),
        )

    def reconcile(self, client, token, messages, succeeded, usage_details=None):
        """CLI receipts establish tool outcomes; task success remains externally verified."""
        from .receipts import audit_codex_tools, codex_tool_counts, codex_usage

        details = usage_details or {}
        if set(details) - {
            "model",
            "elapsed_ms",
            "memory_context_tokens",
            "phase",
            "comparison_group",
            "comparison_arm",
        }:
            raise ValueError("Unsupported receipt measurement details")
        fingerprint = digest([redact(messages), succeeded, details])
        with self.transaction():
            run = self.db.execute(
                "SELECT * FROM runs WHERE token=? AND identity=?", (token, self.identity)
            ).fetchone()
            if (
                not run
                or not run["ended"]
                or set(json.loads(run["gaps"])) - {"unverified_exit_status"}
            ):
                raise ValueError(
                    "Receipt reconciliation requires an ended capture without missing actions"
                )
            prior = self.db.execute(
                "SELECT * FROM reconciliations WHERE identity=? AND run=?", (self.identity, token)
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise ValueError("Reconciliation evidence is immutable")
                derived, events = prior["derived"], json.loads(prior["events"])
            else:
                if run["closed"]:
                    last = json.loads(
                        self.db.execute(
                            "SELECT payload FROM events WHERE run=? ORDER BY sequence DESC LIMIT 1",
                            (token,),
                        ).fetchone()[0]
                    )
                    if last.get("capture_complete", True) and last.get("outcome_verified", True):
                        raise ValueError("An attested outcome cannot be rewritten")
                if succeeded and (
                    not any(m.get("type") == "turn.completed" for m in messages)
                    or any(m.get("type") in {"turn.failed", "error"} for m in messages)
                ):
                    raise ValueError("Successful reconciliation requires a completed CLI turn")
                response = client.http.get(f"/v1/runs/{token}")
                response.raise_for_status()
                audited, _ = audit_codex_tools(response.json(), messages)
                derived = str(uuid4())
                events = [e for e in audited["events"] if e["kind"] != "completion"]
                events.append(
                    ExecutionEvent(
                        event_id="receipt-completion",
                        sequence=len(events),
                        kind="completion",
                        status="success" if succeeded else "failure",
                        response={
                            "source_run": token,
                            "receipt_sha256": digest(redact(messages)),
                            "verification": "operator supplied actual task outcome",
                            "session_usage": {
                                **details,
                                "source": "codex-cli",
                                **codex_usage(messages),
                                "tool_calls": codex_tool_counts(messages)["tool_calls"],
                                "captured_tool_calls": sum(
                                    e["kind"] == "tool" for e in audited["events"]
                                ),
                                "capture_complete": True,
                            },
                        },
                    ).model_dump(mode="json")
                )
                self.db.execute(
                    "INSERT INTO reconciliations VALUES (?,?,?,?,?)",
                    (self.identity, token, derived, fingerprint, json.dumps(events)),
                )
        for start in range(0, len(events), 128):
            result = client.musclememory_observe(
                run_token=derived,
                runtime_profile=json.loads(run["profile"]),
                visibility_scope=run["scope"],
                event_batch=events[start : start + 128],
            )
            if result["rejected_event_count"]:
                raise ValueError("Audited receipt delivery rejected")
        client.record_session_usage(derived, **events[-1]["response"]["session_usage"])
        with self.transaction():
            if not self.db.execute("SELECT closed FROM runs WHERE token=?", (token,)).fetchone()[0]:
                self.append(
                    token,
                    "reconciled-closure",
                    {
                        "kind": "completion",
                        "status": "failure",
                        "capture_complete": False,
                        "response": {"reconciled_run": derived},
                    },
                )
                self.db.execute("UPDATE runs SET closed=1 WHERE token=?", (token,))
        while self.deliver(client, token):
            pass
        return derived

    def learning_evidence(self, client, token):
        """Return only complete, durable native or receipt-audited observations."""
        derived = self.db.execute(
            "SELECT events FROM reconciliations WHERE derived=? AND identity=?",
            (token, self.identity),
        ).fetchone()
        if derived:
            events = json.loads(derived["events"])
            response = client.http.get(f"/v1/runs/{token}")
            response.raise_for_status()
            if response.json()["status"] != "success":
                raise ValueError("Learning requires a delivered successful reconciliation")
            client.record_session_usage(token, **events[-1]["response"]["session_usage"])
            return events
        run = self.db.execute(
            "SELECT closed,gaps FROM runs WHERE token=? AND identity=?", (token, self.identity)
        ).fetchone()
        if not run or not run["closed"] or json.loads(run["gaps"]):
            raise ValueError("Learning requires an attested complete capture")
        while self.deliver(client, token):
            pass
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT payload FROM events WHERE run=? ORDER BY sequence", (token,)
            )
        ]

    def recover_native(self, client, token, transcript_path):
        """Recover a closed turn from actual native results; preserve its original trace."""
        from .recovery import RecoverySkipped, read_native_turns, recover_native_run

        row = self.db.execute(
            "SELECT * FROM runs WHERE token=? AND identity=?", (token, self.identity)
        ).fetchone()
        if not row or not row["closed"]:
            return None
        if self.db.execute(
            "SELECT 1 FROM native_recoveries WHERE identity=? AND run=?",
            (self.identity, token),
        ).fetchone():
            return None
        native = read_native_turns(transcript_path, {row["turn"]}).get(row["turn"])
        if not native:
            return None
        captured = [
            (r[0], json.loads(r[1]))
            for r in self.db.execute(
                "SELECT source,payload FROM events WHERE run=? ORDER BY sequence", (token,)
            )
        ]
        last = captured[-1][1] if captured else {}
        if (
            last.get("kind") == "completion"
            and last.get("status") == "success"
            and last.get("capture_complete", True)
            and last.get("outcome_verified", True)
        ):
            return None
        try:
            recovery = recover_native_run(row, captured, native, CODEX_CONTEXT_TOOLS)
        except RecoverySkipped:
            return None
        events = recovery.pop("events")
        recovery.pop("source_run")
        for start in range(0, len(events), 128):
            result = client.musclememory_observe(
                **recovery, event_batch=events[start : start + 128]
            )
            if result["rejected_event_count"]:
                raise ValueError("Native recovery delivery rejected")
        result = client.auto_learn(recovery["run_token"])
        if result.get("action") != "deferred":
            self.db.execute(
                "INSERT OR IGNORE INTO native_recoveries VALUES (?,?,?)",
                (self.identity, token, json.dumps(result)),
            )
        return result


def guidance(client, intent, profile, scope):
    result = client.musclememory_recall(
        intent_text=intent[:8000], runtime_profile=profile, visibility_scope=scope, match_limit=1
    )
    return render_pointer(result)


def run_hook(config_path, client_name, payload):
    config, tools, profile, client = load_config(config_path, client_name)
    if isinstance(payload.get("cwd"), str):
        project, _ = workspace.project_root(payload["cwd"])
        if project:
            profile = profile.model_copy(
                update={
                    "environment": {
                        **profile.environment,
                        "mm_project": hashlib.sha256(project.encode()).hexdigest(),
                    }
                }
            )
    queue = HookQueue(config["state_dir"], client.capture_identity)
    output = {}
    if client_name == "cursor":
        if payload.get("hook_event_name") == "beforeSubmitPrompt":
            output = {"continue": True}
        elif payload.get("hook_event_name") == "preToolUse":
            # This observer casts no denial vote. Client approvals and other
            # policy hooks still decide whether the tool may execute.
            output = {"permission": "allow"}
    try:
        token, stage = queue.handle(
            client_name, redact(payload), tools, profile, config.get("namespace", "default")
        )
        if token and stage != "started":
            try:
                queue.deliver(client, token)
            except Exception as error:
                print(
                    f"MuscleMemory capture queued ({type(error).__name__}); run hooks flush.",
                    file=sys.stderr,
                )
        if stage == "prompt":
            for ended in queue.pending_endings(token):
                try:
                    queue.deliver(client, ended)
                except Exception:
                    break  # The exact closure remains queued for the next prompt or flush.
        if (
            stage == "prompt"
            and client_name == "codex"
            and payload.get("transcript_path")
            and config.get("auto_learn", True)
        ):
            # task_complete can arrive after Stop; retry closed turns at the next prompt.
            for previous in queue.db.execute(
                "SELECT r.token FROM runs r JOIN runs current ON r.session=current.session "
                "AND r.identity=current.identity WHERE current.token=? AND r.token!=? "
                "AND r.closed=1 AND r.ended=1 ORDER BY r.created DESC LIMIT 4",
                (token, token),
            ).fetchall():
                try:
                    queue.recover_native(client, previous[0], payload["transcript_path"])
                except Exception:
                    break  # Offline recovery remains available through migration.
        if stage == "verified" and config.get("auto_learn", True):
            # Tests passed after the last change: count it as a check for a skill it
            # used, or learn a new skill that waits for a person's OK.
            try:
                while queue.deliver(client, token):
                    pass
                client.auto_learn(token, hint=queue.injected(token))
            except Exception as error:
                print(f"MuscleMemory could not learn ({type(error).__name__}).", file=sys.stderr)
        if stage == "failed" and (skill := queue.injected(token)):
            # The skill handed to this session ended with its own test failing: one
            # failed check flags it until a new version is learned and checked.
            try:
                while queue.deliver(client, token):
                    pass
                client.auto_flag(token, hint=skill)
            except Exception as error:
                print(f"MuscleMemory could not record ({type(error).__name__}).", file=sys.stderr)
        if (
            client_name == "codex"
            and token
            and stage == "ended"
            and payload.get("hook_event_name") == "Stop"
            and payload.get("transcript_path")
            and config.get("auto_learn", True)
        ):
            try:
                queue.recover_native(client, token, payload["transcript_path"])
            except Exception as error:
                print(
                    f"MuscleMemory native recovery queued ({type(error).__name__}).",
                    file=sys.stderr,
                )
        recall_enabled = config.get("recall_enabled", True)
        if stage == "prompt" and client_name != "cursor" and recall_enabled:
            try:
                context = queue.recall_once(
                    client,
                    token,
                    payload["prompt"],
                    profile,
                    config.get("namespace", "default"),
                    config.get("guidance_token_budget", 350),
                    payload.get("cwd"),
                )
                if context:
                    output = {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": context,
                        }
                    }
            except Exception:
                pass
        # Cursor prompt hooks have no documented context output. Inject through
        # the first tool result's supported additional_context field instead.
        if client_name == "cursor" and stage == "captured" and recall_enabled:
            count = queue.db.execute(
                "SELECT count(*) FROM events WHERE run=?", (token,)
            ).fetchone()[0]
            if count == 2:
                try:
                    intent = json.loads(
                        queue.db.execute(
                            "SELECT payload FROM events WHERE run=? AND sequence=0", (token,)
                        ).fetchone()[0]
                    )["intent_text"]
                    context = queue.recall_once(
                        client,
                        token,
                        intent,
                        profile,
                        config.get("namespace", "default"),
                        config.get("guidance_token_budget", 350),
                        payload.get("cwd"),
                    )
                    if context:
                        output = {"additional_context": context}
                except Exception:
                    pass
        return output
    finally:
        queue.close()
        client.close()


def install_hooks(project, clients, config_path):
    """Merge advisory hooks, preserving existing client configuration and backups."""
    project = Path(project).resolve()
    installed = []
    for client in clients:
        directory = project / {"claude": ".claude", "codex": ".codex", "cursor": ".cursor"}[client]
        directory.mkdir(exist_ok=True)
        if directory.is_symlink():
            raise ValueError("Install hooks in a real client configuration directory")
        path = directory / ("settings.json" if client == "claude" else "hooks.json")
        if path.is_symlink():
            raise ValueError("Client hook settings must not be a symlink")
        if client == "codex":
            layer = directory / "config.toml"
            if not layer.exists():
                layer.write_text("# Project configuration; hooks are in hooks.json.\n")
        existing = json.loads(path.read_text()) if path.exists() else {}
        hooks = existing.setdefault("hooks", {})
        events = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
        if client == "claude":
            events.append("PostToolUseFailure")
        if client == "cursor":
            existing.setdefault("version", 1)
            events = [
                "beforeSubmitPrompt",
                "preToolUse",
                "postToolUse",
                "postToolUseFailure",
                "stop",
            ]
        command = shlex.join(
            [
                sys.executable,
                "-m",
                "musclememory",
                "hooks",
                "receive",
                "--client",
                client,
                "--config",
                str(Path(config_path).resolve()),
            ]
        )
        for event in events:
            if client == "cursor":
                item = {"command": command, "timeout": 10}
            else:
                item = {"hooks": [{"type": "command", "command": command, "timeout": 10}]}
            entries = hooks.setdefault(event, [])
            if item not in entries:
                entries.append(item)
        if path.exists() and json.loads(path.read_text()) != existing:
            backup = path.with_name(path.name + ".musclememory-backup-" + str(time.time_ns()))
            backup.write_bytes(path.read_bytes())
            backup.chmod(0o600)
        path.write_text(json.dumps(existing, indent=2) + "\n")
        installed.append(str(path))
    return installed


def hooks_command(arguments):
    if arguments.action == "install":
        clients = list(DEFAULT_TOOLS) if arguments.client == "all" else [arguments.client]
        print(
            json.dumps({"installed": install_hooks(arguments.project, clients, arguments.config)})
        )
        return
    if arguments.action == "receive":
        try:
            raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("Hook input exceeds 2 MiB")
            print(json.dumps(run_hook(arguments.config, arguments.client, json.loads(raw))))
        except Exception as error:
            # Do not echo client payloads, credentials, paths or exception text.
            print(f"MuscleMemory hook failed ({type(error).__name__}).", file=sys.stderr)
            # Non-policy hook errors fail open with a non-blocking exit code. An
            # invalid success response could block a Cursor pre hook instead.
            print("{}")
            raise SystemExit(1) from None
        return
    config, _, _, client = load_config(arguments.config, arguments.client)
    queue = HookQueue(config["state_dir"], client.capture_identity)
    try:
        if arguments.action == "list":
            print(json.dumps({"runs": queue.listing()}))
        elif arguments.action == "flush":
            delivered = 0
            for token in queue.pending():
                while count := queue.deliver(client, token):
                    delivered += count
            print(json.dumps({"delivered_events": delivered}))
        elif arguments.action == "repair":
            repaired = queue.repair_endings()
            delivered = 0
            retry_after = None
            for token in queue.pending():
                try:
                    while count := queue.deliver(client, token):
                        delivered += count
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 429:
                        raise
                    retry_after = error.response.headers.get("Retry-After", "60")
                    break  # Closures are durable; retry without rewriting any events.
            print(
                json.dumps(
                    {
                        "closed_runs": len(repaired),
                        "delivered_events": delivered,
                        "pending_runs": len(queue.pending()),
                        "retry_after_seconds": retry_after,
                    }
                )
            )
        elif arguments.action == "attest":
            token = queue.attest(arguments.run, arguments.succeeded)
            for pending in dict.fromkeys([arguments.run, token]):
                while queue.deliver(client, pending):
                    pass
            result = {"run_token": token, "source_run": arguments.run, "attested": True}
            print(json.dumps(result))
        elif arguments.action == "reconcile":
            messages = [
                json.loads(line)
                for line in Path(arguments.receipts).read_text().splitlines()
                if line.startswith("{")
            ]
            print(
                json.dumps(
                    {
                        "run_token": queue.reconcile(
                            client, arguments.run, messages, arguments.succeeded
                        )
                    }
                )
            )
        elif arguments.action == "learn":
            evidence = queue.learning_evidence(client, arguments.run)
            ids = [
                event["event_id"]
                for event in evidence
                if event["kind"] == "tool"
                and event["status"] == "success"
                and event.get("outcome_verified", True)
            ]
            if not ids or evidence[-1]["status"] != "success":
                raise ValueError("Learning requires successful tool evidence and completion")
            verifiers = [
                event["event_id"]
                for event in evidence
                if event["event_id"] in ids
                and "verify" in operation_categories(event["arguments"], event["capability"])
            ]
            policy = json.loads(Path(arguments.policy).read_text()) if arguments.policy else {}
            print(
                json.dumps(
                    client.musclememory_learn(
                        run_token=arguments.run,
                        completion_proof={
                            "succeeded": True,
                            "evidence_event_ids": verifiers[-1:] or ids[-1:],
                            "verifier": "human_attestation",
                            "summary": arguments.summary,
                        },
                        learning_policy=policy,
                    )
                )
            )
        elif arguments.action == "purge":
            cutoff = time.time() - arguments.older_days * 86400
            with queue.transaction():
                tokens = [
                    row[0]
                    for row in queue.db.execute(
                        "SELECT token FROM runs r WHERE identity=? AND created<? "
                        "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.run=r.token AND ack=0)",
                        (client.capture_identity, cutoff),
                    )
                ]
                for token in tokens:
                    for table in ("active", "pending_calls", "claims", "snapshots", "overlaps"):
                        queue.db.execute(f"DELETE FROM {table} WHERE run=?", (token,))
                    queue.db.execute("DELETE FROM events WHERE run=?", (token,))
                    queue.db.execute(
                        "DELETE FROM skips WHERE identity=? AND run=?", (queue.identity, token)
                    )
                    queue.db.execute("DELETE FROM runs WHERE token=?", (token,))
            print(json.dumps({"purged_runs": len(tokens)}))
    finally:
        queue.close()
        client.close()
