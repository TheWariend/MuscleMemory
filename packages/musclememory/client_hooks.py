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
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

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
    },
    "cursor": {
        "Read": ("filesystem.read", "read"),
        "Write": ("filesystem.write", "write"),
        "Grep": ("filesystem.search", "read"),
        "Delete": ("filesystem.delete", "destructive"),
        "Shell": ("shell.execute", "destructive"),
    },
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
            CREATE TABLE IF NOT EXISTS claims (
                run TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL, path TEXT,
                PRIMARY KEY(run,source));
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

    @staticmethod
    def _claim(name, arguments, tools):
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
            return "file", path
        return "any", None

    def gap(self, run, reason):
        gaps = json.loads(
            self.db.execute("SELECT gaps FROM runs WHERE token=?", (run,)).fetchone()[0]
        )
        if reason not in gaps:
            gaps.append(reason)
        self.db.execute("UPDATE runs SET gaps=? WHERE token=?", (json.dumps(gaps[:100]), run))

    def append(self, run, source, fields):
        fingerprint = digest(fields)
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

    def handle(self, client, payload, tools, profile, scope):
        event_name = payload.get("hook_event_name", "")
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
        view = workspace.snapshot(payload.get("cwd")) if shell and tool_event else None
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
                # Claude has no turn id. Every prompt starts a fresh recording, including
                # repeated identical prompts; the previous trace remains unverified.
                if not turn or not row:
                    token = str(uuid4())
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
                            time.time(),
                        ),
                    )
                    self.append(token, "intent", {"kind": "intent", "intent_text": prompt[:8000]})
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
                if self.db.execute("SELECT 1 FROM active WHERE run=?", (token,)).fetchone():
                    self.gap(token, "unfinished_tool")
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
                if client == "codex":
                    self._finish_incomplete(
                        token, "Codex turn ended without externally verified outcome"
                    )
                return token, "ended"
            source = payload.get("tool_use_id")
            name = payload.get("tool_name")
            if not source or not isinstance(source, str):
                self.gap(token, "missing_tool_call_id")
                return token, "capture_gap"
            if name and "musclememory_" in name:
                return token, "memory_tool_skipped"
            if event_name in {"PreToolUse", "preToolUse"}:
                if self.db.execute(
                    "SELECT 1 FROM events WHERE run=? AND source=?", (token, source)
                ).fetchone():
                    return token, "duplicate_start"
                # Overlapping reads, and edits to different files, can't affect each other.
                # A shell command or test run alongside anything leaves the order unknown.
                kind, path = self._claim(name, payload.get("tool_input"), tools)
                others = self.db.execute(
                    "SELECT 1 FROM active WHERE run=? AND source!=?", (token, source)
                ).fetchone()
                conflict = self.db.execute(
                    "SELECT 1 FROM claims WHERE run=? AND source!=? AND (kind='any' OR path=?)",
                    (token, source, path),
                ).fetchone()
                if others and (kind == "any" or conflict):
                    self.gap(token, "parallel_tool_dependencies_unknown")
                self.db.execute(
                    "INSERT OR IGNORE INTO active VALUES (?,?,?)", (token, source, time.time())
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
                "SELECT payload FROM events WHERE run=? AND source=?", (token, source)
            ).fetchone()
            measured_duration = (
                json.loads(prior[0])["duration_ms"]
                if prior
                else max(0, (time.time() - start[0]) * 1000)
                if start
                else 0
            )
            self.db.execute("DELETE FROM active WHERE run=? AND source=?", (token, source))
            claimed = self.db.execute(
                "SELECT kind FROM claims WHERE run=? AND source=?", (token, source)
            ).fetchone()
            self.db.execute("DELETE FROM claims WHERE run=? AND source=?", (token, source))
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
            if changed:
                arguments[WORKSPACE_CHANGES] = changed[:64]
            response = payload.get("tool_response", payload.get("tool_output"))
            if isinstance(response, str):
                try:
                    response = json.loads(response)
                except ValueError:
                    pass
            failed = event_name in {"PostToolUseFailure", "postToolUseFailure"}
            outcome_verified = True
            if isinstance(response, dict):
                failed |= response.get("isError") is True or response.get("success") is False
                failed |= response.get("exitCode", response.get("exit_code", 0)) not in (None, 0)
            if client == "codex" and name == "Bash":
                code = (
                    re.search(r"(?:Process exited with code|Exit code:)\s*(-?\d+)", response)
                    if isinstance(response, str)
                    else None
                )
                failed |= bool(code and int(code[1]) != 0)
                explicit = isinstance(response, dict) and isinstance(
                    response.get("exitCode", response.get("exit_code")), int
                )
                outcome_verified = bool(code or explicit or failed)
                if not outcome_verified and "verify" in operation_categories(
                    arguments, tools[name][0]
                ):
                    # Codex hooks often send output without the exit code; a test
                    # runner's own summary line ("OK", "FAILED (...)") settles it.
                    verdict = workspace.test_output_verdict(
                        response if isinstance(response, str) else json.dumps(response)
                    )
                    if verdict is not None:
                        outcome_verified, failed = True, failed or not verdict
                if not outcome_verified:
                    self.gap(token, "unverified_exit_status")
            command = arguments.get("command")
            if (
                shell
                and not failed
                and "verify" in operation_categories(arguments, tools[name][0])
                and workspace.piped_test(command_parts(command), str(command))
            ):
                # `tests | tail` exits with tail's status; trust the runner's own summary.
                output = (
                    response
                    if isinstance(response, str)
                    else "\n".join(
                        str(response.get(key) or "") for key in ("stdout", "stderr", "output")
                    )
                    if isinstance(response, dict)
                    else ""
                )
                verdict = workspace.test_output_verdict(output)
                if verdict is False:
                    failed = True
                elif verdict is None:
                    outcome_verified = False
                    self.gap(token, "unverified_exit_status")
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
                    "duration_ms": max(
                        0,
                        payload.get("duration_ms", payload.get("duration", measured_duration)) or 0,
                    ),
                },
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
            for row in rows:
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

    def _finish_incomplete(self, token, reason):
        if self.db.execute("SELECT closed FROM runs WHERE token=?", (token,)).fetchone()[0]:
            return
        self.append(
            token,
            "incomplete-closure",
            {
                "kind": "completion",
                "status": "failure",
                "capture_complete": False,
                "response": {"reason": reason},
            },
        )
        self.db.execute("UPDATE runs SET closed=1 WHERE token=?", (token,))

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
            if run["closed"]:
                completion = json.loads(
                    self.db.execute(
                        "SELECT payload FROM events WHERE run=? ORDER BY sequence DESC LIMIT 1",
                        (token,),
                    ).fetchone()[0]
                )
                if completion["status"] != ("success" if succeeded else "failure"):
                    raise ValueError("Attestation cannot change an existing outcome")
                return
            self.append(
                token,
                "completion",
                {"kind": "completion", "status": "success" if succeeded else "failure"},
            )
            self.db.execute("UPDATE runs SET closed=1 WHERE token=?", (token,))

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
                    if last.get("capture_complete", True):
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


def guidance(client, intent, profile, scope):
    result = client.musclememory_recall(
        intent_text=intent[:8000], runtime_profile=profile, visibility_scope=scope, match_limit=1
    )
    return render_pointer(result)


def run_hook(config_path, client_name, payload):
    config, tools, profile, client = load_config(config_path, client_name)
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
        elif arguments.action == "attest":
            queue.attest(arguments.run, arguments.succeeded)
            while queue.deliver(client, arguments.run):
                pass
            result = {"run_token": arguments.run, "attested": True}
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
                    queue.db.execute("DELETE FROM active WHERE run=?", (token,))
                    queue.db.execute("DELETE FROM events WHERE run=?", (token,))
                    queue.db.execute(
                        "DELETE FROM skips WHERE identity=? AND run=?", (queue.identity, token)
                    )
                    queue.db.execute("DELETE FROM runs WHERE token=?", (token,))
            print(json.dumps({"purged_runs": len(tokens)}))
    finally:
        queue.close()
        client.close()
