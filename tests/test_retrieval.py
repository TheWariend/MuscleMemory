from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from conftest import PROFILE, validated
from musclememory.compiler import compile_trace
from musclememory.config import Settings
from musclememory.core import ExecutionEvent, LearningPolicy, RecallInput
from musclememory.retrieval import Embeddings, bm25_scores, reciprocal_rank_fusion
from musclememory.storage import Revision


def test_bm25_frequency_length_and_stopwords():
    documents = ["manifest manifest inspect", "the manifest " + "unrelated " * 30, "bake cake"]
    scores = bm25_scores(documents, "the manifest")
    assert scores[0] > scores[1] > scores[2] == 0
    assert scores == bm25_scores(documents, "manifest")
    assert bm25_scores([], "manifest") == []


def test_rrf_merges_independent_rankings_once():
    scores = reciprocal_rank_fusion([["a", "b", "a"], ["c", "b"]])
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(2 / 62)
    assert scores["b"] > scores["a"] == scores["c"]


def test_tied_fusion_uses_relevance_before_alphabetical_title():
    from musclememory.retrieval import rank_candidates
    from musclememory.storage import utcnow

    now = utcnow()

    def candidate(token, title, text, vector):
        definition = {
            "title": title,
            "intent_signature": title,
            "steps": [],
            "required_capabilities": [],
            "required_permissions": [],
            "environment_requirements": {},
        }
        return (
            SimpleNamespace(procedure_token=token, lifecycle="active", reviewed_at=now),
            SimpleNamespace(
                definition=definition,
                search_text=text,
                embedding=vector,
                embedding_payload=None,
                embedding_space="test-space",
                embedding_dimensions=2,
                validated_at=now,
                created_at=now,
                test_proven_at=now,
                successes=2,
                failures=0,
                validation_status="validated",
                version=1,
                capability_signature=[],
            ),
        )

    lexical = candidate("a", "Arrange a courier", "arrange arrange arrange courier", [0, 1])
    semantic = candidate(
        "z", "Request a video appointment", "request video appointment", [0.8, 0.6]
    )
    settings = Settings(
        embedding_provider="remote",
        embedding_url="https://provider.test/embed",
        embedding_dim=2,
        min_match=0.1,
    )
    for candidates in [[lexical, semantic], [semantic, lexical]]:
        ranked = rank_candidates(
            candidates, "arrange virtual consultation", [1, 0], PROFILE, settings, "test-space"
        )
        assert ranked[0]["score"] == ranked[1]["score"]
        assert ranked[0]["procedure_token"] == "z"
        assert ranked[0]["relevance"] > ranked[1]["relevance"]


def test_graph_prunes_failed_branch_preserves_parallel_sibling():
    events = [
        ExecutionEvent(
            event_id="root",
            sequence=0,
            kind="tool",
            tool_name="read",
            capability="repository.inspect",
        )
    ]
    for index, (token, dependencies, status) in enumerate(
        [
            ("failed", ["root"], "failure"),
            ("lost", ["failed"], "success"),
            ("lost_child", ["lost"], "success"),
            ("sibling", ["root"], "success"),
            ("parallel", ["root"], "success"),
        ],
        1,
    ):
        events.append(
            ExecutionEvent(
                event_id=token,
                sequence=index,
                kind="tool",
                tool_name="read",
                capability="repository.inspect",
                dependencies=dependencies,
                status=status,
            )
        )
    events.append(ExecutionEvent(event_id="done", sequence=len(events), kind="completion"))
    definition, _ = compile_trace("inspect", events, LearningPolicy(), "graph", {"summary": "ok"})
    assert definition.step_graph == {
        "nodes": ["root", "sibling", "parallel"],
        "edges": [["root", "sibling"], ["root", "parallel"]],
    }
    assert {step.step_token for step in definition.steps} == {"root", "sibling", "parallel"}


def test_configurable_embedding_dimension_and_hash_label(service, principal, monkeypatch):
    assert len(Embeddings(Settings(embedding_dim=32)).embed("a sentence")) == 32
    for dimension in (0, 2001):
        with pytest.raises(ValueError):
            Settings(embedding_dim=dimension)
    service.settings = replace(service.settings, embedding_dim=32)
    service.embeddings = Embeddings(service.settings)
    learned = validated(service, principal)
    with service.db.session() as session:
        revision = session.get(Revision, (learned["procedure_token"], 1))
        assert len(revision.embedding) == 32 and revision.embedding_payload is None
    result = service.recall(
        principal,
        RecallInput(
            intent_text="Inspect repository manifest",
            runtime_profile=PROFILE,
            visibility_scope="team",
        ),
    )
    assert result["vector_mode"] == "lexical-hash"
    explanation = result["candidates"][0]["explanation"]
    assert "lexical-hash" in explanation and "vector_similarity" not in explanation
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: httpx.Response(
            200, request=httpx.Request("POST", url), json={"data": [{"embedding": [1.0] * 32}]}
        ),
    )
    embeddings = Embeddings(
        Settings(embedding_dim=32, embedding_url="https://provider.example/embed")
    )
    assert embeddings.embed("semantic fixture") == [1.0] * 32


def test_file_change_tools_are_interchangeable_but_others_are_not():
    from musclememory.core import RuntimeProfile
    from musclememory.shapes import bound_capability, conditions_satisfied

    claude = RuntimeProfile(
        agent_name="claude", bindings={"filesystem.edit": "Edit", "shell.execute": "Bash"}
    )
    assert bound_capability("filesystem.patch", claude.bindings) == "filesystem.edit"
    assert bound_capability("filesystem.write", claude.bindings) == "filesystem.edit"
    assert bound_capability("browser.navigate", claude.bindings) is None
    assert conditions_satisfied(
        ["capability:filesystem.patch", "capability:shell.execute"], claude, []
    )
    assert not conditions_satisfied(["capability:filesystem.delete"], claude, [])


@pytest.mark.parametrize(
    ("command", "looking"),
    [
        ("pwd && rg --files", True),
        ("sed -n '1,240p' app/a.py", True),
        ("git diff -- app/a.py", True),
        ("ls; grep -n x -r tests | head -20", True),
        ("sed -i s/a/b/ app/a.py", False),
        ("cat a > b", False),
        ("git commit -am fix", False),
        ("find . -name '*.tmp' -delete", False),
        ("python3 -m unittest", False),
        ("git ls-files && cat CLAUDE.md 2>/dev/null | head -50", True),
        ("grep -rn merge . 2>&1 | head", True),
        ("cat a 2>/dev/null > b", False),
        ("ls > files.txt 2>&1", False),
    ],
)
def test_inspection_only_commands(command, looking):
    from musclememory.artifacts import inspection_only

    assert inspection_only({"command": command}, "shell.execute") is looking


@pytest.mark.parametrize(
    ("command", "test"),
    [
        (
            "python3 -m unittest discover -s tests 2>&1 | tail -5",
            "python3 -m unittest discover -s tests",
        ),
        ("python3 - <<'E'\nfix()\nE\npython3 -m unittest 2>&1 | tail -5", "python3 -m unittest"),
        ("cd repo && pytest -q tests/test_a.py > out.txt", "pytest -q tests/test_a.py"),
        ("npm test 2>&1 | tail", "npm test"),
        ("git status", None),
    ],
)
def test_the_test_run_is_taken_out_of_longer_commands(command, test):
    from musclememory.artifacts import test_command

    assert test_command(command) == test


@pytest.mark.parametrize(
    "command",
    [
        "./tests/runtests.py forms_tests --settings=test_sqlite",
        "python tests/runtests.py migrations.test_operations 2>&1 | tail -20",
        "cd tests && python runtests.py model_forms",
        "tox -e py311",
    ],
)
def test_project_test_runners_count_as_checks(command):
    from musclememory.artifacts import operation_categories, test_command

    assert "verify" in operation_categories({"command": command}, "shell.execute")
    assert test_command(command)
