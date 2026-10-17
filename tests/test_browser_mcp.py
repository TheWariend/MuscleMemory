"""The browser MCP server: learn from browsing, check, approve, then run the proven skill."""

import asyncio
from dataclasses import replace

import httpx
import pytest
from musclememory.browser_mcp import BrowserMemory, build_browser_mcp
from musclememory.sdk import MuscleMemoryClient

URL = "http://portal.test/apply"


class FakeDriver:
    """A two-stage application: details (email, plan), then review (code), then submit."""

    def __init__(self, *, renamed=False, code_label="Code"):
        self.renamed, self.code_label, self.clicks = renamed, code_label, []

    async def open(self, url):
        self.url, self.stage, self.notices = url, "details", []
        self.values = {"email": "", "plan": "", "code": ""}

    async def snapshot(self):
        def control(index, role, name, section, value=""):
            return {
                "index": index,
                "role": role,
                "name": name,
                "value": value,
                "landmarks": ["Application", section],
                "enabled": True,
            }

        if self.stage == "details":
            controls = [
                control(
                    0,
                    "textbox",
                    "Contact email" if self.renamed else "Email",
                    "Details",
                    self.values["email"],
                ),
                control(1, "combobox", "Plan", "Details", self.values["plan"]),
                control(2, "button", "Continue", "Details"),
            ]
        elif self.stage == "review":
            controls = [
                control(3, "textbox", self.code_label, "Review", self.values["code"]),
                control(4, "button", "Submit", "Review"),
            ]
        else:
            controls = []
        return {"url": self.url, "text": "", "notices": self.notices, "controls": controls}

    async def act(self, index, action, value):
        key = {0: "email", 1: "plan", 3: "code"}.get(index)
        if key:
            self.values[key] = value
        elif index == 2 and self.values["email"] and self.values["plan"]:
            self.stage = "review"
        elif index == 4 and self.values["code"]:
            self.stage, self.notices = "sent", ["Application received."]
            self.clicks.append("submit")


def ref(page, name):
    return next(c["ref"] for c in page["controls"] if c["name"] == name)


async def by_hand(memory, email, plan, code, *, task="Apply for a plan", submit=True):
    page = await memory_open(memory, task)
    page = await memory.act(ref(page, "Email"), "fill", email)
    page = await memory.act(ref(page, "Plan"), "select", plan)
    page = await memory.act(ref(page, "Continue"), "click")
    page = await memory.act(ref(page, memory.driver.code_label), "fill", code)
    if submit:
        await memory.act(ref(page, "Submit"), "click")
    return await memory.done(True)


async def memory_open(memory, task):
    if memory.run is None:
        await memory.start(task)
    await memory.driver.open(URL)
    return await memory.snapshot()


@pytest.fixture
def client(http, credential):
    def forward(request):
        response = http.request(
            request.method, request.url.raw_path.decode(), content=request.content,
            headers=dict(request.headers),
        )  # fmt: skip
        return httpx.Response(response.status_code, content=response.content)

    with MuscleMemoryClient(
        "http://testserver", credential["api_key"], transport=httpx.MockTransport(forward)
    ) as client:
        yield client


def test_learn_check_approve_then_run_the_skill(client, service, principal, tmp_path):
    memory = BrowserMemory(client, "team", FakeDriver(), state_dir=tmp_path)

    async def scenario():
        learned = await by_hand(memory, "a@x.test", "Basic", "C-1")
        token = learned["learned"]
        for index in range(2):  # two later tasks done by hand count as its checks
            checked = await by_hand(memory, f"b{index}@x.test", "Pro", f"C-{index + 2}")
            assert checked["checked"] == token
        await memory_open(memory, "Apply for a plan")
        assert memory.find_skills()["skills"] == []  # checked twice, waiting for a person's OK
        await memory.done(False)
        service.approve(principal, token)

        page = await memory_open(memory, "Apply for a plan")
        assert "Proven skill" in page["skill"]
        [skill] = memory.find_skills()["skills"]
        assert {i["field"] for i in skill["inputs"]} == {"Email", "Plan", "Code"}
        inputs = {
            i["name"]: v for i, v in zip(skill["inputs"], ["new@x.test", "Max", "C-9"], strict=True)
        }
        result = await memory.run_skill(token, inputs)
        assert "Submit" in result["ready"] and memory.driver.clicks == ["submit"] * 3
        assert memory.driver.values == {"email": "new@x.test", "plan": "Max", "code": "C-9"}
        page = await memory.snapshot()
        await memory.act(ref(page, "Submit"), "click")  # the agent submits
        return await memory.done(True)

    final = asyncio.run(scenario())
    assert final["result"]["successes"] == 3


def test_auto_approve_skips_the_ok_but_not_the_checks(client, service, tmp_path):
    service.settings = replace(service.settings, auto_approve=True)
    memory = BrowserMemory(client, "team", FakeDriver(), state_dir=tmp_path)

    async def scenario():
        await by_hand(memory, "a@x.test", "Basic", "C-1")
        await memory_open(memory, "Apply for a plan")
        assert memory.find_skills()["skills"] == []  # approved, not yet checked
        await memory.done(False)
        for index in range(2):
            await by_hand(memory, f"b{index}@x.test", "Pro", f"C-{index + 2}")
        await memory_open(memory, "Apply for a plan")
        return memory.find_skills()["skills"]

    assert len(asyncio.run(scenario())) == 1


def test_nothing_is_learned_without_a_result_on_the_site(client, tmp_path):
    memory = BrowserMemory(client, "team", FakeDriver(), state_dir=tmp_path)
    result = asyncio.run(by_hand(memory, "a@x.test", "Basic", "C-1", submit=False))
    assert result["learned"] is False


def test_a_renamed_field_is_left_for_the_agent(client, service, principal, tmp_path):
    memory = BrowserMemory(client, "team", FakeDriver(), state_dir=tmp_path)

    async def scenario():
        token = (await by_hand(memory, "a@x.test", "Basic", "C-1"))["learned"]
        for index in range(2):
            await by_hand(memory, f"b{index}@x.test", "Pro", f"C-{index + 2}")
        service.approve(principal, token)
        memory.driver.renamed = True
        await memory_open(memory, "Apply for a plan")
        [skill] = memory.find_skills()["skills"]
        inputs = {
            i["name"]: v for i, v in zip(skill["inputs"], ["new@x.test", "Max", "C-9"], strict=True)
        }
        return await memory.run_skill(token, inputs)

    result = asyncio.run(scenario())
    assert result["stopped"] == "fill_these_then_run_again"
    assert result["left_for_you"][0]["field"] == "Email"


def test_server_lists_the_tools(client, tmp_path):
    server = build_browser_mcp(BrowserMemory(client, "team", FakeDriver(), state_dir=tmp_path))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {"browser_open", "browser_read", "browser_act", "browser_done",
                     "musclememory_find_skills", "musclememory_run_skill"}  # fmt: skip


@pytest.mark.parametrize("agent", ["claude", "codex", "cursor"])
def test_connect_registers_the_browser_without_losing_settings(tmp_path, agent):
    import json
    import tomllib

    from musclememory.local import add_browser

    hooks = tmp_path / "hooks.json"
    if agent == "codex":
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "config.toml").write_text('model = "x"\n')
    elif agent == "claude":
        (tmp_path / ".mcp.json").write_text('{"mcpServers": {"other": {"command": "o"}}}')
    path = add_browser(agent, tmp_path, hooks)
    add_browser(agent, tmp_path, hooks)  # running connect twice changes nothing more
    if agent == "codex":
        config = tomllib.loads(path.read_text())
        assert config["model"] == "x"
        server = config["mcp_servers"]["musclememory_browser"]
    else:
        config = json.loads(path.read_text())
        server = config["mcpServers"]["musclememory-browser"]
        if agent == "claude":
            assert "other" in config["mcpServers"]
    assert server["args"][-3:] == ["browser", "--config", str(hooks)]


def test_passwords_are_never_recorded_and_left_for_the_agent(client, service, principal, tmp_path):
    from musclememory.core import InspectInput

    memory = BrowserMemory(client, "team", FakeDriver(code_label="Password"), state_dir=tmp_path)

    async def scenario():
        token = (await by_hand(memory, "a@x.test", "Basic", "hunter2-secret"))["learned"]
        for index in range(2):
            await by_hand(memory, f"b{index}@x.test", "Pro", "hunter2-secret")
        service.approve(principal, token)
        await memory_open(memory, "Apply for a plan")
        [skill] = memory.find_skills()["skills"]
        assert {i["field"] for i in skill["inputs"]} == {"Email", "Plan"}
        inputs = dict(zip([i["name"] for i in skill["inputs"]], ["n@x.test", "Max"], strict=True))
        return token, await memory.run_skill(token, inputs)

    token, result = asyncio.run(scenario())
    assert result["left_for_you"] == [{"field": "Password", "why": "needs your input"}]
    assert memory.driver.values["code"] == ""  # never typed by the skill
    stored = service.inspect(principal, InspectInput(procedure_token=token))
    assert "hunter2-secret" not in str(stored)


def test_no_skills_hides_tools_and_hint_but_still_learns(client, tmp_path):
    memory = BrowserMemory(client, "team", FakeDriver(), state_dir=tmp_path, skills_enabled=False)

    async def scenario():
        server = build_browser_mcp(memory)
        names = {t.name for t in await server.list_tools()}
        assert names == {"browser_open", "browser_read", "browser_act", "browser_done"}
        assert "musclememory_find_skills" not in server.instructions
        result = await by_hand(memory, "a@x.test", "Basic", "C-1")
        assert result["learned"]
        await memory_open(memory, "Apply for a plan")
        memory.skills = [
            {"start": ["http", "portal.test", 80, "/apply"], "title": "Hidden", "skill": "hidden"}
        ]
        assert "skill" not in await memory.snapshot()

    asyncio.run(scenario())


def test_tool_log_append_errors_and_secret_redaction(client, tmp_path):
    import json

    from musclememory.browser_mcp import FILLED, log_browser_call

    memory = BrowserMemory(client, "team", FakeDriver(code_label="Password"))
    path = tmp_path / "calls.jsonl"
    path.write_text("")

    async def scenario():
        await memory_open(memory, "Test")
        memory.refs["secret"] = {"name": "Password"}

        async def browser_act(ref: str, action: str, value: str = "") -> dict:
            if value == "explode":
                raise ValueError(value)
            return {"error": value} if value == "bad" else {}

        async def act(ref, action, value):
            return await log_browser_call(
                "browser_act",
                dict(ref=ref, action=action, value=value),
                memory,
                path,
                lambda: browser_act(ref, action, value),
            )

        await act(ref="secret", action="fill", value="never-log-this")
        await act(ref="1:0", action="fill", value="ordinary")
        await act(ref="1:0", action="fill", value="bad")
        with pytest.raises(ValueError):
            await act(ref="secret", action="fill", value="explode")

        async def musclememory_run_skill(skill: str, inputs: dict) -> dict:
            return {}

        inputs = {"password": "secret-value", "name": "Ari"}
        await log_browser_call(
            "musclememory_run_skill",
            {"skill": "test", "inputs": inputs},
            memory,
            path,
            lambda: musclememory_run_skill("test", inputs),
        )
        page = await memory.snapshot()
        page = await memory.act(ref(page, "Continue"), "click")
        # Directly snapshot a filled secret to check the page response too.
        memory.driver.stage = "review"
        memory.driver.values["code"] = "also-secret"
        assert "also-secret" not in str(await memory.snapshot())

    asyncio.run(scenario())
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 5
    assert rows[0]["arguments"]["value"] == FILLED
    assert rows[1]["arguments"]["value"] == "ordinary"
    assert rows[2]["error"] == "tool_error"
    assert rows[3]["error"] == "ValueError"
    assert rows[4]["arguments"]["inputs"] == {"password": FILLED, "name": "Ari"}
    assert all(row["duration_ms"] >= 0 for row in rows)
    assert "never-log-this" not in path.read_text()
    assert "secret-value" not in path.read_text()


def test_browser_cli_flags(monkeypatch):
    from musclememory import browser_mcp, cli

    calls = []
    monkeypatch.setattr(
        "sys.argv",
        [
            "musclememory",
            "browser",
            "--config",
            "config.json",
            "--no-skills",
            "--log",
            "calls.jsonl",
        ],
    )
    monkeypatch.setattr(browser_mcp, "serve", lambda *args, **kwargs: calls.append((args, kwargs)))
    cli.main()
    assert calls == [
        (("config.json",), {"headless": True, "no_skills": True, "log_path": "calls.jsonl"})
    ]


def test_logged_tools_work_through_mcp_dispatch(client, tmp_path):
    import json

    memory = BrowserMemory(client, "team", FakeDriver())
    path = tmp_path / "mcp.jsonl"

    async def scenario():
        server = build_browser_mcp(memory, log_path=path)
        await memory_open(memory, "Apply")
        page = await memory.snapshot()
        await server.call_tool(
            "browser_act", {"ref": ref(page, "Email"), "action": "fill", "value": "ari@test"}
        )
        await server.call_tool("musclememory_find_skills", {})
        return {t.name: t.input_schema for t in await server.list_tools()}

    schemas = asyncio.run(scenario())
    assert set(schemas["browser_act"]["properties"]) == {"ref", "action", "value"}
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["tool"] for r in rows] == ["browser_act", "musclememory_find_skills"]
    assert not any(r["error"] for r in rows)


def test_invalid_and_unknown_mcp_calls_are_counted_once(client, tmp_path):
    import json

    from mcp.server.mcpserver.exceptions import ToolError

    memory = BrowserMemory(client, "team", FakeDriver())
    path = tmp_path / "invalid.jsonl"

    async def scenario():
        server = build_browser_mcp(memory, log_path=path)
        with pytest.raises(ToolError):
            await server.call_tool(
                "browser_act", {"ref": ["bad-ref"], "action": "fill", "value": "do-not-log"}
            )
        with pytest.raises(ToolError):
            await server.call_tool("unknown", {"password": "do-not-log"})
        await server.call_tool("browser_open", {"url": "file:///forbidden"})

    asyncio.run(scenario())
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["tool"] for r in rows] == ["browser_act", "unknown", "browser_open"]
    assert all(r["error"] for r in rows)
    assert "do-not-log" not in path.read_text()
