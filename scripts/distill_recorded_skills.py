"""Re-curate preserved recordings and repair the memory graph. Preview by default.

A preview runs against a temporary SQLite copy. --apply backs up the live database
before schema migration, check revalidation, or changing derived skill versions.
No recorded tool command is replayed. Reports may contain private learned text.
"""

import argparse
import json
import os
import sqlite3
import tempfile
from collections import Counter
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from musclememory.backfill import (
    align_learning_contract,
    align_skill_names,
    index_native_scopes,
    rebuild_memory,
    revalidate_checks,
)
from musclememory.cli import migrate
from musclememory.config import Settings
from musclememory.local import config, home, private_write
from musclememory.security import Principal
from musclememory.service import MemoryService
from musclememory.storage import Revision
from sqlalchemy.engine import make_url


def copy_database(source, destination):
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as reader:
        with sqlite3.connect(destination) as writer:
            reader.backup(writer)
            if writer.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Database copy integrity check failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database", type=Path, help="Override the local SQLite file")
    parser.add_argument("--agent", choices=["codex", "claude"])
    parser.add_argument("--limit", type=int, help="Maximum successful recordings to process")
    parser.add_argument("--checks-only", action="store_true")
    parser.add_argument(
        "--align-contract", action="store_true", help="Repair metadata only; no model calls"
    )
    parser.add_argument(
        "--align-names",
        action="store_true",
        help="Remove curator-assigned display names only; no model calls",
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    local = config()
    url = make_url(local["database_url"])
    if url.get_backend_name() != "sqlite":
        parser.error("Local backfill currently requires SQLite")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    source = (args.database or Path(url.database)).resolve(strict=True)
    report = {"applied": args.apply, "backup": None, "checks": [], "results": []}
    with tempfile.TemporaryDirectory(prefix="mm-distill-preview-") as temporary:
        if args.apply:
            directory = home() / "backups"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or directory.stat().st_mode & 0o077:
                raise ValueError("Backup directory must be private")
            saved = directory / ("semantic-learning-" + uuid4().hex + ".sqlite")
            copy_database(source, saved)
            report["backup"] = str(saved)
            working = source
        else:
            working = Path(temporary) / "memory.sqlite"
            copy_database(source, working)
        database_url = "sqlite:///" + str(working)
        migrate(database_url)
        settings = replace(
            Settings.from_env(),
            database_url=database_url,
            learning_agent=args.agent or Settings.from_env().learning_agent,
        )
        service = MemoryService(settings)
        namespace = local["namespace"]
        principal = Principal(
            "local-backfill", local["tenant"], {namespace: ["read", "write", "delete", "admin"]}
        )

        def save(progress=None):
            if progress is not None:
                if progress.get("procedure_token"):
                    with service.db.session() as session:
                        revision = session.get(
                            Revision, (progress["procedure_token"], progress["procedure_version"])
                        )
                        if revision:
                            progress["learning"] = revision.definition["provenance"].get("learning")
                report["results"].append(progress)
                print(
                    json.dumps(
                        {
                            "run": progress["run_token"],
                            "action": progress["action"],
                            "reason": progress.get("reason"),
                        }
                    ),
                    flush=True,
                )
            report["actions"] = dict(Counter(r["action"] for r in report["results"]))
            private_write(args.report, json.dumps(report, indent=2) + "\n")

        try:
            if args.align_names:
                report["name_repairs"] = align_skill_names(
                    service, principal, namespace, apply=True
                )
            elif args.align_contract:
                report["contract_repairs"] = align_learning_contract(
                    service, principal, namespace, apply=True
                )
            else:
                report["indexed_scopes"] = index_native_scopes(service, principal, namespace)
                report["checks"] = revalidate_checks(service, principal, namespace, apply=True)
                save()
                if not args.checks_only:
                    rebuild_memory(service, principal, namespace, limit=args.limit, progress=save)
            save()
        finally:
            service.db.engine.dispose()
    print(
        json.dumps(
            {
                "applied": report["applied"],
                "backup": report["backup"],
                "actions": report["actions"],
                "invalid_checks": sum(not c["valid"] for c in report["checks"]),
            }
        )
    )


if __name__ == "__main__":
    main()
