"""Launch MuscleMemory's stdio MCP server using a credential from macOS Keychain."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEYCHAIN_SERVICE = "com.thewariend.musclememory.codex"
KEYCHAIN_ACCOUNT = "local-mcp"


def main() -> int:
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            KEYCHAIN_ACCOUNT,
            "-w",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    key = result.stdout.strip()
    if result.returncode or not key:
        print("MuscleMemory credential is unavailable in macOS Keychain.", file=sys.stderr)
        return 1

    os.chdir(ROOT)
    environment = os.environ.copy()
    environment["MM_API_KEY"] = key
    # Use the same store as the local install (~/.musclememory unless MM_HOME is set).
    from musclememory.local import default_database_url

    environment.setdefault("MM_DATABASE_URL", default_database_url())
    os.execve(
        sys.executable,
        [
            sys.executable,
            "-m",
            "musclememory.cli",
            "stdio",
            "--url",
            environment.get("MM_API_URL", "http://127.0.0.1:8080"),
        ],
        environment,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
