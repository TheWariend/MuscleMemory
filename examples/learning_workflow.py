"""Run against an authenticated server: python examples/learning_workflow.py."""

import json
import os
from pathlib import Path
from uuid import uuid4

from musclememory.compiler import materialize
from musclememory.core import ProcedureDefinition
from musclememory.observer import ObservedRun
from musclememory.sdk import MuscleMemoryClient


def workflow(client, namespace="default"):
    profile = {
        "agent_name": "python-files-agent",
        "bindings": {"repository.inspect": "read_manifest"},
    }
    source = Path(__file__).resolve().parents[1] / "pyproject.toml"
    learned = None
    for index in range(3):
        run = ObservedRun(
            client,
            "Inspect project package manifest",
            profile,
            namespace,
            run_token=f"example-{uuid4()}",
        )

        @run.instrument("repository.inspect")
        def read_manifest(path):
            content = Path(path).read_text()
            assert "[project]" in content
            return {"contains_project_metadata": True, "bytes": len(content.encode())}

        assert read_manifest(str(source))["contains_project_metadata"]
        run.complete()
        if index == 0:
            learned = client.musclememory_learn(
                run_token=run.run_token,
                completion_proof={
                    "succeeded": True,
                    "evidence_event_ids": ["e1"],
                    "summary": "Package manifest contains project metadata",
                },
                learning_policy={
                    "title": "Inspect a project manifest",
                    "parameters": {
                        "manifest_path": {
                            "definition": {
                                "kind": "string",
                                "description": "Authorized manifest path",
                            },
                            "observed_value": str(source),
                        }
                    },
                    "preconditions": ["Caller has read permission for the supplied manifest path"],
                },
            )
        else:
            client.musclememory_evaluate(
                procedure_token=learned["procedure_token"],
                run_token=run.run_token,
                outcome_report={
                    "succeeded": True,
                    "procedure_version": 1,
                    "evidence_event_ids": ["e1"],
                },
                performance_metrics={"observed": run.measurements()},
            )
    # Every skill waits for a person's OK; approving needs an admin key.
    client.approve(learned["procedure_token"])
    other_profile = {
        "agent_name": "another-compatible-agent",
        "bindings": {"repository.inspect": "workspace.read"},
    }
    recalled = client.musclememory_recall(
        intent_text="Inspect project package manifest",
        runtime_profile=other_profile,
        visibility_scope=namespace,
        match_limit=1,
    )
    assert not recalled["no_match"]
    candidate = recalled["candidates"][0]
    # Adapt data only; the agent still chooses whether it is authorized to call the tool.
    adapted = materialize(
        ProcedureDefinition.model_validate(candidate["definition"]), {"manifest_path": str(source)}
    )
    return {
        "procedure_token": learned["procedure_token"],
        "resolved_tools": candidate["resolved_tools"],
        "adapted_steps": adapted,
        "independent_successes": candidate["successes"],
    }


if __name__ == "__main__":
    with MuscleMemoryClient(
        os.getenv("MM_API_URL", "http://127.0.0.1:8080"), os.environ["MM_API_KEY"]
    ) as client:
        print(json.dumps(workflow(client, os.getenv("MM_NAMESPACE", "default")), indent=2))
