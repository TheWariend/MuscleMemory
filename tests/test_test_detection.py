import pytest
from musclememory import artifacts
from musclememory.artifacts import command_parts, operation_categories
from musclememory.workspace import piped_test

TEST_RUNS = [
    "pytest -q",
    "python3 -m unittest discover -s tests",
    "/bin/zsh -lc 'python tests/runtests.py admin_views'",
    "python manage.py test",
    "cargo test",
    "go test ./...",
    "npm test",
    "npm run test:unit",
    "uv run pytest",
    "CI=1 npx jest",
    "timeout 60 go test ./pkg",
    "make check",
    "cd app && pytest -x",
]
NOT_TEST_RUNS = [
    "cargo build",
    "cargo check",
    "ruff check .",
    "echo test",
    "go build ./...",
    "npm install",
    "mypy .",
    "make build",
    "python3 -m parcelkit.cli export input.json",
    "sed -n '1,260p' tests/test_contracts.py",
    "git diff -- tests/test_login.py",
]


@pytest.mark.parametrize("command", TEST_RUNS)
def test_real_test_runs_count_as_verification(command):
    assert "verify" in operation_categories({"command": command}, "shell.execute")


@pytest.mark.parametrize("command", NOT_TEST_RUNS)
def test_commands_that_only_mention_tests_do_not(command):
    assert "verify" not in operation_categories({"command": command}, "shell.execute")
    assert artifacts.test_command(command) is None


def test_test_command_keeps_only_the_test_run():
    assert artifacts.test_command("cd app && CI=1 pytest -x 2>&1 | tail") == "CI=1 pytest -x"


@pytest.mark.parametrize(
    ("command", "piped"),
    [("pytest 2>&1 | tail -5", True), ("pytest -q", False), ("cat x | grep y && pytest", False)],
)
def test_piped_test_needs_the_runner_before_the_pipe(command, piped):
    assert piped_test(command_parts(command), command) is piped
