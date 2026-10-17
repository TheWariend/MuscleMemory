"""MuscleMemory's browser MCP server: browse, learn proven skills, and run them on request.

Agents browse with `browser_open`, `browser_read` and `browser_act`. Every action is
recorded with what the page showed before and after. When the agent finishes with
`browser_done`, a task the site confirmed becomes a skill, or counts as a check on one.
Proven skills (independent checks plus a person's OK, or automatic approval if the
installation allows it) can be found with `musclememory_find_skills` and carried out
with `musclememory_run_skill`, which does every step with checks except the final
submit. The agent always submits.
"""

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from .browser import (
    BROWSER_CONTEXT,
    BrowserState,
    capture_browser_context,
    carry_out,
    page_identity,
    record_browser_action,
    skill_inputs,
)
from .observer import ObservedRun
from .security import SENSITIVE_KEY

FILLED = "•••"  # what MuscleMemory keeps for a secret field the agent filled
PROFILE = {
    "agent_name": "musclememory-browser",
    "bindings": {
        "browser.navigate": "browser_open",
        "browser.act": "browser_act",
        "browser.verify": "browser_done",
    },
}
SNAPSHOT = """() => {
  const all=[...document.querySelectorAll('input,select,textarea,button,a')];
  function name(e) { return e.getAttribute('aria-label') ||
    (e.labels && [...e.labels].map(l=>l.innerText.trim()).join(' ')) ||
    e.innerText.trim() || e.getAttribute('placeholder') || ''; }
  function landmarks(e) { const found=[]; for(let p=e.parentElement;p;p=p.parentElement) {
    if(p.getAttribute('aria-label')) found.unshift(p.getAttribute('aria-label')); }
    return found; }
  return {url:location.href, text:document.body.innerText.slice(0,4000),
    notices:[...document.querySelectorAll('[role=status],[role=alert]')]
      .map(e=>e.innerText.trim()).filter(Boolean),
    controls:all.map((e,i)=>({index:i, name:name(e), landmarks:landmarks(e),
      role:e.tagName==='SELECT'?'combobox':e.tagName==='BUTTON'?'button':
        e.tagName==='A'?'link':e.type==='checkbox'?'checkbox':
        e.type==='submit'||e.type==='button'?'button':'textbox',
      value:e.type==='checkbox'?String(e.checked):(e.value||''),
      visible:!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden',
      enabled:!e.disabled,
      options:e.options?[...e.options].map(o=>o.label):undefined}))
      .filter(c=>c.visible && c.name)};
}"""


class PlaywrightDriver:
    """A real browser through Playwright. Installed Chrome if present, else Chromium."""

    CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    def __init__(self, headless=True):
        self.headless, self.page = headless, None

    async def _start(self):
        if self.page is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as error:
            raise RuntimeError(
                'Install the browser extra: pip install "thewariend-musclememory[browser]"'
            ) from error
        self._playwright = await async_playwright().start()
        chrome = self.CHROME if Path(self.CHROME).exists() else None
        browser = await self._playwright.chromium.launch(
            executable_path=chrome, headless=self.headless
        )
        self.page = await browser.new_page()

    async def open(self, url):
        await self._start()
        await self.page.goto(url)

    async def snapshot(self):
        await self._start()
        return await self.page.evaluate(SNAPSHOT)

    async def act(self, index, action, value):
        element = self.page.locator("input,select,textarea,button,a").nth(index)
        if action == "fill":
            await element.fill(value)
        elif action == "select":
            await element.select_option(label=value)
        else:
            await element.click()
        await self.page.wait_for_timeout(100)


def sensitive(name):
    """Passwords, codes and keys: never recorded, never learned, always typed by the agent."""
    return bool(SENSITIVE_KEY.search(slug(name)) or re.search(r"\b(pin|cvv|cvc|otp)\b", name, re.I))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60] or "input"


def page_url(identity):
    scheme, host, port, path = identity
    default = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    return f"{scheme}://{host}{'' if default else f':{port}'}{path}"


class BrowserMemory:
    """One agent's browser and its current task. All tools go through this object."""

    def __init__(self, client, namespace, driver, *, state_dir=None, skills_enabled=True):
        self.skills_enabled = skills_enabled
        self.client, self.namespace, self.driver = client, namespace, driver
        self.state_dir = Path(state_dir) if state_dir else None
        self.run = self.skills = self.used = None
        self.state, self.refs, self.epoch = None, {}, 0

    # Page reading and actions ---------------------------------------------------------

    async def snapshot(self):
        raw = await self.driver.snapshot()
        self.epoch += 1
        self.refs, controls, shown = {}, [], []
        for item in raw["controls"]:
            ref = f"{self.epoch}:{item['index']}"
            self.refs[ref] = item
            controls.append(
                {
                    "ref": ref,
                    "role": item["role"],
                    "name": item["name"][:300],
                    "landmarks": item["landmarks"][-12:],
                    "value": item["value"][:2000],
                    "enabled": item["enabled"],
                }
            )
            if sensitive(item["name"]) and controls[-1]["value"] and item["role"] == "textbox":
                controls[-1]["value"] = FILLED  # only that it's filled, never the secret
            entry = {k: v for k, v in controls[-1].items() if v not in ("", True)}
            entry["section"] = " > ".join(entry.pop("landmarks", []))
            if item.get("options"):
                entry["options"] = item["options"][:40]
            shown.append(entry)
        self.state = BrowserState(url=raw["url"], controls=controls[:256], notices=raw["notices"])
        page = {
            "url": raw["url"],
            "notices": raw["notices"],
            "text": raw["text"],
            "controls": shown,
        }
        hint = self._skill_hint()
        if hint:
            page["skill"] = hint
        return page

    def _skill_hint(self):
        if not self.skills_enabled:
            return None
        try:
            here = page_identity(self.state.url)
        except ValueError:
            return None
        for skill in self.skills or []:
            if skill["start"] == here and not self.used:
                return (
                    f"Proven skill '{skill['title']}' starts on this page. Call "
                    f"musclememory_run_skill with skill='{skill['skill']}' and its inputs."
                )
        return None

    async def act(self, ref, action, value=""):
        if action not in {"fill", "select", "click"}:
            raise ValueError("action must be fill, select or click")
        if ref not in self.refs or not self.refs[ref]["enabled"]:
            return {"error": "That ref is stale or disabled. Read the page again."}
        control = self.refs[ref]
        target = {k: control[k] for k in ("role", "name", "landmarks")}
        target["landmarks"] = target["landmarks"][-12:]
        before = self.state
        try:
            await self.driver.act(control["index"], action, value)
        except Exception as error:
            page = await self.snapshot()
            page["error"] = f"{action} failed: {str(error).splitlines()[0][:300]}"
            return page
        page = await self.snapshot()
        if self.run:
            # A secret field is recorded as a step, but only ever with a hidden value: the
            # skill knows the field exists and leaves it to the agent every time.
            hidden = action == "fill" and sensitive(control["name"]) and value
            recorded = None if action == "click" else (FILLED if hidden else value)
            try:
                capture_browser_context(action, target, before, self.state, value=recorded)
            except ValueError:
                recorded = False  # nothing observable changed; keep browsing, learn nothing
            if recorded is not False:
                await asyncio.to_thread(
                    record_browser_action, self.run, action, target, before, self.state,
                    value=recorded,
                )  # fmt: skip
        return page

    # Tasks ----------------------------------------------------------------------------

    async def start(self, task):
        self.run = ObservedRun(self.client, task, PROFILE, self.namespace)
        self.task, self.used, self.skills = task, None, []
        try:
            await asyncio.to_thread(self.run.flush)
            if not self.skills_enabled:
                return
            found = await asyncio.to_thread(
                self.client.musclememory_recall,
                intent_text=task,
                runtime_profile=PROFILE,
                visibility_scope=self.namespace,
                match_limit=3,
                observation_run=self.run.run_token,
            )
        except Exception:
            found = {}
        for candidate in found.get("candidates", []):
            definition = candidate["definition"]
            steps = [
                s["arguments"][BROWSER_CONTEXT]
                for s in definition["steps"]
                if isinstance(s["arguments"].get(BROWSER_CONTEXT), dict)
            ]
            if not steps:
                continue
            self.skills.append(
                {
                    "skill": candidate["procedure_token"],
                    "version": candidate["procedure_version"],
                    "title": definition["title"][:160],
                    "start": steps[0]["page"],
                    "inputs": skill_inputs(definition),
                    "definition": definition,
                }
            )

    def find_skills(self):
        return {
            "skills": [
                {
                    "skill": s["skill"],
                    "title": s["title"],
                    "start_page": page_url(s["start"]),
                    "inputs": [
                        {"name": i["input"], "field": i["field"], "section": i["section"]}
                        for i in s["inputs"]
                    ],
                }
                for s in self.skills
            ],
            "how": "Open start_page, then call musclememory_run_skill with every input. "
            "It never submits; you do.",
        }

    async def run_skill(self, skill, values):
        chosen = next((s for s in self.skills if s["skill"] == skill), None)
        if not chosen:
            return {"error": "Unknown skill. Use musclememory_find_skills for this task."}
        self.used = chosen
        loop = asyncio.get_running_loop()

        def observe():
            asyncio.run_coroutine_threadsafe(self.snapshot(), loop).result()
            return self.state

        def act(action, ref, value):
            asyncio.run_coroutine_threadsafe(self.act(ref, action, value or ""), loop).result()

        values = {k: str(v) for k, v in values.items()}
        result = await asyncio.to_thread(carry_out, chosen["definition"], values, observe, act)
        await self.snapshot()
        return result

    @staticmethod
    def _shows_result(context):
        """The final click visibly changed the site: a new message, or a new page."""
        post = (context or {}).get("postcondition", {})
        return (
            (context or {}).get("action") == "click"
            and post.get("kind") == "state"
            and bool(post.get("notices") or post.get("page") != context.get("page"))
        )

    def _same_result(self, run, definition):
        """The final click is the skill's own submit and produced its result again."""
        final = self._final_of(run)
        steps = [s["arguments"][BROWSER_CONTEXT] for s in definition["steps"]
                 if isinstance(s["arguments"].get(BROWSER_CONTEXT), dict)]  # fmt: skip
        if not steps or not final:
            return False
        proven, post = steps[-1], final["postcondition"]
        expected = proven["postcondition"]
        return (
            final["target"] == proven["target"]
            and post["page"] == expected["page"]
            and set(post.get("notices", [])) >= set(expected.get("notices", []))
        )

    async def done(self, succeeded):
        if not self.run:
            return {"error": "No task is in progress."}
        run, used = self.run, self.used
        self.run = None
        confirmed = succeeded and self._shows_result(self._final_of(run))
        if used:
            confirmed = confirmed and self._same_result(run, used["definition"])
        run_check = run.instrument("browser.verify")(lambda: bool(confirmed))
        await asyncio.to_thread(run_check)
        evidence = run.events[-1]["event_id"]
        try:
            await asyncio.to_thread(run.complete, bool(confirmed))
            report = {
                "succeeded": bool(confirmed),
                "procedure_version": None,
                "evidence_event_ids": [evidence] if confirmed else [],
            }
            metrics = {"observed": run.measurements()}
            if used:
                result = await asyncio.to_thread(
                    self._evaluate, run, used["skill"], used["version"], report, metrics
                )
                return {"checked": used["skill"], "result": result}
            if not confirmed:
                reason = "the site showed no result" if succeeded else "the task failed"
                return {"learned": False, "reason": f"Nothing learned: {reason}."}
            for token, version in self._known(run):
                try:
                    result = await asyncio.to_thread(
                        self._evaluate, run, token, version, report, metrics
                    )
                    return {"checked": token, "result": result}
                except Exception:
                    continue  # a different skill; try the next, or learn a new one
            learned = await asyncio.to_thread(self._learn, run, evidence)
            return {"learned": learned["procedure_token"], "needs_checks": True}
        except Exception as error:
            # Tell the agent why, instead of failing the tool: the task itself is finished.
            detail = getattr(getattr(error, "response", None), "text", "") or str(error)
            return {
                "learned": False,
                "reason": f"MuscleMemory could not record this: {detail[:300]}",
            }
        finally:
            run.close()

    @staticmethod
    def _final_of(run):
        acts = [e for e in run.events if e.get("capability") == "browser.act"]
        return acts[-1]["arguments"].get(BROWSER_CONTEXT) if acts else None

    def _evaluate(self, run, token, version, report, metrics):
        if version is None:
            version = self.client.musclememory_inspect(procedure_token=token)["procedure_version"]
        return self.client.musclememory_evaluate(
            procedure_token=token,
            run_token=run.run_token,
            outcome_report={**report, "procedure_version": version},
            performance_metrics=metrics,
        )

    def _learn(self, run, evidence):
        parameters, seen = {}, set()
        for event in run.events:
            context = event["arguments"].get(BROWSER_CONTEXT) if event["kind"] == "tool" else None
            value = event["arguments"].get("value") if context else None
            if not context or context["action"] not in {"fill", "select"} or value in {"", FILLED}:
                continue
            if value in seen or len(parameters) >= 64:
                continue  # the same value twice is one input (an email and its confirmation)
            seen.add(value)
            target = context["target"]
            name = slug(".".join([*target["landmarks"][-1:], target["name"]]))
            while name in parameters:
                name += "_2"
            parameters[name] = {"definition": {"kind": "string"}, "observed_value": value}
        learned = self.client.musclememory_learn(
            run_token=run.run_token,
            completion_proof={
                "succeeded": True,
                "evidence_event_ids": [evidence],
                "summary": "The site showed a result after the final action",
            },
            learning_policy={"title": self.task[:200], "parameters": parameters},
        )
        self._remember(run, learned["procedure_token"])
        return learned

    def _key(self, run):
        contexts = [e["arguments"][BROWSER_CONTEXT] for e in run.events
                    if e.get("capability") == "browser.act"
                    and isinstance(e["arguments"].get(BROWSER_CONTEXT), dict)]  # fmt: skip
        if not contexts:
            return None
        return json.dumps([contexts[0]["page"], contexts[-1]["target"]], sort_keys=True)

    def _index(self):
        path = self.state_dir / "browser-skills.json" if self.state_dir else None
        return path, (json.loads(path.read_text()) if path and path.exists() else {})

    def _remember(self, run, token):
        path, index = self._index()
        if path:
            key = self._key(run)
            index.setdefault(key, [])
            if token not in index[key]:
                index[key].append(token)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(index))

    def _known(self, run):
        """Skills this server learned for the same start page and final button."""
        _, index = self._index()
        return [(token, None) for token in index.get(self._key(run), [])]


async def log_browser_call(tool, arguments, memory, path, execute):
    """Count the public invocation before schema validation, including failed calls.

    Skill internals are not extra MCP calls. Resolve a field name before acting,
    since refs expire on the next page read. Never log errors that may echo inputs.
    """

    def redact(value):
        if isinstance(value, dict):
            return {k: FILLED if sensitive(k) else redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [redact(v) for v in value]
        return value

    arguments = redact(arguments)
    if tool == "browser_act" and arguments.get("action") == "fill":
        ref = arguments.get("ref")
        control = memory.refs.get(ref, {}) if isinstance(ref, str) else {}
        if sensitive(control.get("name", "")) or not control:
            arguments["value"] = FILLED
    started, error = time.monotonic(), None
    try:
        result = await execute()
        payload = (
            result if isinstance(result, dict) else getattr(result, "structured_content", None)
        )
        if payload is None:
            for block in getattr(result, "content", []):
                if getattr(block, "type", None) == "text":
                    try:
                        payload = json.loads(block.text)
                    except ValueError:
                        continue
                    if isinstance(payload, dict) and payload.get("error"):
                        break
        if getattr(result, "is_error", False) or (
            isinstance(payload, dict) and payload.get("error")
        ):
            error = "tool_error"
        return result
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        with Path(path).open("a", encoding="utf-8") as log:
            log.write(
                json.dumps(
                    {
                        "tool": tool,
                        "arguments": arguments,
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                        "error": error,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def build_browser_mcp(memory, *, log_path=None):
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    class BrowserServer(MCPServer):
        async def call_tool(self, name, arguments, context=None):
            async def execute():
                return await super(BrowserServer, self).call_tool(name, arguments, context)

            if log_path:
                return await log_browser_call(name, arguments, memory, log_path, execute)
            return await execute()

    server = BrowserServer(
        "musclememory-browser",
        instructions=(
            "A browser that learns. Start each task with browser_open(url, task). "
            + (
                "Before doing a task by hand, call musclememory_find_skills: "
                "a proven skill does the steps for you. "
                if memory.skills_enabled
                else ""
            )
            + "Always finish with browser_done."
        ),
        log_level="CRITICAL",
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

    def tool(*, annotations):
        def register(function):
            if not memory.skills_enabled and function.__name__.startswith("musclememory_"):
                return function
            return server.tool(annotations=annotations)(function)

        return register

    @tool(annotations=write)
    async def browser_open(url: str, task: str = "") -> dict:
        """Open a page. The first call of a task needs `task`: one line saying what you're doing."""
        if urlsplit(url).scheme not in {"http", "https"}:
            return {"error": "Only http and https pages can be opened."}
        if memory.run is None:
            if not task.strip():
                return {"error": "Pass task: one line saying what you're doing on this site."}
            await memory.start(task.strip())
        await memory.driver.open(url)
        page = await memory.snapshot()
        if memory.run:
            await asyncio.to_thread(
                memory.run.record_tool_result, "browser.navigate", {"url": url},
                {"opened": memory.state.url}, outcome_verified=True,
            )  # fmt: skip
        return page

    @tool(annotations=read)
    async def browser_read() -> dict:
        """Read the current page again without reloading it. Refs change on every read."""
        return await memory.snapshot()

    @tool(annotations=write)
    async def browser_act(ref: str, action: str, value: str = "") -> dict:
        """Act on one control by its current ref: fill text, select an option by label, or click."""
        return await memory.act(ref, action, value)

    @tool(annotations=read)
    async def musclememory_find_skills() -> dict:
        """Proven skills for the current task, with the inputs each one needs."""
        if memory.run is None:
            return {"error": "Start a task with browser_open(url, task) first."}
        return memory.find_skills()

    @tool(annotations=write)
    async def musclememory_run_skill(skill: str, inputs: dict[str, str]) -> dict:
        """Carry out a proven skill on the open page with checks. It never submits; you do.

        Pass every input by name. If it leaves fields for you, fill them and call it again.
        """
        if memory.run is None:
            return {"error": "Start a task with browser_open(url, task) first."}
        return await memory.run_skill(skill, inputs)

    @tool(annotations=write)
    async def browser_done(succeeded: bool) -> dict:
        """Finish the task. Say whether it succeeded; MuscleMemory also checks the site's result."""
        return await memory.done(succeeded)

    return server


def serve(config_path, *, headless=True, no_skills=False, log_path=None):
    from .client_hooks import load_config

    config, _, _, client = load_config(config_path, "codex")
    client.http.timeout = 30
    memory = BrowserMemory(
        client,
        config.get("namespace", "default"),
        PlaywrightDriver(headless=headless),
        state_dir=config.get("state_dir"),
        skills_enabled=not no_skills,
    )
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    build_browser_mcp(memory, log_path=log_path).run(transport="stdio")
