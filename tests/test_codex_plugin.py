import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "package_codex_plugin", ROOT / "scripts/package_codex_plugin.py"
)
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


def test_branded_launcher_preserves_arguments_and_does_not_copy_other_credentials(tmp_path):
    runtime = tmp_path / "runtime with spaces"
    runtime.mkdir()
    executable = runtime / "server"
    executable.write_text(
        f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o700)
    sentinel = tmp_path / "must-not-exist"
    arguments = ["--config", f"literal 'quoted' $(touch {sentinel}) `touch {sentinel}`"]
    config = tmp_path / "config.toml"
    config.write_text(
        f"[mcp_servers.musclememory]\ncommand = {json.dumps(str(executable))}\n"
        f"args = {json.dumps(arguments)}\nstartup_timeout_sec = 20\n"
        '[mcp_servers.unrelated]\ntoken = "fixture-only-unrelated-secret"\n'
    )
    before = config.read_bytes()
    plugin = packager.package(config, tmp_path / "marketplace")
    portable = json.loads((plugin / "mcp.json").read_text())["mcpServers"]["musclememory"]
    assert set(portable) == {"type", "command", "args"}
    result = subprocess.run(
        [str(plugin / portable["command"]), *portable["args"]],
        cwd=plugin,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == arguments
    assert not sentinel.exists()
    assert config.read_bytes() == before
    for file in plugin.rglob("*"):
        if file.is_file() and file.suffix != ".png":
            assert "fixture-only-unrelated-secret" not in file.read_text()
    assert (plugin / "scripts/server").stat().st_mode & 0o777 == 0o700


def test_inline_credentials_are_rejected_before_creating_a_package(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[mcp_servers.musclememory]\ncommand = "python"\nargs = ["--config", "/config"]\n'
        'env = {MM_API_KEY = "fixture-only-secret"}\n'
    )
    output = tmp_path / "package"
    with pytest.raises(ValueError, match="Inline credentials"):
        packager.package(config, output)
    assert not output.exists()
