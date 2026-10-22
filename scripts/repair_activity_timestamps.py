"""Restore local Activity start times from matching native hook queue records.

Preview by default. --apply backs up SQLite before correcting run metadata;
execution events, outcomes, skills, and retention deadlines are preserved.
"""

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from musclememory.client_hooks import load_config, private_file
from musclememory.config import Settings
from musclememory.local import config, home
from musclememory.service import MemoryService
from musclememory.storage import Run, utcnow
from sqlalchemy.engine import make_url


def backup_database(path):
    directory = home() / "backups"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("Backup directory must be private (0700)")
    target = directory / ("activity-time-" + uuid4().hex + ".sqlite")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(target) as destination:
            source.backup(destination)
    return str(target)


def repair(apply=False):
    local = config()
    url = make_url(local["database_url"])
    if url.get_backend_name() != "sqlite" or not url.database:
        raise ValueError("This recovery command requires the local SQLite installation")
    database = Path(url.database).resolve(strict=True)
    service = MemoryService(Settings(database_url=local["database_url"]))
    repaired, skipped = 0, 0
    backup = backup_database(database) if apply else None
    try:
        for agent in ("claude", "codex", "cursor"):
            path = home() / "hooks" / (agent + ".json")
            if not path.is_file():
                continue
            hook, _, _, client = load_config(path, agent)
            try:
                # A remote hook configuration cannot identify this local database.
                if str(client.http.base_url).rstrip("/") != local["url"].rstrip("/"):
                    continue
                principal = service.authenticate(
                    client.http.headers["authorization"][7:], rate_limit=False,
                )
                queue_path = Path(hook["state_dir"]) / "hooks.sqlite"
                private_file(queue_path)
                with sqlite3.connect(queue_path.as_uri() + "?mode=ro", uri=True) as queue:
                    queue.row_factory = sqlite3.Row
                    rows = queue.execute(
                        "SELECT r.*,e.payload FROM runs r JOIN events e ON e.run=r.token "
                        "AND e.sequence=0 WHERE r.identity=?", (client.capture_identity,),
                    ).fetchall()
                with service.db.transaction() as session:
                    for row in rows:
                        run = session.get(Run, (principal.tenant, row["token"]))
                        if not run:
                            continue
                        principal.require(run.namespace, "write")
                        intent = json.loads(row["payload"])
                        if (
                            run.namespace != row["scope"]
                            or run.runtime != json.loads(row["profile"])
                            or intent.get("kind") != "intent"
                            or run.intent != intent.get("intent_text")
                        ):
                            skipped += 1
                            continue
                        started = datetime.fromtimestamp(row["created"], timezone.utc)
                        started = started.replace(tzinfo=None)
                        if started > utcnow():
                            skipped += 1
                            continue
                        # Normal delivery differs by milliseconds; correct backlogs
                        # only, and never move an already earlier start forward.
                        if (run.created_at - started).total_seconds() <= 1:
                            continue
                        repaired += 1
                        if apply:
                            run.created_at = started
                            service.audit(
                                session, principal, run.namespace,
                                "repair_capture_time", run.run_token,
                            )
                    if apply:
                        session.commit()
            finally:
                client.close()
        return {"applied": apply, "corrected_runs": repaired,
                "skipped_mismatches": skipped, "backup": backup}
    finally:
        service.db.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Back up and apply corrections")
    print(json.dumps(repair(parser.parse_args().apply)))
