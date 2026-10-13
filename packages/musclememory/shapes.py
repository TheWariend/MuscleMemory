"""Value-free action shapes used to verify evaluations and compare failed runs.

Shapes keep key names and JSON types only, never argument values, so failure
contrasts and evaluation checks cannot leak captured data.
"""

import hashlib
import json
from collections import Counter, defaultdict

from .artifacts import operation_categories
from .core import DomainError
from .security import MASK, SENSITIVE_KEY, redact


def safe_name(value):
    if redact(value) != value or MASK in value or "$mm_capture_omitted" in value:
        raise DomainError("secret_dependency", "State features contain redacted identifiers")
    return value


def argument_shape(value, parameters=None, path="$", depth=0):
    """Only key names and JSON types; no scalar value, array length, or hash of it."""
    if depth > 20:
        raise DomainError("payload_depth", "State shape nesting exceeds 20 levels", 422)
    if isinstance(value, str) and (MASK in value or "$mm_capture_omitted" in value):
        raise DomainError("secret_dependency", "State features need complete, non-secret arguments")
    if isinstance(value, dict):
        if "$mm_capture_omitted" in value:
            raise DomainError(
                "incomplete_arguments", "State features need fully captured arguments"
            )
        if set(value) == {"$mm_parameter"} and parameters:
            spec = parameters[value["$mm_parameter"]]
            kind = spec.kind if hasattr(spec, "kind") else spec["kind"]
            return [f"{path}:{kind}"]
        result = [f"{path}:object"]
        for key in sorted(value):
            safe_name(key)
            if SENSITIVE_KEY.search(key):
                raise DomainError("secret_dependency", "Secret keys cannot enter a state index")
            # JSON Pointer escaping keeps keys containing '/' or '~' distinct.
            child = key.replace("~", "~0").replace("/", "~1")
            result.extend(argument_shape(value[key], parameters, f"{path}/{child}", depth + 1))
        return sorted(set(result))
    if isinstance(value, list):
        return sorted(
            {
                f"{path}:array",
                *(
                    feature
                    for item in value
                    for feature in argument_shape(item, parameters, f"{path}/*", depth + 1)
                ),
            }
        )
    kind = (
        "null"
        if value is None
        else "boolean"
        if isinstance(value, bool)
        else "integer"
        if isinstance(value, int)
        else "number"
        if isinstance(value, float)
        else "string"
    )
    return [f"{path}:{kind}"]


def descriptor(capability, tool, arguments, effect, permission, parameters=None):
    return {
        "capability": safe_name(capability),
        "tool": safe_name(tool),
        "argument_shape": argument_shape(arguments, parameters),
        "effect": effect,
        "permission": safe_name(permission) if permission else None,
        "operations": operation_categories(arguments, capability),
    }


def action_shape(event):
    """Value-free description of one tool call: capability, tool and argument shape."""
    return descriptor(
        event.capability, event.tool_name, event.arguments, event.effect, event.permission
    )


def shape_matches(template, arguments, parameters):
    expected = set(argument_shape(template, parameters))
    actual = set(argument_shape(arguments))
    containers = []

    def collect(value, path="$", depth=0):
        if depth > 20:
            return
        if isinstance(value, dict):
            if set(value) == {"$mm_parameter"}:
                specification = parameters[value["$mm_parameter"]]
                kind = (
                    specification.kind if hasattr(specification, "kind") else specification["kind"]
                )
                if kind in {"object", "array"}:
                    containers.append(path + "/")
            else:
                for key, child in value.items():
                    collect(
                        child, path + "/" + key.replace("~", "~0").replace("/", "~1"), depth + 1
                    )
        elif isinstance(value, list):
            for child in value:
                collect(child, path + "/*", depth + 1)

    collect(template)
    return expected.issubset(actual) and all(
        any(feature.startswith(prefix) for prefix in containers) for feature in actual - expected
    )


def capability_signature(events):
    return [event.capability for event in events if event.kind == "tool"]


def signature_key(capabilities):
    return hashlib.sha256(json.dumps(capabilities, separators=(",", ":")).encode()).hexdigest()


# Changing a file is one ability, whatever an agent calls its tool: Codex patches,
# Claude Code edits and writes, Cursor writes. Skills learned by one agent stay usable
# by another. Every other capability must be bound exactly.
FILE_CHANGE = ("filesystem.edit", "filesystem.patch", "filesystem.write")


def bound_capability(capability, bindings):
    """The agent's own capability that provides ``capability``, or None."""
    if capability in bindings:
        return capability
    if capability in FILE_CHANGE:
        return next((other for other in FILE_CHANGE if other in bindings), None)
    return None


def conditions_satisfied(conditions, profile, prefix):
    for condition in conditions:
        if not isinstance(condition, str) or ":" not in condition:
            return False
        kind, value = condition.split(":", 1)
        if kind == "capability":
            satisfied = bound_capability(value, profile.bindings) is not None
        elif kind == "permission":
            satisfied = value in profile.permissions
        elif kind == "environment":
            satisfied = value in profile.environment and MASK not in profile.environment[value]
        elif kind == "state-capability":
            satisfied = any(event.capability == value for event in prefix)
        else:
            # Free-form applicability prose cannot be safely treated as satisfied.
            return False
        if not satisfied:
            return False
    return True


def action_identity(state):
    return json.dumps({key: value for key, value in state.items() if key != "tool"}, sort_keys=True)


def contrast_diff(failed_events, successful_events):
    """Compare actions/shapes, excluding actual values, aliases and success labels."""

    def actions(events):
        output = []
        for event in events:
            if event.kind != "tool":
                continue
            output.append({"step_token": event.event_id, **action_shape(event)})
        return output

    failed, succeeded = actions(failed_events), actions(successful_events)

    def identities(items):
        return Counter(
            action_identity({key: value for key, value in item.items() if key != "step_token"})
            for item in items
        )

    failed_keys, success_keys = identities(failed), identities(succeeded)

    def unmatched(items, other):
        remaining, output = other.copy(), []
        for item in items:
            identity = action_identity(
                {key: value for key, value in item.items() if key != "step_token"}
            )
            if remaining[identity]:
                remaining[identity] -= 1
            else:
                output.append(item)
        return output

    only_success, only_failure = unmatched(succeeded, failed_keys), unmatched(failed, success_keys)
    groups = defaultdict(list)
    for item in failed:
        groups[item["capability"]].append(item)
    differences, positions = [], defaultdict(int)
    for item in succeeded:
        cap, position = item["capability"], positions[item["capability"]]
        positions[cap] += 1
        if position < len(groups[cap]):
            prior = groups[cap][position]
            if prior["argument_shape"] != item["argument_shape"]:
                differences.append(
                    {
                        "capability": cap,
                        "occurrence": position,
                        "failed_shape": prior["argument_shape"],
                        "successful_shape": item["argument_shape"],
                    }
                )
    return {
        "only_success": only_success,
        "only_failure": only_failure,
        "argument_shape_differences": differences,
    }
