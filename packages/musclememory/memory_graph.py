"""A small typed evidence graph on the existing SQLite/PostgreSQL store."""

import hashlib
import json

from sqlalchemy import or_, select

from .artifacts import extract_experience, skill_name
from .storage import MemoryEdge, Procedure, Revision

RELATIONS = {
    "derived_from",
    "supported_by",
    "corroborates",
    "refines",
    "supersedes",
    "requires",
    "related_to",
    "redistills",
}


def node(kind, reference, version=0, event_id=""):
    return {"kind": kind, "reference": reference, "version": version, "event_id": event_id}


def node_key(endpoint):
    return hashlib.sha256(json.dumps(endpoint, sort_keys=True).encode()).hexdigest()


def connect(session, tenant, namespace, source, relation, target, evidence=None):
    if relation not in RELATIONS:
        raise ValueError("Unknown memory relation")
    source_key, target_key = node_key(source), node_key(target)
    prior = session.scalar(
        select(MemoryEdge).where(
            MemoryEdge.tenant == tenant,
            MemoryEdge.namespace == namespace,
            MemoryEdge.source_key == source_key,
            MemoryEdge.target_key == target_key,
            MemoryEdge.relation == relation,
        )
    )
    if prior is None:
        session.add(
            MemoryEdge(
                tenant=tenant,
                namespace=namespace,
                source_key=source_key,
                target_key=target_key,
                source=source,
                target=target,
                relation=relation,
                evidence=evidence or {},
            )
        )


def link_learning(session, tenant, namespace, token, version, source_run, proposal=None):
    skill = node("skill", token, version)
    connect(session, tenant, namespace, skill, "derived_from", node("run", source_run))
    if not proposal:
        return
    for index, claim in enumerate(proposal.claims()):
        lesson = node("claim", token, version, str(index))
        connect(session, tenant, namespace, skill, "supported_by", lesson, {"text": claim.text})
        for event in claim.evidence_event_ids:
            connect(
                session,
                tenant,
                namespace,
                lesson,
                "supported_by",
                node("event", source_run, event_id=event),
                {"text": claim.text},
            )
    if proposal.target and proposal.action in {"refine", "supersede"}:
        connect(
            session,
            tenant,
            namespace,
            skill,
            "supersedes" if proposal.action == "supersede" else "refines",
            node("skill", proposal.target, proposal.target_version),
            {"reason": proposal.reason, "corrections": proposal.correction_event_ids},
        )

    for link in proposal.links:
        connect(
            session,
            tenant,
            namespace,
            skill,
            link.relation,
            node("skill", link.target, link.target_version),
            {"reason": link.reason.text, "event_ids": link.reason.evidence_event_ids},
        )


def read_graph(session, tenant, namespace, root=None, limit=100):
    """Bounded graph inspection; every edge and skill endpoint is scope checked."""
    query = select(MemoryEdge).where(
        MemoryEdge.tenant == tenant,
        MemoryEdge.namespace == namespace,
    )
    if root:
        query = query.where(
            or_(
                MemoryEdge.source["reference"].as_string() == root,
                MemoryEdge.target["reference"].as_string() == root,
            )
        )
    rows = session.scalars(
        query.order_by(MemoryEdge.created_at.desc(), MemoryEdge.edge_id).limit(limit + 1)
    ).all()
    nodes, edges = {}, []
    for edge in rows[:limit]:
        endpoints = ((edge.source_key, edge.source), (edge.target_key, edge.target))
        scoped = True
        for _, endpoint in endpoints:
            if endpoint["kind"] in {"skill", "claim"}:
                procedure = session.get(Procedure, endpoint["reference"])
                if not procedure or procedure.tenant != tenant or procedure.namespace != namespace:
                    scoped = False
                    break
        if not scoped:
            continue
        for key, endpoint in endpoints:
            item = {"id": key, **endpoint}
            if endpoint["kind"] in {"skill", "claim"}:
                procedure = session.get(Procedure, endpoint["reference"])
                revision = session.get(Revision, (endpoint["reference"], endpoint["version"]))
                if revision:
                    item["title"] = (
                        procedure.name
                        or skill_name(extract_experience(revision.definition["steps"]))
                        or revision.definition["title"]
                    )
                else:
                    item["title"] = "Unavailable revision"
                item["current"] = procedure.current_version == endpoint["version"]
                item["status"] = procedure.lifecycle if item["current"] else "historical"
            nodes[key] = item
        edges.append(
            {
                "id": edge.edge_id,
                "source": edge.source_key,
                "target": edge.target_key,
                "relation": edge.relation,
                "evidence": edge.evidence,
            }
        )
    return {"nodes": list(nodes.values()), "edges": edges, "truncated": len(rows) > limit}
