import json
import os
import sys

import httpx2
import pytest
from conftest import PROFILE
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from musclememory.core import LearnInput
from musclememory.delivery import token_count
from musclememory.mcp_server import build_mcp

NAMES = {
    "musclememory_recall",
    "musclememory_observe",
    "musclememory_learn",
    "musclememory_evaluate",
    "musclememory_inspect",
}
FIELDS = {
    "musclememory_recall": {"intent_text", "runtime_profile", "visibility_scope", "match_limit"},
    "musclememory_observe": {"run_token", "event_batch", "runtime_profile", "visibility_scope"},
    "musclememory_learn": {"run_token", "completion_proof", "learning_policy"},
    "musclememory_evaluate": {
        "procedure_token",
        "run_token",
        "outcome_report",
        "performance_metrics",
    },
    "musclememory_inspect": {"procedure_token", "revision_selector"},
}


async def protocol_workflow(client, approve):
    """Learn, check and recall over MCP. `approve` is the person's OK, which MCP cannot give."""
    discovered = await client.list_tools()
    assert {tool.name for tool in discovered.tools} == NAMES
    for tool in discovered.tools:
        assert set(tool.input_schema["properties"]) == FIELDS[tool.name]
        assert tool.meta["musclememory/contractVersion"] == "1"
    events = [
        {
            "event_id": "intent",
            "sequence": 0,
            "kind": "intent",
            "intent_text": "Inspect repository manifest",
        },
        {
            "event_id": "read",
            "sequence": 1,
            "kind": "tool",
            "tool_name": "read_file",
            "capability": "repository.inspect",
            "arguments": {"path": "manifest"},
            "duration_ms": 1,
        },
        {"event_id": "done", "sequence": 2, "kind": "completion"},
    ]
    common = {"runtime_profile": PROFILE.model_dump(), "visibility_scope": "team"}
    for index in range(3):
        run = f"mcp-{index}"
        result = await client.call_tool(
            "musclememory_observe", {**common, "run_token": run, "event_batch": events}
        )
        assert not result.is_error, result
        assert result.structured_content["accepted_event_count"] == 3
        if index == 0:
            result = await client.call_tool(
                "musclememory_learn",
                {
                    "run_token": run,
                    "completion_proof": {
                        "succeeded": True,
                        "evidence_event_ids": ["read"],
                        "summary": "Manifest checked",
                    },
                    "learning_policy": {},
                },
            )
            assert not result.is_error, result
            procedure = result.structured_content["procedure_token"]
        else:
            result = await client.call_tool(
                "musclememory_evaluate",
                {
                    "procedure_token": procedure,
                    "run_token": run,
                    "outcome_report": {
                        "succeeded": True,
                        "procedure_version": 1,
                        "evidence_event_ids": ["read"],
                    },
                    "performance_metrics": {"observed": {"tool_calls": 1, "duration_ms": 1}},
                },
            )
            assert not result.is_error, result
    recall = {**common, "intent_text": "Inspect repository manifest"}
    result = await client.call_tool("musclememory_recall", recall)
    assert not result.is_error and result.structured_content["no_match"]  # waits for an OK
    approve(procedure)
    result = await client.call_tool("musclememory_recall", recall)
    assert not result.is_error and not result.structured_content["no_match"]
    result = await client.call_tool("musclememory_inspect", {"procedure_token": procedure})
    assert not result.is_error and result.structured_content["successes"] == 2
    denied = await client.call_tool(
        "musclememory_recall", {**common, "visibility_scope": "private", "intent_text": "inspect"}
    )
    assert denied.is_error
    invalid = await client.call_tool(
        "musclememory_recall", {**common, "intent_text": "inspect", "match_limit": 100}
    )
    assert invalid.is_error


def approver(service, principal):
    return lambda procedure: service.approve(principal, procedure)


async def test_official_client_in_process(service, credential, principal):
    async with Client(build_mcp(service, stdio_key=credential["api_key"]), mode="legacy") as client:
        await protocol_workflow(client, approver(service, principal))


async def test_large_inspection_returns_bounded_summary(service, principal, credential):
    from conftest import trace

    marker = "RAW_DIAGNOSTIC_TRACE_" * 3000
    trace(service, principal, arguments={"path": "package.json", "diagnostic": marker})
    skill = service.learn(
        principal,
        LearnInput(
            run_token="training",
            completion_proof={
                "succeeded": True,
                "evidence_event_ids": ["read"],
                "summary": "Manifest checked",
            },
        ),
    )["procedure_token"]
    async with Client(build_mcp(service, stdio_key=credential["api_key"]), mode="legacy") as client:
        response = await client.call_tool("musclememory_inspect", {"procedure_token": skill})
        assert not response.is_error
        result = response.structured_content
        assert result["detail_truncated"] and result["needs_review"]
        assert result["successes"] == 0 and result["procedure_token"] == skill
        assert "RAW_DIAGNOSTIC_TRACE_" not in json.dumps(result)
        assert token_count(json.dumps(result)) <= 4000
        assert result["detail_path"].endswith("revision_selector=1")


async def test_stdio_real_subprocess(service, credential, principal):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "musclememory.cli", "stdio"],
        env={
            **os.environ,
            "MM_DATABASE_URL": service.settings.database_url,
            "MM_API_KEY": credential["api_key"],
        },
    )
    async with Client(params, mode="legacy") as client:
        await protocol_workflow(client, approver(service, principal))


async def test_stdio_rest_gateway_contract_and_revocation(
    remote_server, service, credential, principal
):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "musclememory.cli", "stdio", "--url", remote_server],
        env={**os.environ, "MM_API_KEY": credential["api_key"]},
    )
    async with Client(params, mode="legacy") as client:
        await protocol_workflow(client, approver(service, principal))
        service.revoke_key(principal, credential["key_id"], "team")
        result = await client.call_tool("musclememory_inspect", {"procedure_token": "unknown"})
        assert result.is_error
        assert "unauthorized" in str(result.content)


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_streamable_http_official_client(remote_server, credential, mode, service, principal):
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {credential['api_key']}"}
    ) as http:
        async with streamable_http_client(remote_server + "/mcp", http_client=http) as streams:
            # The official Client accepts a custom transport context manager.
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def transport_streams():
                yield streams[0], streams[1]

            async with Client(transport_streams(), mode=mode) as client:
                await protocol_workflow(client, approver(service, principal))
