import json

from musclememory.core import EvaluateInput, LearnInput, ObserveInput, RecallInput, RuntimeProfile
from musclememory.delivery import token_count
from musclememory.planning import compose_plan, task_segments

PROFILE = RuntimeProfile(bindings={"filesystem.read": "read", "filesystem.write": "write"})


def learned_edge(service, principal, title, reads, writes):
    for sample in range(3):
        token = title.replace(" ", "-") + "-" + str(sample)
        events = [{"event_id": "i", "sequence": 0, "kind": "intent", "intent_text": title}]
        for direction, paths in (("read", reads), ("write", writes)):
            for path in paths:
                events.append(
                    {
                        "event_id": "e" + str(len(events)),
                        "sequence": len(events),
                        "kind": "tool",
                        "tool_name": direction,
                        "capability": "filesystem." + direction,
                        "effect": direction,
                        "arguments": {"path": path},
                    }
                )
        proof = [events[-1]["event_id"]]
        events.append({"event_id": "end", "sequence": len(events), "kind": "completion"})
        service.observe(
            principal,
            ObserveInput(
                run_token=token,
                runtime_profile=PROFILE,
                visibility_scope="team",
                event_batch=events,
            ),
        )
        if sample == 0:
            learned = service.learn(
                principal,
                LearnInput(
                    run_token=token,
                    completion_proof={
                        "succeeded": True,
                        "evidence_event_ids": proof,
                        "summary": "Fixture outcome checked",
                    },
                ),
            )
            service.approve(principal, learned["procedure_token"])
        else:
            service.evaluate(
                principal,
                EvaluateInput(
                    procedure_token=learned["procedure_token"],
                    run_token=token,
                    outcome_report={
                        "succeeded": True,
                        "procedure_version": 1,
                        "evidence_event_ids": proof,
                    },
                    performance_metrics={
                        "observed": {"tool_calls": len(events) - 2, "duration_ms": 0}
                    },
                ),
            )
    return learned["procedure_token"]


def test_plan_inserts_transitive_prerequisites_in_order_and_keeps_uncovered_goals(
    service, principal
):
    schema = learned_edge(
        service, principal, "Build schema", ["src/spec.json"], ["build/schema.json"]
    )
    types = learned_edge(
        service, principal, "Generate types", ["build/schema.json"], ["build/types.py"]
    )
    routes = learned_edge(
        service, principal, "Create routes", ["build/types.py"], ["src/routes.py"]
    )
    for index in range(11):
        learned_edge(
            service,
            principal,
            f"Independent auxiliary workflow {index}",
            [f"aux/{index}.in"],
            [f"aux/{index}.out"],
        )
    result = service.recall(
        principal,
        RecallInput(intent_text="Create routes", runtime_profile=PROFILE, visibility_scope="team"),
    )
    plan = result["plan"]
    assert [step["procedure_token"] for step in plan["steps"]] == [schema, types, routes]
    assert [step["role"] for step in plan["steps"]] == ["prerequisite", "prerequisite", "requested"]
    assert plan["complete"] and plan["external_inputs"] == ["src/spec.json"]
    assert {(edge["from"], edge["to"]) for edge in plan["dependencies"]} == {
        (schema, types),
        (types, routes),
    }
    pointer = json.loads(result["guidance"])
    assert len(pointer["plan"]) == 3 and token_count(result["guidance"]) <= 350
    with service.db.session() as session:
        partial = compose_plan(
            session,
            principal.tenant,
            "team",
            [{"procedure_token": routes}],
            PROFILE,
            service.settings,
            gaps=[{"reason": "uncovered_subgoal", "segment": "Deploy service"}],
        )
        assert not partial["complete"] and partial["gaps"][0]["segment"] == "Deploy service"
        foreign = compose_plan(
            session,
            "other-tenant",
            "team",
            [{"procedure_token": routes}],
            PROFILE,
            service.settings,
        )
        assert not foreign["complete"] and not foreign["steps"]
    assert task_segments("Fix precedence; keep false values; preserve nested defaults") == [
        "Fix precedence; keep false values; preserve nested defaults"
    ]


def test_cycles_never_return_an_executable_schedule(service, principal):
    a = learned_edge(service, principal, "Cycle A", ["cycle/b"], ["cycle/a"])
    learned_edge(service, principal, "Cycle B", ["cycle/a"], ["cycle/b"])
    for index in range(12):
        learned_edge(
            service,
            principal,
            f"Independent auxiliary workflow {index}",
            [f"aux/{index}.in"],
            [f"aux/{index}.out"],
        )
    with service.db.session() as session:
        plan = compose_plan(
            session, principal.tenant, "team", [{"procedure_token": a}], PROFILE, service.settings
        )
        assert not plan["complete"] and not plan["steps"]
        assert any(gap["reason"] == "dependency_cycle" for gap in plan["gaps"])
