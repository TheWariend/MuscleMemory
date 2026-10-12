"""Private Compose PostgreSQL backups and restore checks into a fresh disposable DB."""

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def docker(project, *command, **kwargs):
    return subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(ROOT / "compose.yaml"),
            "exec",
            "-T",
            "postgres",
            *command,
        ],
        check=True,
        **kwargs,
    )


def private(path):
    info = path.lstat()
    if (
        path.is_symlink()
        or not path.is_file()
        or info.st_mode & 0o077
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
    ):
        raise ValueError("Backup must be an owned private regular file (0600)")


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def backup(project, directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("Backup directory must be private (0700)")
    destination = directory / (
        datetime.now(UTC).strftime("memory-%Y%m%dT%H%M%SZ-") + uuid4().hex + ".dump"
    )
    temporary = destination.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as file:
            docker(
                project,
                "pg_dump",
                "-U",
                "memory",
                "-d",
                "musclememory",
                "--format=custom",
                stdout=file,
            )
            file.flush()
            os.fsync(file.fileno())
        temporary.rename(destination)
        metadata = {
            "format": "postgres-custom",
            "created_at": datetime.now(UTC).isoformat(),
            "file": destination.name,
            "sha256": digest(destination),
            "bytes": destination.stat().st_size,
        }
        manifest = destination.with_suffix(".json")
        with os.fdopen(os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            json.dump(metadata, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        print(json.dumps({"backup": str(destination), "sha256": metadata["sha256"]}))
    finally:
        temporary.unlink(missing_ok=True)


def verify(project, source):
    private(source)
    manifest = source.with_suffix(".json")
    private(manifest)
    metadata = json.loads(manifest.read_text())
    if metadata["sha256"] != digest(source) or metadata["bytes"] != source.stat().st_size:
        raise ValueError("Backup checksum or size mismatch")
    scratch = "mm_restore_" + uuid4().hex
    created = False
    try:
        docker(project, "createdb", "-U", "memory", scratch, stdout=subprocess.DEVNULL)
        created = True
        with source.open("rb") as file:
            docker(
                project,
                "pg_restore",
                "-U",
                "memory",
                "--exit-on-error",
                "--no-owner",
                "-d",
                scratch,
                stdin=file,
            )
        tables = docker(
            project,
            "psql",
            "-U",
            "memory",
            "-d",
            scratch,
            "-At",
            "-c",
            "SELECT tablename FROM pg_tables WHERE schemaname='public'",
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        required = {
            "credentials",
            "runs",
            "events",
            "procedures",
            "revisions",
            "evaluations",
            "audit_log",
            "rate_windows",
            "alembic_version",
        }
        if not required.issubset(tables):
            raise ValueError("Restored database is missing required schema tables")
        counts = docker(
            project,
            "psql",
            "-U",
            "memory",
            "-d",
            scratch,
            "-At",
            "-c",
            "SELECT json_build_object('runs',(SELECT count(*) FROM runs),"
            "'events',(SELECT count(*) FROM events),'procedures',(SELECT count(*) FROM procedures),"
            "'evaluations',(SELECT count(*) FROM evaluations))",
            capture_output=True,
            text=True,
        ).stdout.strip()
        print(
            json.dumps(
                {
                    "restore_verified": True,
                    "tables_checked": len(required),
                    "source_modified": False,
                    "restored_rows": json.loads(counts),
                }
            )
        )
    finally:
        if created:
            docker(project, "dropdb", "-U", "memory", scratch, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "verify"])
    parser.add_argument("--project", required=True, help="The existing private Compose project")
    parser.add_argument(
        "--path", required=True, type=Path, help="Private backup directory or dump file"
    )
    args = parser.parse_args()
    (backup if args.action == "backup" else verify)(args.project, args.path.resolve())


if __name__ == "__main__":
    main()
