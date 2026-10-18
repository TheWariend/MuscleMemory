"""Deterministic compilation. No LLM execution and no implicit replay."""

import json
import re
from copy import deepcopy

from .artifacts import (
    WORKSPACE_CHANGES,
    command_parts,
    extract_experience,
    first_sentence,
    targets,
)
from .core import DomainError, ExecutionEvent, LearningPolicy, ProcedureDefinition, ProcedureStep
from .security import MASK, redact


def check_type(value, kind):
    return {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }[kind]


def parameterize(value, bindings):
    for name, binding in bindings.items():
        # Exact typed replacement avoids unsafe textual substitution into shell commands.
        if type(value) is type(binding.observed_value) and value == binding.observed_value:
            return {"$mm_parameter": name}
    if isinstance(value, dict):
        return {k: parameterize(v, bindings) for k, v in value.items()}
    if isinstance(value, list):
        return [parameterize(v, bindings) for v in value]
    return value


def materialize(definition: ProcedureDefinition, values: dict):
    if set(values) != set(definition.parameters):
        raise DomainError("parameters", "Supply exactly the declared procedure parameters", 422)
    for name, spec in definition.parameters.items():
        if not check_type(values[name], spec.kind):
            raise DomainError("parameter_type", f"Incorrect parameter type: {name}", 422)

    def replace(value):
        if isinstance(value, dict):
            if set(value) == {"$mm_parameter"}:
                return deepcopy(values[value["$mm_parameter"]])
            return {k: replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace(v) for v in value]
        return value

    steps = []
    omitted = set()
    for step in definition.steps:
        if step.condition and values[step.condition.parameter] != step.condition.equals:
            omitted.add(step.step_token)
            continue
        if omitted.intersection(step.dependencies):
            raise DomainError(
                "branch_dependency", "A required dependency was skipped by a condition"
            )
        data = step.model_dump(mode="json")
        data["arguments"] = replace(step.arguments)
        steps.append(data)
    return steps


def signature(intent):
    return " ".join(re.findall(r"[\w]+", intent.casefold()))


def compile_trace(
    intent: str,
    events: list[ExecutionEvent],
    policy: LearningPolicy,
    run_token: str,
    proof: dict,
):
    if not events or events[-1].kind != "completion" or events[-1].status != "success":
        raise DomainError("incomplete_trace", "A successful completion event must close the trace")
    for name, binding in policy.parameters.items():
        if not check_type(binding.observed_value, binding.definition.kind):
            raise DomainError("parameter_type", f"Observed value does not match parameter: {name}")
        if MASK in json.dumps(redact(binding.observed_value)):
            raise DomainError(
                "secret_parameter", "Redacted secrets cannot become procedure parameters"
            )
    by_id = {e.event_id: e for e in events}
    blocked = {e.event_id for e in events if e.kind == "tool" and e.status == "failure"}
    for event in events:
        if event.kind != "tool":
            continue
        for dependency in event.dependencies:
            dep = by_id.get(dependency)
            if not dep or dep.kind != "tool" or dep.sequence >= event.sequence:
                raise DomainError("failed_dependency", "Steps depend on unavailable evidence")
        # Dependencies are required (AND), so a failed parent makes this branch
        # unusable. Independent siblings remain; no invented serial edges.
        if blocked.intersection(event.dependencies):
            blocked.add(event.event_id)
    evidence = set(proof.get("evidence_event_ids", []))
    # A step whose result can't be confirmed (a command sent without its exit code, or
    # `tests | tail` with no summary line) is left out like a failed attempt, unless it
    # changed project files: the passing test after it confirms those changes.
    unconfirmed_checks = {
        e.event_id
        for e in events
        if e.kind == "tool"
        and not e.outcome_verified
        and e.event_id not in evidence
        and not e.arguments.get(WORKSPACE_CHANGES)
    }
    selected = {
        e.event_id
        for e in events
        if e.kind == "tool"
        and e.status == "success"
        and not e.exploratory
        and e.event_id not in blocked
        and e.event_id not in unconfirmed_checks
    }
    warnings = []
    modified = {
        path
        for event in events
        if event.kind == "tool" and event.status == "success"
        for path in targets(event.arguments, event.effect, event.capability)[1]
    }
    for event in events:
        if event.event_id not in selected or event.event_id in evidence:
            continue
        words = command_parts(event.arguments.get("command"))
        command = event.arguments.get("command", "")
        simple_shell = isinstance(command, str) and not any(char in command for char in ";|&<>`$")
        harmless_status = (
            bool(words)
            and (
                words == ["pwd"]
                or (
                    words[0] == "git"
                    and len(words) > 1
                    and words[1] in {"status", "diff", "log", "show", "ls-files"}
                )
            )
            and simple_shell
        )
        reads, writes = targets(event.arguments, event.effect, event.capability)
        discovery = (
            bool(words)
            and words[0] in {"cat", "head", "tail", "rg", "grep", "ls", "stat", "wc"}
            and simple_shell
        )
        if harmless_status or (modified and discovery and not writes and not set(reads) & modified):
            selected.remove(event.event_id)
    # Explicit dependencies below recover necessary discovery, including a
    # successful exploratory action that supplied a retained action's input.
    # Retain successful exploratory dependencies if a kept action needs them.
    pending = list(selected)
    while pending:
        event = by_id[pending.pop()]
        for dependency in event.dependencies:
            dep = by_id.get(dependency)
            if not dep or dep.kind != "tool" or dep.status != "success":
                raise DomainError(
                    "failed_dependency", "Successful steps depend on unavailable evidence"
                )
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    kept = [e for e in events if e.event_id in selected]
    if not kept:
        if blocked:
            raise DomainError("failed_dependency", "Reusable steps depend on unavailable evidence")
        raise DomainError("empty_procedure", "No reusable successful tool actions were recorded")
    if len(selected) < sum(e.kind == "tool" for e in events):
        warnings.append(
            "Failed attempts and unneeded exploration were removed; dependencies retained."
        )
    for event in kept:
        if not event.outcome_verified and not event.arguments.get(WORKSPACE_CHANGES):
            raise DomainError(
                "unverified_tool_outcome", "Tool outcomes require authoritative evidence"
            )
        if "$mm_capture_omitted" in json.dumps(event.arguments):
            raise DomainError(
                "incomplete_arguments", "A reusable step needs fully captured JSON arguments"
            )
        if MASK in json.dumps(event.arguments):
            raise DomainError(
                "secret_dependency", "A reusable step depends on redacted credentials"
            )
        if "$mm_parameter" in json.dumps(event.arguments):
            raise DomainError(
                "reserved_marker", "Trace arguments contain a reserved parameter marker"
            )
        if event.condition and event.condition.parameter not in policy.parameters:
            raise DomainError("unknown_condition", "Branch references an undeclared parameter")
    steps = [
        ProcedureStep(
            step_token=e.event_id,
            capability=e.capability,
            source_tool=e.tool_name,
            arguments=parameterize(e.arguments, policy.parameters),
            dependencies=e.dependencies,
            condition=e.condition,
            permission=e.permission,
            effect=e.effect,
            requires_approval=e.effect != "read",
        )
        for e in kept
    ]
    warnings.append(
        "Completion evidence is caller-attested; structural validation is not task verification."
    )
    return ProcedureDefinition(
        title=policy.title or first_sentence(intent),
        intent_signature=signature(intent),
        parameters={k: v.definition for k, v in policy.parameters.items()},
        preconditions=policy.preconditions,
        required_capabilities=sorted({s.capability for s in steps}),
        required_permissions=sorted({s.permission for s in steps if s.permission}),
        environment_requirements=policy.environment_requirements,
        steps=steps,
        step_graph={
            "nodes": [s.step_token for s in steps],
            "edges": [[parent, s.step_token] for s in steps for parent in s.dependencies],
        },
        expected_outcomes=policy.expected_outcomes or [proof["summary"]],
        experience=extract_experience(steps),
        provenance={
            "source_run": run_token,
            "completion_evidence": proof,
            "compiler": "deterministic-v1",
            "removed_events": len(events) - len(kept),
        },
    ), warnings
