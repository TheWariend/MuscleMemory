"""Bounded dependency plans over validated producer/consumer evidence."""

import re
from collections import defaultdict

from sqlalchemy import select

from .artifacts import extract_experience
from .retrieval import compatibility_freshness
from .shapes import conditions_satisfied
from .storage import Procedure, Revision


def task_segments(intent):
    # Split explicit sequencing only; plain conjunctions can belong to one goal.
    # Conditional and quoted prose is not a reliable goal boundary.
    if re.search(r"\bif\b|[\"'`]", intent, flags=re.I):
        return [intent.strip()]
    return [
        part.strip()
        for part in re.split(r"\s+(?:and then|then|followed by)\s+", intent, flags=re.I)
        if part.strip()
    ]


def compose_plan(
    session, tenant, namespace, requested, profile, settings, *, gaps=(), max_steps=12, max_depth=6
):
    rows = session.execute(
        select(Procedure, Revision)
        .join(
            Revision,
            (Procedure.procedure_token == Revision.procedure_token)
            & (Procedure.current_version == Revision.version),
        )
        .where(
            Procedure.tenant == tenant,
            Revision.tenant == tenant,
            Procedure.namespace == namespace,
            Procedure.lifecycle == "active",
            Revision.validation_status == "validated",
        )
        .order_by(Procedure.procedure_token)
        .limit(settings.candidate_limit + 1)
    ).all()
    if len(rows) > settings.candidate_limit:
        return {
            "steps": [],
            "dependencies": [],
            "gaps": [*gaps, {"reason": "planning_corpus_limit"}],
            "complete": False,
            "external_inputs": [],
        }
    eligible = {
        p.procedure_token: (p, r, extract_experience(r.definition["steps"]))
        for p, r in rows
        if compatibility_freshness(p, r, profile, settings) is not None
        and conditions_satisfied(r.definition.get("applicability_conditions", []), profile, [])
        and not r.definition["parameters"]
        and not any(step.get("condition") for step in r.definition["steps"])
    }
    producers, touches = defaultdict(set), defaultdict(set)
    for token, (_, _, experience) in eligible.items():
        for artifact in experience["writes"]:
            producers[artifact].add(token)
        for artifact in [*experience["reads"], *experience["writes"]]:
            touches[artifact].add(token)
    requested_tokens = {item["procedure_token"] for item in requested}
    steps, edges, missing, external = [], [], list(gaps), set()
    active, done = set(), set()

    def visit(token, depth):
        if token in done:
            return True
        if token in active:
            missing.append({"reason": "dependency_cycle", "procedure": token})
            return False
        if depth > max_depth or len(done) >= max_steps:
            missing.append({"reason": "planning_bound", "procedure": token})
            return False
        if token not in eligible:
            missing.append({"reason": "ineligible_procedure", "procedure": token})
            return False
        active.add(token)
        procedure, revision, experience = eligible[token]
        complete = True
        for artifact in experience["inputs"]:
            choices = producers[artifact] - {token}
            if len(touches[artifact]) > settings.link_fanout_limit:
                external.add(artifact)
                continue
            if not choices:
                external.add(artifact)
                continue
            explicit = choices & requested_tokens
            if len(explicit) == 1:
                choices = explicit
            if len(choices) != 1:
                missing.append({"reason": "ambiguous_producer", "artifact": artifact})
                complete = False
                continue
            parent = next(iter(choices))
            complete = visit(parent, depth + 1) and complete
            edges.append({"from": parent, "to": token, "artifact": artifact})
        active.remove(token)
        if len(done) >= max_steps:
            missing.append({"reason": "planning_bound", "procedure": token})
            return False
        done.add(token)
        steps.append(
            {
                "procedure_token": token,
                "procedure_version": revision.version,
                "role": "requested" if token in requested_tokens else "prerequisite",
                "title": revision.definition["title"],
                "requires_files": experience["inputs"],
                "changed_files": experience["writes"],
                "verify": experience["verification_commands"][-1:],
                "permissions": revision.definition["required_permissions"],
            }
        )
        return complete

    complete = True
    for item in requested:
        complete = visit(item["procedure_token"], 0) and complete
    if any(gap["reason"] == "dependency_cycle" for gap in missing):
        steps, edges = [], []
    return {
        "steps": steps,
        "dependencies": edges,
        "gaps": missing,
        "external_inputs": sorted(external),
        "complete": complete and not missing,
    }
