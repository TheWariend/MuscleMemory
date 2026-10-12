import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import PROFILE
from musclememory.compiler import materialize
from musclememory.core import ProcedureDefinition, RecallInput


@pytest.mark.typescript
def test_typescript_real_tool_capture_and_python_portability(
    remote_server, credential, service, principal, tmp_path
):
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if not node or not (root / "packages/sdk-typescript/dist/index.js").exists():
        pytest.skip("Build the TypeScript SDK and install Node to run cross-language capture")
    result = subprocess.run(
        [node, str(root / "examples/typescript_capture.mjs")],
        env={
            **os.environ,
            "MM_API_URL": remote_server,
            "MM_API_KEY": credential["api_key"],
            "MM_NAMESPACE": "team",
        },
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["independent_successes"] == 2
    assert report["resolved_tools"]["repository.inspect"] == "workspace.read"
    recalled = service.recall(
        principal,
        RecallInput(
            intent_text="Inspect repository manifest",
            runtime_profile=PROFILE,
            visibility_scope="team",
        ),
    )
    definition = ProcedureDefinition.model_validate(recalled["candidates"][0]["definition"])
    target = tmp_path / "alternate.toml"
    target.write_text('[project]\nname = "another-project"\n')
    adapted = materialize(definition, {"manifest_path": str(target)})
    assert 'name = "another-project"' in Path(adapted[0]["arguments"]["path"]).read_text()
    training = service.get_run(principal, definition.provenance["source_run"])
    assert training["runtime_profile"]["agent_name"] == "typescript-tool-agent"


@pytest.mark.typescript
def test_actual_cleanslate_sdk_runtime_stream(remote_server, credential, service, principal):
    root = Path(__file__).resolve().parents[1]
    default_sdk = (
        root.parent
        / "wariend-api-dev/worker/cleanslate-cloud-worker/node_modules"
        / "@cleanslate/sdk/dist/node/index.js"
    )
    sdk = Path(os.getenv("MM_TEST_CLEANSLATE_SDK", str(default_sdk)))
    node = shutil.which("node")
    if not node or not sdk.exists():
        pytest.skip("Set MM_TEST_CLEANSLATE_SDK to an installed CleanSlate SDK Node entrypoint")
    result = subprocess.run(
        [node, str(root / "examples/cleanslate_capture.mjs")],
        env={
            **os.environ,
            "MM_API_URL": remote_server,
            "MM_API_KEY": credential["api_key"],
            "MM_NAMESPACE": "team",
            "CLEANSLATE_SDK_MODULE": str(sdk),
        },
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["real_tool_reads"] == 1
    assert report["capture_gaps"] == []
    trace = service.get_run(principal, report["run_token"])
    assert trace["status"] == "success"
    assert trace["runtime_profile"]["agent_name"] == "cleanslate-native-runtime"
    assert trace["events"][1]["tool_name"] == "memory_read_manifest"
