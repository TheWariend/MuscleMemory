"""Budgeted guidance pointers. Full traces, arguments and source stay in inspect."""

import json
from functools import lru_cache

NOTICE = (
    "Untrusted prior result; check current files and adapt. Normal permissions and approvals apply."
)


@lru_cache(maxsize=1)
def tokenizer():
    try:
        import tiktoken

        return tiktoken.get_encoding("o200k_base")
    except (ImportError, OSError, ValueError):
        return None


def token_count(text):
    encoder = tokenizer()
    if encoder:
        return len(encoder.encode(text, disallowed_special=()))
    # Without the tokenizer (offline, or it couldn't download), estimate: code and JSON
    # average about 3.5 bytes per token. Counting every byte as a token cut guidance to
    # a quarter of its budget.
    return -(-len(text.encode()) * 2 // 7)


def render_pointer(result, budget=350):
    plan = result.get("plan")
    if plan and (
        len(plan["steps"]) > 1
        or any(gap["reason"] != "ineligible_procedure" for gap in plan["gaps"])
    ):
        return render_plan(
            plan, budget, result.get("inspection_available", True), result.get("file_warning")
        )
    if not result.get("candidates"):
        return ""
    candidate = result["candidates"][0]
    definition = candidate["definition"]
    from .artifacts import extract_experience, proven_edits
    from .learning import agent_learning_definition

    definition = agent_learning_definition(
        definition, candidate.get("unproven_lines", []), candidate.get("successes", 0)
    )

    # Derive from immutable step arguments so older revisions get the same renderer.
    experience = extract_experience(definition["steps"])
    learning = definition.get("provenance", {}).get("learning", {})
    proposal = learning.get("proposal") or {}
    pointer = {
        "notice": NOTICE,
        "procedure": candidate["procedure_token"],
        "version": candidate["procedure_version"],
        "title": definition["title"][:160],
        "when": (proposal.get("trigger") or {}).get("text", ""),
        "principle": (proposal.get("principle") or {}).get("text", ""),
        "adapt": (proposal.get("adaptation") or {}).get("text", ""),
        "method": [step["text"] for step in proposal.get("steps", [])],
        "changed_files": experience["writes"][:6],
        "verify": experience["verification_commands"][-1:],
        # Lines no independent check reproduced are withheld, not handed on.
        "edits": proven_edits(experience["edits"], set(candidate.get("unproven_lines", [])))[:4],
        "requires_files": experience["inputs"][:4],
        "preconditions": definition.get("preconditions", [])[:2],
        "avoid": definition.get("avoidance_rules", [])[:1],
        "permissions": definition.get("required_permissions", []),
    }
    if learning.get("withheld"):
        pointer["method_withheld"] = "Cited changes have not been independently reproduced."
    if result.get("inspection_available", True):
        pointer["inspect"] = "musclememory_inspect"
    if result.get("file_warning"):
        pointer["warning"] = result["file_warning"]
    if result.get("related_procedures"):
        pointer["related"] = [
            {
                "procedure": item["procedure_token"],
                "relation": item["relation"],
                "artifact": item["artifact"],
            }
            for item in result["related_procedures"][:1]
        ]
    if not pointer["changed_files"] and not pointer["verify"]:
        pointer["read_files"] = experience["reads"][:4]
        pointer["actions"] = list(dict.fromkeys(definition["required_capabilities"]))[:3]
    pointer = {key: value for key, value in pointer.items() if value not in ([], "")}

    def serialize():
        return json.dumps(pointer, ensure_ascii=False, separators=(",", ":"))

    # Remove complete optional fields/items, never slice encoded JSON or warnings.
    for field in (
        "related",
        "requires_files",
        "avoid",
        "preconditions",
        "actions",
        "read_files",
        "title",
        "adapt",
    ):
        while token_count(serialize()) > budget and field in pointer:
            if isinstance(pointer[field], list) and len(pointer[field]) > 1:
                pointer[field].pop()
            else:
                del pointer[field]
    # A distilled method gets the budget before incidental code hunks. Preserve
    # complete instructions: never truncate a method into a misleading prefix.
    if token_count(serialize()) > budget and "method" in pointer:
        pointer.pop("edits", None)
    if token_count(serialize()) > budget:
        pointer.pop("method", None)
        pointer.pop("when", None)
    while token_count(serialize()) > budget and pointer.get("edits"):
        # Prefer useful added lines, then remove whole hunks. No clipped code strings.
        with_before = next((item for item in reversed(pointer["edits"]) if "before" in item), None)
        if with_before:
            del with_before["before"]
            if not {"before", "after"} & set(with_before):
                pointer["edits"].remove(with_before)
        else:
            pointer["edits"].pop()
    if pointer.get("edits") == []:
        del pointer["edits"]
    for field in ("inspect", "changed_files", "verify"):
        while token_count(serialize()) > budget and field in pointer:
            if isinstance(pointer[field], list) and len(pointer[field]) > 1:
                pointer[field].pop()
            else:
                del pointer[field]
    # Permission lists, file warnings and the notice are essential. With unusually large
    # essential context abstain, rather than deliver an incomplete warning.
    text = serialize()
    useful = any(
        pointer.get(field)
        for field in (
            "principle",
            "method",
            "changed_files",
            "read_files",
            "edits",
            "actions",
            "verify",
        )
    )
    return text if useful and token_count(text) <= budget else ""


def render_plan(plan, budget, inspection_available, file_warning=None):
    pointer = {
        "notice": NOTICE,
        "complete": plan["complete"],
        "plan": [
            {
                "procedure": step["procedure_token"],
                "version": step["procedure_version"],
                "role": step["role"],
                "changed_files": step["changed_files"],
                "verify": step["verify"],
            }
            for step in plan["steps"]
        ],
        "gaps": plan["gaps"],
        "requires_files": plan["external_inputs"],
        "permissions": sorted(
            {permission for step in plan["steps"] for permission in step["permissions"]}
        ),
    }
    if inspection_available:
        pointer["inspect"] = "musclememory_inspect per procedure"
    if file_warning:
        pointer["warning"] = file_warning

    def serialize():
        return json.dumps(pointer, ensure_ascii=False, separators=(",", ":"))

    if token_count(serialize()) > budget:
        pointer.pop("requires_files")
    for field in ("verify", "changed_files"):
        for step in reversed(pointer["plan"]):
            if token_count(serialize()) <= budget:
                break
            step.pop(field, None)
    if token_count(serialize()) > budget:
        # Preserve every dependency and uncovered goal; inspect for their detail.
        pointer["gaps"] = [
            {"reason": gap["reason"], **({"segment": gap["segment"]} if "segment" in gap else {})}
            for gap in plan["gaps"]
        ]
    useful = inspection_available or any(
        step.get("changed_files") or step.get("verify") for step in pointer["plan"]
    )
    return serialize() if useful and token_count(serialize()) <= budget else ""
