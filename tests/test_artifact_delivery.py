import json
from dataclasses import replace

from musclememory.artifacts import extract_experience, normalized_path, operation_categories
from musclememory.core import (
    EvaluateInput,
    InspectInput,
    LearnInput,
    ObserveInput,
    RecallInput,
    RuntimeProfile,
)
from musclememory.delivery import render_pointer, token_count
from musclememory.retrieval import graph_neighbors
from musclememory.storage import ArtifactPosting
from sqlalchemy import func, select

PROFILE = RuntimeProfile(bindings={"filesystem.read": "read", "filesystem.write": "write"})


def procedure(service, principal, name, direction, path):
    for index in range(3):
        token = f"{name.replace(' ', '-')}-{index}"
        service.observe(
            principal,
            ObserveInput(
                run_token=token,
                visibility_scope="team",
                runtime_profile=PROFILE,
                event_batch=[
                    {"event_id": "intent", "sequence": 0, "kind": "intent", "intent_text": name},
                    {
                        "event_id": "action",
                        "sequence": 1,
                        "kind": "tool",
                        "tool_name": direction,
                        "capability": "filesystem." + direction,
                        "effect": direction,
                        "arguments": {"path": path},
                    },
                    {"event_id": "done", "sequence": 2, "kind": "completion"},
                ],
            ),
        )
        if not index:
            learned = service.learn(
                principal,
                LearnInput(
                    run_token=token,
                    completion_proof={
                        "succeeded": True,
                        "evidence_event_ids": ["action"],
                        "summary": "Checked actual fixture",
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
                        "evidence_event_ids": ["action"],
                    },
                    performance_metrics={"observed": {"tool_calls": 1, "duration_ms": 0}},
                ),
            )
    return learned["procedure_token"]


def test_exact_artifact_and_typed_graph_is_scoped_and_prunes_hubs(service, principal):
    producer = procedure(
        service, principal, "Build the generated schema", "write", "out/schema.json"
    )
    consumer = procedure(
        service, principal, "Validate generated definitions", "read", "out/schema.json"
    )
    for index in range(18):
        procedure(
            service,
            principal,
            f"Independent auxiliary workflow {index}",
            "read",
            f"aux/{index}.txt",
        )
    recalled = service.recall(
        principal,
        RecallInput(
            intent_text="Use out/schema.json", visibility_scope="team", runtime_profile=PROFILE
        ),
    )
    assert {c["procedure_token"] for c in recalled["candidates"]} == {producer, consumer}
    assert all(c["explanation"]["exact_target_matches"] == 1 for c in recalled["candidates"])
    source = next(c for c in recalled["candidates"] if c["procedure_token"] == consumer)
    with service.db.session() as session:
        related = graph_neighbors(
            session, principal.tenant, "team", [source], PROFILE, service.settings
        )
        assert related == [
            {
                "procedure_token": producer,
                "procedure_version": 1,
                "title": "Build the generated schema",
                "relation": "prerequisite",
                "artifact": "out/schema.json",
            }
        ]
    procedure(service, principal, "Another consumer", "read", "out/schema.json")
    procedure(service, principal, "Fourth consumer", "read", "out/schema.json")
    # Four procedures now share the artifact; a cap of three treats it as a hub.
    service.settings = replace(service.settings, link_fanout_limit=3)
    with service.db.session() as session:
        assert (
            graph_neighbors(session, principal.tenant, "team", [source], PROFILE, service.settings)
            == []
        )
        assert (
            graph_neighbors(session, "foreign", "team", [source], PROFILE, service.settings) == []
        )
    service.delete_memory(principal, producer)
    with service.db.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ArtifactPosting)
                .where(ArtifactPosting.procedure_token == producer)
            )
            == 0
        )


def test_pointer_contains_useful_targets_with_valid_json_within_budget(service, principal):
    token = procedure(service, principal, "Write schema", "write", "generated/schema.json")
    result = service.recall(
        principal,
        RecallInput(intent_text="Write schema", visibility_scope="team", runtime_profile=PROFILE),
    )
    result["candidates"][0]["definition"]["steps"][0]["arguments"]["source"] = (
        "large source" * 10000
    )
    pointer = render_pointer(result, 350)
    data = json.loads(pointer)
    assert data["procedure"] == token and data["changed_files"] == ["generated/schema.json"]
    assert "large source" not in pointer and "approvals" in data["notice"]
    assert token_count(pointer) <= 350
    definition = result["candidates"][0]["definition"]
    definition["required_permissions"] = [f"permission.{i}" for i in range(1000)]
    assert render_pointer(result, 128) == ""


def test_delivery_preserves_source_edits_and_separates_changed_targets_from_reads():
    steps = [
        {
            "step_token": "read",
            "arguments": {"path": "tests/test_contracts.py"},
            "capability": "filesystem.read",
            "effect": "read",
        },
        {
            "step_token": "patch",
            "arguments": {
                "patch": "*** Begin Patch\n*** Update File: src/math.py\n@@\n"
                "-return int(value)\n+return round(value)\n*** End Patch"
            },
            "capability": "filesystem.patch",
            "effect": "write",
        },
        {
            "step_token": "test",
            "arguments": {"command": "pytest tests/test_contracts.py"},
            "capability": "shell.execute",
            "effect": "destructive",
        },
    ]
    definition = {
        "steps": steps,
        "title": "Round fractional values",
        "required_capabilities": [],
        "required_permissions": [],
        "preconditions": [],
    }
    result = {
        "candidates": [
            {
                "procedure_token": "p",
                "procedure_version": 1,
                "relevance": 1,
                "definition": definition,
            }
        ],
        "inspection_available": False,
    }
    pointer = json.loads(render_pointer(result, 350))
    assert pointer["changed_files"] == ["src/math.py"]
    assert pointer["verify"] == ["pytest tests/test_contracts.py"]
    assert pointer["edits"][0]["file"] == "src/math.py"
    assert pointer["edits"] == [
        {"file": "src/math.py", "before": "return int(value)", "after": "return round(value)"}
    ]
    assert "inspect" not in pointer and "tests/test_contracts.py" not in pointer["changed_files"]
    assert token_count(render_pointer(result, 350)) < 300


def test_extraction_keeps_verifier_and_patch_targets_not_shell_bodies():
    experience = extract_experience(
        [
            {
                "step_token": "patch",
                "arguments": {"input": "*** Update File: src/module.py\n+content"},
                "capability": "filesystem.patch",
                "effect": "write",
            },
            {
                "step_token": "test",
                "arguments": {"command": "python3 -m unittest discover -s tests"},
                "capability": "shell.execute",
                "effect": "destructive",
            },
        ]
    )
    assert experience["writes"] == ["src/module.py"]
    assert experience["verification_commands"] == ["python3 -m unittest discover -s tests"]
    assert normalized_path("../outside") is None
    assert normalized_path("secret?token=value") is None
    assert operation_categories({"command": "cat totally-private-file.json"}, "shell.execute") == [
        "inspect"
    ]


def test_programs_and_listed_folders_are_not_files_a_skill_reads():
    def reads(command):
        return extract_experience(
            [
                {
                    "step_token": "run",
                    "arguments": {"command": command},
                    "capability": "shell.execute",
                    "effect": "destructive",
                }
            ]
        )["reads"]

    assert reads('ls -a | grep -i -E "agents|claude"; ls .github') == []
    assert reads(
        ".venv/bin/ruff check src/a.py && .venv/bin/python -m pytest tests/test_a.py -q"
    ) == ["src/a.py", "tests/test_a.py"]
    assert reads("CI=1 ./node_modules/.bin/jest src/a.test.js") == ["src/a.test.js"]
    assert reads("cat src/a.py; head docs/b.md") == ["docs/b.md", "src/a.py"]


def test_reindex_preserves_validation_and_history(service, principal):
    token = procedure(service, principal, "Build schema", "write", "schema.json")
    before = service.inspect(
        principal,
        InspectInput(procedure_token=token),
    )
    result = service.reindex_embeddings(principal, "team")
    after = service.inspect(
        principal,
        InspectInput(procedure_token=token),
    )
    assert result["indexed_revisions"] == 1
    assert before["definition"] == after["definition"] and after["successes"] == 2
    assert before["validation_history"] == after["validation_history"]


def test_edits_keep_only_changed_lines_and_separate_changes():
    from musclememory.artifacts import patch_edits

    patch = (
        "*** Begin Patch\n*** Update File: app/money.py\n@@\n"
        "-from decimal import Decimal\n+from decimal import Decimal\n \n def cents(value):\n"
        "-    return int(float(value) * 100)\n+    return int(Decimal(value) * 100)\n"
        "*** Update File: app/exports.py\n@@\n     for row in rows:\n"
        "+        if row in seen:\n+            continue\n         amount = cents(row)\n"
        "-        amount = abs(amount)\n+        amount = -abs(amount)\n*** End Patch\n"
    )
    assert patch_edits({"command": patch}, "filesystem.patch") == [
        {
            "file": "app/money.py",
            "before": "    return int(float(value) * 100)",
            "after": "    return int(Decimal(value) * 100)",
        },
        {"file": "app/exports.py", "after": "        if row in seen:\n            continue"},
        {
            "file": "app/exports.py",
            "before": "        amount = abs(amount)",
            "after": "        amount = -abs(amount)",
        },
    ]
    # A Claude Code edit that repeats unchanged lines around the change.
    edit = {
        "file_path": "app/money.py",
        "old_string": "def cents(value):\n    return 1",
        "new_string": "def cents(value):\n    return 2",
    }
    assert patch_edits(edit, "filesystem.edit") == [
        {"file": "app/money.py", "before": "    return 1", "after": "    return 2"}
    ]
    assert patch_edits({**edit, "new_string": edit["old_string"]}, "filesystem.edit") == []


def test_without_the_tokenizer_the_estimate_is_close_not_four_times_over(monkeypatch):
    from musclememory import delivery

    monkeypatch.setattr(delivery, "tokenizer", lambda: None)
    text = '{"file": "parcelkit/settings.py", "after": "result[key] = value"}'
    assert delivery.token_count(text) == -(-len(text) * 2 // 7)
    assert delivery.token_count(text) < len(text) / 3
