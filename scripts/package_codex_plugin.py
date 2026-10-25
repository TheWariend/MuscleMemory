"""Package the existing file-authenticated MCP connection with local branding.

Creates a private local marketplace. Does not install it, change connections,
copy inline credentials, add hooks, or change learning preferences.
"""

import argparse
import json
import os
import shlex
import shutil
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package(config_path, output):
    settings = tomllib.loads(config_path.read_text())
    server = settings.get("mcp_servers", {}).get("musclememory", {})
    if not server.get("command") or "--config" not in server.get("args", []):
        raise ValueError("Expected the existing MuscleMemory stdio connection with --config")
    if server.get("env") or server.get("http_headers"):
        raise ValueError("Inline credentials are not copied; use the file-authenticated connection")
    if server.get("env_vars") or server.get("cwd"):
        raise ValueError("Custom environment or working-directory settings need explicit support")
    if output.exists():
        raise FileExistsError("Output already exists; choose a new package directory")
    source = ROOT / "integrations/codex/plugin"
    manifest = json.loads((source / ".codex-plugin/plugin.json").read_text())
    for field in ("composerIcon", "logo", "logoDark"):
        path = manifest["interface"][field]
        if not (source / path).is_file():
            raise ValueError(f"Missing branding asset: {path}")
    output.mkdir(parents=True, mode=0o700)
    plugin = output / "plugins/musclememory"
    shutil.copytree(source, plugin)
    launcher = plugin / "scripts/server"
    launcher.parent.mkdir(mode=0o700)
    launcher.write_text(
        "#!/bin/sh\nexec " + shlex.join([server["command"], *server["args"]]) + "\n"
    )
    connection = {
        "type": "stdio",
        **{
            key: server[key]
            for key in ("command", "args", "env_vars", "startup_timeout_sec", "tool_timeout_sec")
            if key in server
        },
    }
    files = {
        plugin / "mcp.json": {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {
                "musclememory": {
                    "type": "stdio",
                    "command": "./scripts/server",
                    "args": [],
                }
            },
        },
        plugin / ".mcp.json": {
            "mcpServers": {
                "musclememory": {key: value for key, value in connection.items() if key != "type"}
            },
        },
        output / ".agents/plugins/marketplace.json": {
            "name": "musclememory-local",
            "interface": {"displayName": "MuscleMemory local"},
            "plugins": [
                {
                    "name": "musclememory",
                    "source": {"source": "local", "path": "./plugins/musclememory"},
                    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                    "category": "Productivity",
                }
            ],
        },
    }
    for path, payload in files.items():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(payload, indent=2) + "\n")
    for parent, _directories, names in os.walk(output):
        Path(parent).chmod(0o700)
        for name in names:
            (Path(parent) / name).chmod(0o600)
    launcher.chmod(0o700)
    return plugin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plugin = package(args.config.expanduser(), args.output.expanduser())
    print(json.dumps({"marketplace": str(args.output.expanduser()), "plugin": str(plugin)}))


if __name__ == "__main__":
    main()
