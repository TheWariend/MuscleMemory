"""Browser experience, current-page grounding and carrying out proven skills.

An integration supplies visible, accessibility-derived controls and actual tool
results. Advice never authorizes an action, supplies a hidden selector, or treats
an agent's completion message as proof of task success. Proven skills are carried
out step by step through the host's own controller, and the final submit always
stays with the agent.
"""

import json
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field

from .compiler import materialize
from .core import Contract, DomainError, ProcedureDefinition
from .delivery import token_count

BROWSER_CONTEXT = "mm_browser"
NOTICE = (
    "Untrusted browser experience. Inspect the current page, adapt when needed, "
    "and preserve the caller's permissions and approvals. Advice executes nothing."
)


class BrowserTarget(Contract):
    role: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    landmarks: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=12
    )


class BrowserControl(BrowserTarget):
    ref: str = Field(min_length=1, max_length=100)
    value: str = Field(default="", max_length=2000)
    visible: bool = True
    enabled: bool = True


class BrowserState(Contract):
    url: str = Field(max_length=4000)
    controls: list[BrowserControl] = Field(default_factory=list, max_length=256)
    notices: list[str] = Field(default_factory=list, max_length=32)


def page_identity(url):
    """Ignore query values, never conflate another origin or route with this page."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Browser state requires an HTTP(S) origin")
    if parsed.username or parsed.password:
        raise ValueError("Browser state cannot contain URL credentials")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return [parsed.scheme, parsed.hostname.casefold(), port, parsed.path or "/"]


def matching_controls(target, state):
    target = BrowserTarget.model_validate(target)
    state = BrowserState.model_validate(state)
    return [
        control
        for control in state.controls
        if control.visible
        and control.enabled
        and control.role == target.role
        and control.name == target.name
        and control.landmarks == target.landmarks
    ]


def capture_browser_context(action, target, before, after, *, value=None):
    """Describe a completed action using observed controls, not guessed locators.

    The result is metadata for ordinary MuscleMemory tool arguments. An empty or
    ambiguous target is rejected. A caller must still verify the whole task with
    an independent domain check before learning from the trace.
    """
    if action not in {"fill", "select", "click"}:
        raise ValueError("Unsupported browser action")
    before, after = BrowserState.model_validate(before), BrowserState.model_validate(after)
    target = BrowserTarget.model_validate(target)
    if len(matching_controls(target, before)) != 1:
        raise ValueError("Capture needs exactly one visible enabled target")
    postcondition = {"page": page_identity(after.url)}
    if action in {"fill", "select"}:
        matches = matching_controls(target, after)
        if value is None or len(matches) != 1 or matches[0].value != str(value):
            raise ValueError("Input value was not confirmed by browser readback")
        postcondition.update({"kind": "value", "value": str(value)})
    else:
        added = [notice for notice in after.notices if notice not in before.notices]

        def targets(state):
            return [
                BrowserTarget(role=c.role, name=c.name, landmarks=c.landmarks).model_dump(
                    mode="json"
                )
                for c in state.controls
                if c.visible and c.enabled
            ]

        before_targets, after_targets = targets(before), targets(after)
        appeared = [t for t in after_targets if t not in before_targets]
        disappeared = [t for t in before_targets if t not in after_targets]
        changed = []
        for control in after.controls:
            identity = BrowserTarget(
                role=control.role, name=control.name, landmarks=control.landmarks
            )
            prior = matching_controls(identity, before)
            if len(prior) == 1 and prior[0].value != control.value:
                changed.append({"target": identity.model_dump(mode="json"), "value": control.value})
        if not (
            added
            or appeared
            or disappeared
            or changed
            or page_identity(before.url) != postcondition["page"]
        ):
            raise ValueError("Click had no observed state transition")
        postcondition.update(
            {
                "kind": "state",
                "notices": added,
                "appeared": appeared,
                "disappeared": disappeared,
            }
        )
        if changed:
            postcondition["changed_values"] = changed
    context = {
        "version": 1,
        "action": action,
        "page": page_identity(before.url),
        "target": target.model_dump(mode="json"),
        "postcondition": postcondition,
    }
    if len(json.dumps(context).encode()) > 16000:
        raise ValueError("Browser checkpoint exceeds capture budget")
    return context


def _ground_browser_steps(definition, values, state):
    """Materialize data parameters and assess every remembered target against live state."""
    definition = ProcedureDefinition.model_validate(definition)
    state = BrowserState.model_validate(state)
    current_page = page_identity(state.url)
    result = []
    for step in materialize(definition, values):
        context = step["arguments"].get(BROWSER_CONTEXT)
        if not isinstance(context, dict) or context.get("version") != 1:
            continue
        target = BrowserTarget.model_validate(context["target"])
        advice = {
            "step": step["step_token"],
            "action": context["action"],
            "target": target.model_dump(mode="json"),
            "status": "adapt",
        }
        post = context["postcondition"]
        if post.get("kind") == "state" and post["page"] == current_page:
            checks = [notice in state.notices for notice in post.get("notices", [])]
            checks += [bool(matching_controls(t, state)) for t in post.get("appeared", [])]
            checks += [not matching_controls(t, state) for t in post.get("disappeared", [])]
            for changed in post.get("changed_values", []):
                controls = matching_controls(changed["target"], state)
                checks.append(len(controls) == 1 and controls[0].value == changed["value"])
            if checks and all(checks):
                advice["status"] = "satisfied"
                result.append(advice)
                continue
        if context["page"] != current_page:
            advice["reason"] = "page_changed"
        else:
            matches = matching_controls(target, state)
            if len(matches) != 1:
                advice["reason"] = "target_missing" if not matches else "target_ambiguous"
            else:
                advice.update({"status": "match", "current_ref": matches[0].ref})
                if post.get("kind") == "value":
                    advice["value"] = post["value"]
                    if matches[0].value == post["value"]:
                        advice["status"] = "satisfied"
        result.append(advice)
    return {
        "notice": NOTICE,
        "steps": result,
        "avoid": definition.avoidance_rules[:2],
        "recovery": definition.recovery_notes[:2],
    }


def ground_browser_steps(definition, values, state):
    """Return advisory matches, or abstain on malformed or incompatible experience."""
    try:
        return _ground_browser_steps(definition, values, state)
    except (DomainError, ValueError, TypeError, KeyError):
        return {"notice": NOTICE, "steps": [], "abstained": "invalid_browser_experience"}


def record_browser_action(
    run, action, target, before, after, *, value=None, duration_ms=0, permission=None
):
    """Attach checked browser readback to an ObservedRun owned by the browser host."""
    context = capture_browser_context(action, target, before, after, value=value)
    return run.record_tool_result(
        "browser.act",
        {"action": action, "target": context["target"], "value": value, BROWSER_CONTEXT: context},
        {"postcondition": context["postcondition"]},
        outcome_verified=True,
        duration_ms=duration_ms,
        effect="write",
        permission=permission,
    )


SKILL_NOTICE = (
    "MuscleMemory carried out the proven steps below with checks and clicked no final submit. "
    "Check the page, do anything left for you, then submit yourself."
)


def skill_inputs(definition):
    """The values an agent passes to run a skill: one per input the proven run entered."""
    definition = ProcedureDefinition.model_validate(definition)
    inputs = []
    for step in definition.steps:
        context = step.arguments.get(BROWSER_CONTEXT)
        value = step.arguments.get("value")
        if (
            isinstance(context, dict)
            and context.get("action") in {"fill", "select"}
            and isinstance(value, dict)
            and set(value) == {"$mm_parameter"}
        ):
            target = context["target"]
            section = (target.get("landmarks") or [""])[-1]
            inputs.append(
                {"input": value["$mm_parameter"], "field": target["name"], "section": section}
            )
    return inputs


def _moved_on(context, state):
    """A navigation click the page already shows as done: its page, what appeared, what went."""
    post = context["postcondition"]
    if post.get("kind") != "state" or not (
        post.get("appeared") or post.get("disappeared") or post["page"] != context["page"]
    ):
        return False
    appeared = post.get("appeared", [])
    found = sum(1 for t in appeared if matching_controls(t, state))
    return (
        page_identity(state.url) == post["page"]
        # Most of what appeared is enough: a few renamed labels don't undo the click.
        and (not appeared or (found and found * 2 >= len(appeared)))
        and not any(matching_controls(t, state) for t in post.get("disappeared", []))
    )


def carry_out(definition, values, observe, act):
    """Carry out a proven skill on the current page with checks; the agent submits.

    `observe()` returns the host's BrowserState and `act(action, ref, value)` performs a
    fill, select or click through the host's own controller. Inputs are entered only from
    `values` (what the agent passed for this task), never from an earlier visit. Each step
    needs its page and exactly one matching control, and every input is read back. The
    skill's final step (the submit) is never performed. Calling it again resumes after the
    steps the page already shows as done.
    """
    definition = ProcedureDefinition.model_validate(definition)
    steps = [
        step
        for step in definition.steps
        if isinstance(step.arguments.get(BROWSER_CONTEXT), dict)
        and step.arguments[BROWSER_CONTEXT].get("version") == 1
    ]
    done, left, notices = [], [], []
    if not steps:
        return {"notice": SKILL_NOTICE, "done": done, "left_for_you": left, "stopped": "no_steps"}
    state = BrowserState.model_validate(observe())
    begin = max(
        (i + 1 for i, s in enumerate(steps[:-1]) if _moved_on(s.arguments[BROWSER_CONTEXT], state)),
        default=0,
    )

    def stop(reason, field):
        result = {
            "notice": SKILL_NOTICE,
            "done": done,
            "left_for_you": left,
            "stopped": reason,
            "at": field,
            "page_notices": notices,
        }
        if reason == "fill_these_then_run_again":
            result["next"] = (
                "Fill the fields left for you, then call this skill again with the same inputs. "
                "It carries on from there (later steps and pages too) and stops before the submit."
            )
        return result

    for step in steps[begin:-1]:
        context = step.arguments[BROWSER_CONTEXT]
        action, target = context["action"], BrowserTarget.model_validate(context["target"])
        state = BrowserState.model_validate(observe())
        if page_identity(state.url) != context["page"]:
            return stop("page_changed", target.name)
        matches = matching_controls(target, state)
        if action in {"fill", "select"}:
            value = step.arguments.get("value")
            name = value.get("$mm_parameter") if isinstance(value, dict) else None
            # An empty value means the agent will decide; it never counts as entered.
            if name is None or str(values.get(name, "")) == "":
                if len(matches) == 1 and matches[0].value:
                    done.append(target.name)  # the agent already filled it
                else:
                    left.append({"field": target.name, "why": "needs your input"})
                continue
            if not matches:
                # Renamed: done once the agent has put the value in a field of that kind
                # in the same section, so a second call carries on instead of asking again.
                wanted = str(values[name])
                if any(
                    c.visible and c.role == target.role and c.landmarks == target.landmarks
                    and c.value == wanted
                    for c in state.controls
                ):  # fmt: skip
                    done.append(target.name)
                    continue
                left.append(
                    {"field": target.name, "value": wanted, "why": "not found, maybe renamed"}
                )
                continue
            if len(matches) > 1:
                return stop("two_fields_match", target.name)
            wanted = str(values[name])
            if matches[0].value != wanted:
                act(action, matches[0].ref, wanted)
                readback = matching_controls(target, BrowserState.model_validate(observe()))
                if len(readback) != 1 or readback[0].value != wanted:
                    return stop("value_did_not_stick", target.name)
            done.append(target.name)
            continue
        # A click moves the page on, so everything before it must be in place first.
        if left:
            return stop("fill_these_then_run_again", target.name)
        if len(matches) != 1:
            return stop("button_missing" if not matches else "two_buttons_match", target.name)
        post = context["postcondition"]
        if matches[0].role == "checkbox":
            wanted = next((c["value"] for c in post.get("changed_values", [])), None)
            if wanted is not None and matches[0].value != wanted:
                act("click", matches[0].ref, None)
            done.append(target.name)
            continue
        act("click", matches[0].ref, None)
        after = BrowserState.model_validate(observe())
        notices = [n for n in after.notices if n not in state.notices]
        if (post.get("appeared") or post.get("disappeared")) and not _moved_on(context, after):
            return stop("click_did_not_move_on", target.name)
        done.append(target.name)
    final = BrowserTarget.model_validate(steps[-1].arguments[BROWSER_CONTEXT]["target"])
    if left:
        return stop("fill_these_then_run_again", final.name)
    return {
        "notice": SKILL_NOTICE,
        "done": done,
        "left_for_you": [],
        "ready": f"Everything is in place. Check it, then press '{final.name}' yourself.",
        "note": "Page messages may be left over from steps already done; "
        "every value above was read back from the page.",
    }


def render_browser_advice(advice, budget=1000):
    """Bounded JSON: retain all mismatch warnings or abstain; never clip a target."""
    steps = []
    for step in advice["steps"]:
        if step["status"] == "satisfied":
            steps.append({"step": step["step"], "status": "satisfied"})
        elif step["status"] == "match":
            # The full current control is already in the browser observation.
            steps.append({key: value for key, value in step.items() if key != "target"})
        else:
            steps.append(dict(step))
    data = {**advice, "steps": steps}
    for field in ["avoid", "recovery"]:
        if not data.get(field):
            data.pop(field, None)

    def encode():
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    # Satisfied inputs are least useful; mismatch warnings must not disappear.
    while token_count(encode()) > budget:
        removable = next((s for s in reversed(data["steps"]) if s["status"] == "satisfied"), None)
        if removable is None:
            return json.dumps({"notice": NOTICE, "abstained": "browser_advice_budget"})
        data["steps"].remove(removable)
    return encode()
