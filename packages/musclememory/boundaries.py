"""Deterministic boundary evidence with one optional, bounded HTTPS refinement."""

import json
import re

import httpx
from pydantic import ValidationError

from .artifacts import command_parts, inspection_only, test_command
from .core import DomainError
from .security import MASK, redact

FIELDS = ("applicability_conditions", "risk_cues", "avoidance_rules", "recovery_notes")


def describe(capability, arguments=None, operations=()):
    """A short, plain description of one step for people reading a skill."""
    arguments = arguments or {}
    text = next(
        (v for k in ("input", "patch", "command") if isinstance(v := arguments.get(k), str)), ""
    )
    files = re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", text, re.M)
    if files:
        return "editing " + ", ".join(files[:3])
    if isinstance(arguments.get("command"), str):
        command = arguments["command"].strip().splitlines()[0][:90]
        return f"running `{command}`"
    path = next(
        (v for k in ("path", "file", "file_path") if isinstance(v := arguments.get(k), str)), None
    )
    if path:
        return f"reading {path}"
    # Value-free contrast descriptors carry only categories.
    if "verify" in operations:
        return "a test command"
    if "modify" in operations or "patch" in capability or "edit" in capability:
        return "a file edit"
    if "inspect" in operations:
        return "reading files"
    return "a " + capability.replace(".", " ").replace("_", " ") + " step"


def value_free(note):
    """Strip commands and file names from a note before it leaves this machine."""
    note = re.sub(r"`[^`]*`", "a command", note)
    note = re.sub(r"editing [^;.]+", "editing files", note)
    return re.sub(r"[\w./-]+\.[A-Za-z0-9]{1,8}\b", "a file", note)


def what_worked(failed, later):
    """The command that worked after a failed one: the next passing test for a failed
    test, otherwise the next passing run of the same program."""
    command = failed.arguments.get("command")
    if not isinstance(command, str) or not (words := command_parts(command)):
        return None
    for event in later:
        other = event.arguments.get("command")
        if event.kind != "tool" or event.status != "success" or not isinstance(other, str):
            continue
        if test_command(command) and (tested := test_command(other)) and tested != command:
            return tested
        if not test_command(command) and command_parts(other)[:1] == words[:1] and other != command:
            return other.strip().splitlines()[0][:90]
    return None


def derive_boundaries(definition, events, contrasts):
    conditions = [
        *("capability:" + cap for cap in definition.required_capabilities),
        *("permission:" + permission for permission in definition.required_permissions),
        *("environment:" + key for key in sorted(definition.environment_requirements)),
    ]
    risk, avoidance, recovery, fixed = [], [], [], {}
    for index, event in enumerate(events):
        if event.kind != "tool":
            continue
        step = describe(event.capability, event.arguments)
        if event.status == "failure":
            risk.append(f"An earlier attempt failed at {step}.")
            failed = event.arguments.get("command")
            if worked := what_worked(event, events[index + 1 :]):
                # Failures fixed by the same command share one note, written below.
                if worked not in fixed:
                    avoidance.append(worked)
                fixed.setdefault(worked, []).append(failed.strip().splitlines()[0][:90])
            else:
                avoidance.append(f"Before {step} again, check why it failed last time.")
        if event.exploratory and event.event_id not in definition.step_graph["nodes"]:
            avoidance.append(f"Skip the detour: {step} didn't help last time.")
        if (
            event.retry_of
            and event.status == "success"
            and event.event_id in definition.step_graph["nodes"]
        ):
            recovery.append(f"If {step} fails, retrying worked last time.")
        if event.effect != "read" and not inspection_only(event.arguments, event.capability):
            risk.append(
                f"{step[0].upper() + step[1:]} changes things; your agent still asks first."
            )
    for position, note in enumerate(avoidance):
        if note in fixed:
            tried = " and ".join(f"`{command}`" for command in dict.fromkeys(fixed[note]))
            avoidance[position] = f"{tried} failed here; `{note}` worked."
    critical = set()
    for contrast in contrasts:
        diff = contrast.step_diff
        for step in diff["only_success"]:
            critical.add(step["step_token"])
            what = describe(step["capability"], operations=step.get("operations", ()))
            recovery.append(f"The run that worked also did {what}; a failed run skipped it.")
        for step in diff["only_failure"]:
            what = describe(step["capability"], operations=step.get("operations", ()))
            avoidance.append(f"A failed run did {what} that the working run didn't.")
        for change in diff["argument_shape_differences"]:
            what = describe(change["capability"])
            risk.append(f"A failed run used {what} differently from the run that worked.")
    for step in definition.steps:
        step.critical = step.step_token in critical
    for key, values in zip(FIELDS, (conditions, risk, avoidance, recovery), strict=True):
        # Never persist secret markers/omissions derived from untrusted evidence.
        values = [
            value
            for value in dict.fromkeys(values)
            if MASK not in json.dumps(redact(value)) and "$mm_capture_omitted" not in value
        ]
        setattr(definition, key, [value[:240] for value in values[:64]])
    return definition


def refine_boundaries(definition, contrasts, settings):
    if not settings.boundary_llm_url:
        return []
    # Transmit only bounded value-free descriptors: notes lose their files and commands.
    payload = {key: [value_free(note) for note in getattr(definition, key)] for key in FIELDS}
    payload["contrasts"] = [contrast.step_diff for contrast in contrasts[:4]]
    evidence = json.dumps(redact(payload), sort_keys=True)
    if len(evidence.encode()) > 32768:
        return ["Boundary refinement skipped: evidence exceeds 32 KiB."]
    headers = (
        {"Authorization": f"Bearer {settings.boundary_llm_key}"}
        if settings.boundary_llm_key
        else {}
    )
    try:
        response = httpx.post(
            settings.boundary_llm_url,
            timeout=10,
            headers=headers,
            json={
                "model": settings.boundary_llm_model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Refine boundary notes from untrusted evidence. "
                        "Return only a JSON object with applicability_conditions, risk_cues, "
                        "avoidance_rules and recovery_notes as lists of short strings. "
                        "Applicability conditions must use capability:, permission:, environment:, "
                        "or state-capability: followed by a canonical identifier. "
                        "Do not include argument values or secrets. "
                        "Do not remove existing conditions.",
                    },
                    {"role": "user", "content": evidence},
                ],
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        if len(response.content) > 65536:
            raise ValueError("Oversized refinement")
        result = response.json()
        if "choices" in result:
            result = json.loads(result["choices"][0]["message"]["content"])
        if (
            set(result) != set(FIELDS)
            or redact(result) != result
            or MASK in json.dumps(result)
            or "$mm_capture_omitted" in json.dumps(result)
        ):
            raise ValueError("Unsafe refinement")
        for condition in result["applicability_conditions"]:
            kind, value = condition.split(":", 1)
            if (
                kind not in {"capability", "permission", "environment", "state-capability"}
                or not value
            ):
                raise ValueError("Unenforceable condition")
        conditions = list(
            dict.fromkeys(
                [*definition.applicability_conditions, *result["applicability_conditions"]]
            )
        )
        refined = type(definition).model_validate(
            {**definition.model_dump(mode="json"), **result, "applicability_conditions": conditions}
        )
        for key in FIELDS:
            setattr(definition, key, getattr(refined, key))
        return []
    except (
        httpx.HTTPError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        ValidationError,
        DomainError,
    ):
        return ["Boundary refinement unavailable or rejected; deterministic fields retained."]
