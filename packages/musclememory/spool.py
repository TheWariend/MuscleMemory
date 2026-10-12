"""Private local capture journal. No credentials or unsanitized payloads belong here."""

import json
import os
import re
import sqlite3
import stat
from pathlib import Path

from .core import ExecutionEvent, ObserveInput


class CaptureSpool:
    def __init__(self, path, identity, metadata=None):
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise ValueError("Durable capture requires a client capture_identity")
        self.path = Path(path)
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        if metadata is not None:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("Capture spool must be a private regular file (mode 0600)")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise ValueError("Capture spool must belong to the current user")
            if info.st_size > 80 * 1024 * 1024:
                raise ValueError("Capture spool exceeds the storage limit")
        finally:
            os.close(descriptor)
        self.connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        try:
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA journal_mode=DELETE")
            if metadata is not None:
                with self.connection:
                    self.connection.execute(
                        "CREATE TABLE capture (id INTEGER PRIMARY KEY CHECK(id=1), "
                        "version INTEGER NOT NULL, identity TEXT NOT NULL, metadata TEXT NOT NULL, "
                        "closed INTEGER NOT NULL DEFAULT 0)"
                    )
                    self.connection.execute(
                        "CREATE TABLE events (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL, "
                        "acknowledged INTEGER NOT NULL DEFAULT 0)"
                    )
                    self.connection.execute(
                        "INSERT INTO capture (id,version,identity,metadata) VALUES (1,1,?,?)",
                        (identity, json.dumps(metadata, allow_nan=False)),
                    )
            row = self.connection.execute(
                "SELECT version,identity,metadata,closed FROM capture WHERE id=1"
            ).fetchone()
            if not row or row[0] != 1 or row[1] != identity:
                raise ValueError("Capture spool version or client identity does not match")
            self.metadata, self.closed = json.loads(row[2]), bool(row[3])
            # Validate disk data as contracts, never as executable instructions.
            ObserveInput(**self.metadata, event_batch=[{"kind": "intent"}])
            bounds = self.connection.execute(
                "SELECT count(*),max(length(payload)) FROM events"
            ).fetchone()
            if bounds[0] > 1024 or (bounds[1] or 0) > 65536 or len(row[2]) > 65536:
                raise ValueError("Capture spool exceeds payload limits")
            rows = self.connection.execute(
                "SELECT sequence,payload,acknowledged FROM events ORDER BY sequence"
            ).fetchall()
            if len(rows) > 1024:
                raise ValueError("Capture spool exceeds the trace limit")
            self.events, self.pending = [], []
            for sequence, payload, acknowledged in rows:
                event = ExecutionEvent.model_validate_json(payload).model_dump(mode="json")
                if sequence != len(self.events) or event["sequence"] != sequence:
                    raise ValueError("Capture spool has invalid event ordering")
                if len(json.dumps(event)) > 65536:
                    raise ValueError("Capture spool event exceeds 64 KiB")
                self.events.append(event)
                if not acknowledged:
                    self.pending.append(event)
            if bool(self.events and self.events[-1]["kind"] == "completion") != self.closed:
                raise ValueError("Capture spool completion state is inconsistent")
            if metadata is None and (not self.events or self.events[0]["kind"] != "intent"):
                raise ValueError("Capture spool has no initial intent")
        except BaseException:
            self.connection.close()
            raise

    def append(self, event):
        payload = json.dumps(event, allow_nan=False)
        with self.connection:
            # A stale second writer must fail before changing the first writer's trace.
            row = self.connection.execute(
                "SELECT closed,(SELECT count(*) FROM events) FROM capture WHERE id=1"
            ).fetchone()
            if row[0] or row[1] != event["sequence"]:
                raise RuntimeError("Capture spool changed; use one active writer per trace")
            self.connection.execute(
                "INSERT INTO events (sequence,payload) VALUES (?,?)", (event["sequence"], payload)
            )
            if event["kind"] == "completion":
                self.connection.execute("UPDATE capture SET closed=1 WHERE id=1")

    def acknowledge(self, events):
        with self.connection:
            self.connection.executemany(
                "UPDATE events SET acknowledged=1 WHERE sequence=?",
                [(event["sequence"],) for event in events],
            )

    def close(self):
        self.connection.close()
