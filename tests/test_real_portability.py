import os
import sys
import time
from pathlib import Path

from conftest import PROFILE
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from musclememory.compiler import materialize
from musclememory.core import EvaluateInput, LearnInput, ObserveInput, ProcedureDefinition
from musclememory.observer import ObservedRun


async def test_python_agent_to_stdio_mcp_agent_real_files(service, principal, credential, tmp_path):
    class Sink:
        def musclememory_observe(self, **arguments):
            return service.observe(principal, ObserveInput(**arguments))

    source = tmp_path / "first.toml"
    target = tmp_path / "second.toml"
    source.write_text('[project]\nname = "first"\n')
    target.write_text('[project]\nname = "second"\n')
    token = None
    for index in range(3):
        run = ObservedRun(Sink(), "Inspect repository manifest", PROFILE, "team", f"python-{index}")

        @run.instrument("repository.inspect")
        def read_file(path):
            text = Path(path).read_text()
            assert "[project]" in text
            return {"has_project": True}

        read_file(str(source))
        run.complete()
        if index == 0:
            token = service.learn(
                principal,
                LearnInput(
                    run_token=run.run_token,
                    completion_proof={
                        "succeeded": True,
                        "evidence_event_ids": ["e1"],
                        "summary": "Project section present",
                    },
                    learning_policy={
                        "parameters": {
                            "path": {
                                "definition": {"kind": "string"},
                                "observed_value": str(source),
                            }
                        }
                    },
                ),
            )["procedure_token"]
            service.approve(principal, token)
        else:
            service.evaluate(
                principal,
                EvaluateInput(
                    procedure_token=token,
                    run_token=run.run_token,
                    outcome_report={
                        "succeeded": True,
                        "procedure_version": 1,
                        "evidence_event_ids": ["e1"],
                    },
                    performance_metrics={"observed": run.measurements()},
                ),
            )

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
        profile = {
            "agent_name": "stdio-tool-agent",
            "bindings": {"repository.inspect": "workspace.read"},
        }
        recall = await client.call_tool(
            "musclememory_recall",
            {
                "intent_text": "Inspect repository manifest",
                "runtime_profile": profile,
                "visibility_scope": "team",
            },
        )
        candidate = recall.structured_content["candidates"][0]
        assert candidate["resolved_tools"]["repository.inspect"] == "workspace.read"
        inspected = await client.call_tool(
            "musclememory_inspect", {"procedure_token": candidate["procedure_token"]}
        )
        steps = materialize(
            ProcedureDefinition.model_validate(inspected.structured_content["definition"]),
            {"path": str(target)},
        )
        started = time.perf_counter()
        text = Path(steps[0]["arguments"]["path"]).read_text()
        assert 'name = "second"' in text
        duration = (time.perf_counter() - started) * 1000
        observed = await client.call_tool(
            "musclememory_observe",
            {
                "run_token": "adapted-run",
                "runtime_profile": profile,
                "visibility_scope": "team",
                "event_batch": [
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
                        "tool_name": "workspace.read",
                        "capability": "repository.inspect",
                        "arguments": {"path": str(target)},
                        "response": {"has_project": True},
                        "duration_ms": duration,
                    },
                    {"event_id": "done", "sequence": 2, "kind": "completion"},
                ],
            },
        )
        assert not observed.is_error
        evaluation = await client.call_tool(
            "musclememory_evaluate",
            {
                "procedure_token": token,
                "run_token": "adapted-run",
                "outcome_report": {
                    "succeeded": True,
                    "procedure_version": 1,
                    "evidence_event_ids": ["read"],
                },
                "performance_metrics": {"observed": {"tool_calls": 1, "duration_ms": duration}},
            },
        )
        assert not evaluation.is_error and evaluation.structured_content["successes"] == 3
