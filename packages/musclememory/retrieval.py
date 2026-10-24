"""Bounded tenant-filtered hybrid candidate selection and explainable ranking."""

import hashlib
import math
import re
from collections import Counter, OrderedDict
from functools import lru_cache
from threading import Lock

import httpx
from pgvector.sqlalchemy import Vector
from sqlalchemy import Float, and_, cast, func, or_, select, text

from .artifacts import (
    extract_experience,
    needs_test_proof,
    operation_categories,
    query_keys,
    targets,
)
from .compiler import signature
from .core import DomainError
from .shapes import bound_capability, conditions_satisfied
from .storage import ArtifactPosting, Procedure, Revision, utcnow


@lru_cache(maxsize=2)
def local_embedding_model(model, cache_dir):
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=model, cache_dir=cache_dir, threads=2)


class Embeddings:
    def __init__(self, settings):
        self.settings = settings
        self.provider = settings.embedding_provider or (
            "remote" if settings.embedding_url else "lexical-hash"
        )
        self.space = ("semantic:" if settings.embedding_url else "lexical:") + hashlib.sha256(
            (
                f"{settings.embedding_url}:{settings.embedding_model}"
                + (f":{settings.embedding_dim}" if settings.embedding_dim != 128 else "")
            ).encode()
        ).hexdigest()[:32]
        self.mode = "semantic" if settings.embedding_url else "lexical-hash"
        if self.provider == "fastembed":
            self.mode = "semantic"
            self.space = (
                "semantic:"
                + hashlib.sha256(
                    f"fastembed:{settings.embedding_model}:{settings.embedding_dim}".encode()
                ).hexdigest()[:32]
            )

        self.space += ":task-title-v2"
        self._cache, self._cache_lock = OrderedDict(), Lock()

    def metadata(self):
        return {
            "provider": self.provider,
            "model": self.settings.embedding_model,
            "dimensions": self.settings.embedding_dim,
            "mode": self.mode,
            "space": self.space,
            "document_strategy": "task-title-v2",
        }

    def embed(self, text):
        with self._cache_lock:
            if text in self._cache:
                self._cache.move_to_end(text)
                return list(self._cache[text])
        vector = self._embed_uncached(text)
        with self._cache_lock:
            self._cache[text] = tuple(vector)
            self._cache.move_to_end(text)
            if len(self._cache) > 128:
                self._cache.popitem(last=False)
        return list(vector)

    def _embed_uncached(self, text):
        if self.provider == "fastembed":
            try:
                model = local_embedding_model(
                    self.settings.embedding_model, self.settings.embedding_cache_dir
                )
                vector = next(iter(model.embed([text]))).tolist()
                if (
                    len(vector) != self.settings.embedding_dim
                    or not all(math.isfinite(v) for v in vector)
                    or not any(vector)
                ):
                    raise ValueError("Invalid local embedding dimensions")
                return vector
            except Exception as error:
                raise DomainError(
                    "embedding_unavailable",
                    "Local semantic provider unavailable; "
                    "install the semantic extra and check model/dimensions",
                    503,
                ) from error
        if self.settings.embedding_url:
            headers = {}
            if self.settings.embedding_key:
                headers["Authorization"] = f"Bearer {self.settings.embedding_key}"
            try:
                response = httpx.post(
                    self.settings.embedding_url,
                    headers=headers,
                    timeout=10,
                    json={"model": self.settings.embedding_model, "input": [text]},
                )
                response.raise_for_status()
                vector = response.json()["data"][0]["embedding"]
                if (
                    len(vector) != self.settings.embedding_dim
                    or any(not math.isfinite(float(v)) for v in vector)
                    or sum(float(v) ** 2 for v in vector) == 0
                ):
                    raise ValueError("invalid vector")
                return [float(v) for v in vector]
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
                raise DomainError(
                    "embedding_unavailable", "Embedding provider failed validation", 503
                ) from error
        vector = [0.0] * self.settings.embedding_dim
        for term in re.findall(r"\w+", text.casefold()):
            hashed = hashlib.blake2b(term.encode(), digest_size=8).digest()
            vector[int.from_bytes(hashed[:4], "big") % self.settings.embedding_dim] += (
                1 if hashed[4] % 2 else -1
            )
        norm = math.sqrt(sum(v * v for v in vector)) or 1
        return [v / norm for v in vector]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return max(0.0, dot / norm) if norm else 0.0


def cosines(vector, stored):
    """Cosine similarity against many stored vectors at once; None scores 0."""
    import numpy as np

    present = [index for index, item in enumerate(stored) if item is not None]
    result = [0.0] * len(stored)
    query = np.asarray(vector, dtype=np.float64)
    norm = float(np.linalg.norm(query))
    if not present or not norm:
        return result
    matrix = np.asarray([stored[index] for index in present], dtype=np.float64)
    norms = np.linalg.norm(matrix, axis=1)
    values = (matrix @ query) / np.where(norms == 0, np.inf, norms * norm)
    for index, value in zip(present, values, strict=True):
        result[index] = max(0.0, float(value))
    return result


def reliability(revision):
    # Laplace smoothing; expose sample counts alongside this indicator.
    return (revision.successes + 1) / (revision.successes + revision.failures + 2)


STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it of on or that the "
    "this to was were with".split()
)


def lexical_terms(value):
    return [term for term in signature(value).split() if term not in STOPWORDS]


def bm25_scores(documents, query, k1=1.5, b=0.75):
    """Same tokenization, IDF and floating-point math on both databases.

    Corpus = the tenant/namespace-filtered bounded candidate union. Native FTS
    only selects candidates; it never supplies the final lexical score.
    """
    documents = [Counter(lexical_terms(document)) for document in documents]
    count = len(documents)
    if not count:
        return []
    lengths = [sum(document.values()) for document in documents]
    average = sum(lengths) / count or 1.0
    query_terms = set(lexical_terms(query))
    frequencies = {term: sum(term in document for document in documents) for term in query_terms}
    scores = []
    for document, length in zip(documents, lengths, strict=True):
        score = 0.0
        for term in sorted(query_terms):
            tf = document[term]
            idf = math.log1p((count - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / average))
        scores.append(score)
    return scores


def reciprocal_rank_fusion(lists, k=60):
    scores = {}
    for ranking in lists:
        # A procedure appears once per list even if a candidate pool duplicated it.
        ranking = list(dict.fromkeys(ranking))
        for rank, token in enumerate(ranking, 1):
            scores[token] = scores.get(token, 0.0) + 1.0 / (k + rank)
    return scores


def stored_vector(revision):
    return (
        revision.embedding_payload if revision.embedding_payload is not None else revision.embedding
    )


def compatibility_freshness(procedure, revision, profile, settings):
    definition = revision.definition
    anchor = revision.validated_at or revision.created_at
    age_days = (utcnow() - anchor).total_seconds() / 86400
    if (
        any(
            bound_capability(capability, profile.bindings) is None
            for capability in definition["required_capabilities"]
        )
        or set(definition["required_permissions"]) - set(profile.permissions)
        or any(
            profile.environment.get(key) != value
            for key, value in definition["environment_requirements"].items()
        )
        or age_days >= settings.stale_days
        or reliability(revision) < settings.min_reliability
        or revision.successes < settings.min_successes
        or revision.validation_status != "validated"
        or procedure.lifecycle != "active"
        or procedure.reviewed_at is None  # automatically learned, waiting for a person's OK
        or (
            settings.require_proven_test
            and revision.test_proven_at is None
            and needs_test_proof(definition)
        )
    ):
        return None
    return max(0.0, 1 - age_days / settings.stale_days)


def vector_neighbors(session, statement, vector, space, limit):
    """Nearest stored vectors through the partial HNSW index for this dimension."""
    dimensions = len(vector)
    # Must match the index expression exactly: (embedding::vector(N)) <=> query.
    distance = cast(Revision.embedding, Vector(dimensions)).op("<=>", return_type=Float)(vector)
    # Scoping filters run after the index scan. Iterative scans keep reading the
    # graph until enough rows pass them; ef_search must cover the result limit.
    session.execute(text(f"SET LOCAL hnsw.ef_search = {max(100, 2 * int(limit))}"))
    session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    return session.execute(
        statement.where(
            Revision.embedding_dimensions == dimensions, Revision.embedding_space == space
        )
        .order_by(distance)
        .limit(limit)
    ).all()


# SQLite has no vector or full-text index here. Each process keeps the current
# revisions of a scope as one normalized vector matrix plus keyword postings and
# rebuilds them when revisions are added or removed. A recall is then a matrix
# product and a few array lookups instead of a scan of every row.
_LOCAL_INDEXES, _LOCAL_INDEX_LOCK = OrderedDict(), Lock()


def clear_vector_cache():
    with _LOCAL_INDEX_LOCK:
        _LOCAL_INDEXES.clear()


def local_index(session, tenant, namespace, space, dimensions):
    import numpy as np

    # Cheap whole-table change marker; reindexing clears the cache explicitly.
    fingerprint = tuple(session.execute(text("SELECT count(*), max(rowid) FROM revisions")).one())
    key = (str(session.bind.url), tenant, namespace, space, dimensions)
    with _LOCAL_INDEX_LOCK:
        cached = _LOCAL_INDEXES.get(key)
    if cached and cached["fingerprint"] == fingerprint:
        return cached
    rows = session.execute(
        select(
            Revision.procedure_token,
            Revision.search_text,
            Revision.embedding_space,
            Revision.embedding_dimensions,
            Revision.embedding,
            Revision.embedding_payload,
        )
        .join(
            Procedure,
            (Procedure.procedure_token == Revision.procedure_token)
            & (Procedure.current_version == Revision.version),
        )
        .where(
            Procedure.tenant == tenant,
            Procedure.namespace == namespace,
            Revision.tenant == tenant,
        )
    ).all()
    postings, lengths = {}, []
    for index, row in enumerate(rows):
        terms = Counter(lexical_terms(row.search_text))
        lengths.append(sum(terms.values()))
        for term, frequency in terms.items():
            postings.setdefault(term, ([], []))
            postings[term][0].append(index)
            postings[term][1].append(frequency)
    semantic = [
        index
        for index, row in enumerate(rows)
        if row.embedding_space == space and row.embedding_dimensions == dimensions
    ]
    matrix = np.array(
        [
            rows[index].embedding_payload
            if rows[index].embedding_payload is not None
            else rows[index].embedding
            for index in semantic
        ],
        dtype=np.float32,
    ).reshape(len(semantic), dimensions)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix /= np.where(norms == 0, 1, norms)
    cached = {
        "fingerprint": fingerprint,
        "tokens": [row.procedure_token for row in rows],
        "postings": {
            term: (np.array(docs), np.array(freqs, dtype=np.float32))
            for term, (docs, freqs) in postings.items()
        },
        "lengths": np.array(lengths, dtype=np.float32),
        "semantic": np.array(semantic, dtype=np.int64),
        "matrix": matrix,
    }
    with _LOCAL_INDEX_LOCK:
        _LOCAL_INDEXES[key] = cached
        while len(_LOCAL_INDEXES) > 8:
            _LOCAL_INDEXES.popitem(last=False)
    return cached


def top_rows(session, statement, tokens, scores, limit):
    import numpy as np

    positive = np.flatnonzero(scores > 0)
    if not len(positive):
        return []
    # Over-fetch: lifecycle and validation filters are applied by the database.
    count = min(len(positive), 4 * limit)
    top = positive[np.argpartition(-scores[positive], count - 1)[:count]]
    chosen = [tokens[index] for index in top[np.argsort(-scores[top], kind="stable")]]
    # Check eligibility with a token-only query, then load full rows for winners.
    eligible = set(
        session.scalars(
            statement.with_only_columns(Procedure.procedure_token).where(
                Procedure.procedure_token.in_(chosen)
            )
        )
    )
    winners = [token for token in chosen if token in eligible][:limit]
    if not winners:
        return []
    order = {token: rank for rank, token in enumerate(winners)}
    rows = session.execute(statement.where(Procedure.procedure_token.in_(winners))).all()
    return sorted(rows, key=lambda row: order[row[0].procedure_token])


def local_lexical_neighbors(session, statement, index, query, limit, k1=1.5, b=0.75):
    import numpy as np

    count = len(index["tokens"])
    if not count:
        return []
    lengths = index["lengths"]
    average = float(lengths.mean()) or 1.0
    scores = np.zeros(count, dtype=np.float32)
    for term in set(lexical_terms(query)):
        if term not in index["postings"]:
            continue
        docs, tf = index["postings"][term]
        idf = math.log1p((count - len(docs) + 0.5) / (len(docs) + 0.5))
        scores[docs] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * lengths[docs] / average))
    return top_rows(session, statement, index["tokens"], scores, limit)


def local_vector_neighbors(session, statement, index, vector, limit):
    import numpy as np

    query = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(query))
    if not len(index["semantic"]) or not norm:
        return []
    scores = np.zeros(len(index["tokens"]), dtype=np.float32)
    # Shift by one so every compatible vector, even an orthogonal one, ranks.
    scores[index["semantic"]] = index["matrix"] @ (query / norm) + 1.0
    return top_rows(session, statement, index["tokens"], scores, limit)


def hybrid_candidates(session, tenant, namespace, query, vector, space, limit):
    statement = (
        select(Procedure, Revision)
        .join(
            Revision,
            (
                (Procedure.procedure_token == Revision.procedure_token)
                & (Procedure.current_version == Revision.version)
            ),
        )
        .where(
            Procedure.tenant == tenant,
            Revision.tenant == tenant,
            Procedure.namespace == namespace,
            Procedure.lifecycle == "active",
            Revision.validation_status == "validated",
        )
    )
    pools = []
    exact_keys = query_keys(query)
    if exact_keys:
        conditions = [
            and_(
                ArtifactPosting.direction.in_(["read", "write"])
                if kind == "artifact"
                else ArtifactPosting.direction == "command",
                ArtifactPosting.value == value,
            )
            for kind, value in exact_keys
        ]
        pools.append(
            session.execute(
                statement.join(
                    ArtifactPosting,
                    (ArtifactPosting.procedure_token == Revision.procedure_token)
                    & (ArtifactPosting.version == Revision.version),
                )
                .where(
                    ArtifactPosting.tenant == tenant,
                    ArtifactPosting.namespace == namespace,
                    or_(*conditions),
                )
                .limit(limit)
            ).all()
        )
    if session.bind.dialect.name == "postgresql":
        ts = func.to_tsvector("english", Revision.search_text)
        tsquery = func.websearch_to_tsquery("english", query)
        pools.append(
            session.execute(
                statement.where(ts.op("@@")(tsquery))
                .order_by(func.ts_rank_cd(ts, tsquery).desc())
                .limit(limit)
            ).all()
        )
        pools.append(vector_neighbors(session, statement, vector, space, limit))
    else:
        index = local_index(session, tenant, namespace, space, len(vector))
        pools.append(local_lexical_neighbors(session, statement, index, query, limit))
        pools.append(local_vector_neighbors(session, statement, index, vector, limit))
    pools.append(
        session.execute(statement.order_by(Procedure.updated_at.desc()).limit(limit)).all()
    )
    return sorted(
        {p.procedure_token: (p, r) for pool in pools for p, r in pool}.values(),
        key=lambda pair: pair[0].procedure_token,
    )


def same_place(asked, path):
    """A requested path and a skill's path name the same file, or one holds the other."""
    asked, path = asked.rstrip("/"), path.rstrip("/")
    return asked == path or path.startswith(asked + "/") or asked.startswith(path + "/")


def rank_candidates(candidates, query, vector, profile, settings, embedding_space=None):
    candidates = list(candidates)
    lexical = bm25_scores([r.search_text for _, r in candidates], query)
    space = embedding_space or Embeddings(settings).space
    similarities = cosines(
        vector,
        [
            stored_vector(r)
            if r.embedding_space == space and r.embedding_dimensions == len(vector)
            else None
            for _, r in candidates
        ],
    )
    keys = query_keys(query)
    exact_targets, other_targets = [], []
    for _, revision in candidates:
        experience = revision.definition.get("experience") or extract_experience(
            revision.definition["steps"]
        )
        # A test script every task runs says nothing about which task this is.
        testing = set()
        for step in revision.definition["steps"]:
            if "verify" in operation_categories(step["arguments"], step["capability"]):
                testing.update(*targets(step["arguments"], step["effect"], step["capability"]))
        touched = {*experience["reads"], *experience["writes"]} - testing
        stored_keys = {("artifact", p) for p in touched}
        stored_keys.update(("command", c) for c in experience["commands"])
        exact_targets.append(len(keys & stored_keys))
        # The request names folders or files to work in, and the skill changed none of
        # them: it is a different task in the same project.
        named = {p for kind, p in keys if kind == "artifact" and "/" in p} - testing
        other_targets.append(
            bool(named and experience["writes"])
            and not any(same_place(asked, path) for asked in named for path in touched)
        )
    tokens = [p.procedure_token for p, _ in candidates]
    # Stable text tie-breaks keep different task families independent of UUIDs.
    tie_keys = [
        (revision.search_text, token)
        for (_, revision), token in zip(candidates, tokens, strict=True)
    ]
    lexical_ranking = sorted(range(len(tokens)), key=lambda i: (-lexical[i], tie_keys[i]))
    semantic_ranking = sorted(range(len(tokens)), key=lambda i: (-similarities[i], tie_keys[i]))
    fusion = reciprocal_rank_fusion(
        [
            [
                tokens[i]
                for i in sorted(range(len(tokens)), key=lambda i: -exact_targets[i])
                if exact_targets[i]
            ],
            [tokens[i] for i in lexical_ranking if lexical[i] > 0],
            [
                tokens[i]
                for i in semantic_ranking
                if similarities[i]
                >= (
                    settings.semantic_min_similarity
                    if Embeddings(settings).mode == "semantic"
                    else 0.01
                )
            ],
        ]
    )
    results = []
    for index, (procedure, revision) in enumerate(candidates):
        definition = revision.definition
        freshness = compatibility_freshness(procedure, revision, profile, settings)
        # Learned applicability conditions fail closed: unknown or unmet means skip.
        if freshness is None or not conditions_satisfied(
            definition.get("applicability_conditions", []), profile, []
        ):
            continue
        if other_targets[index]:
            continue
        exact = float(
            signature(query) == definition["intent_signature"] or exact_targets[index] > 0
        )
        lexical_strength = lexical[index] / (1.0 + lexical[index])
        similarity = similarities[index]
        semantic_strength = similarity if similarity >= settings.semantic_min_similarity else 0.0
        relevance = max(
            exact, semantic_strength, 0.55 * lexical_strength + 0.45 * semantic_strength
        )
        if relevance < settings.min_match:
            continue
        fused = fusion.get(procedure.procedure_token, 0.0) / (3 / 61)
        score = 0.75 * max(exact, fused) + 0.20 * reliability(revision) + 0.05 * freshness
        results.append(
            {
                "procedure_token": procedure.procedure_token,
                "procedure_version": revision.version,
                "score": round(score, 4),
                "relevance": round(relevance, 4),
                "explanation": {
                    "exact_match": bool(exact),
                    "exact_target_matches": exact_targets[index],
                    "bm25": round(lexical[index], 6),
                    (
                        "vector_similarity"
                        if Embeddings(settings).mode == "semantic"
                        else "lexical-hash"
                    ): round(similarity, 4),
                    "rrf": round(fusion.get(procedure.procedure_token, 0.0), 6),
                    "capabilities_compatible": True,
                    "applicability_satisfied": True,
                    "freshness": round(freshness, 4),
                },
                "confidence": round(reliability(revision), 4),
                "successes": revision.successes,
                "failures": revision.failures,
                "definition": definition,
                "capability_signature": revision.capability_signature,
                "avoidance_rules": definition.get("avoidance_rules", []),
                "recovery_notes": definition.get("recovery_notes", []),
                "resolved_tools": {
                    cap: profile.bindings[bound_capability(cap, profile.bindings)]
                    for cap in definition["required_capabilities"]
                },
            }
        )
    return sorted(
        results,
        key=lambda item: (
            -item["score"],
            -item["relevance"],
            item["definition"]["intent_signature"],
            item["procedure_token"],
        ),
    )


def graph_neighbors(session, tenant, namespace, primary, profile, settings, limit=2):
    """One-hop typed producer/consumer links, independently gated and scoped."""
    if not primary:
        return []
    source = primary[0]
    definition = source["definition"]
    experience = definition.get("experience") or extract_experience(definition["steps"])
    related = {}
    from .memory_graph import node, node_key
    from .storage import MemoryEdge

    key = node_key(node("skill", source["procedure_token"], source["procedure_version"]))
    semantic = session.scalars(
        select(MemoryEdge)
        .where(
            MemoryEdge.tenant == tenant,
            MemoryEdge.namespace == namespace,
            MemoryEdge.source_key == key,
            MemoryEdge.relation.in_(["requires", "related_to"]),
        )
        .limit(settings.link_fanout_limit)
    ).all()
    for edge in semantic:
        target = edge.target
        procedure = session.get(Procedure, target["reference"])
        revision = session.get(Revision, (target["reference"], target["version"]))
        if (
            not procedure
            or not revision
            or procedure.tenant != tenant
            or procedure.namespace != namespace
            or procedure.current_version != revision.version
            or compatibility_freshness(procedure, revision, profile, settings) is None
        ):
            continue
        related[procedure.procedure_token] = {
            "procedure_token": procedure.procedure_token,
            "procedure_version": revision.version,
            "title": revision.definition["title"],
            "relation": edge.relation,
            "artifact": "",
            "reason": edge.evidence.get("reason", ""),
        }
    for field, direction, relation in (
        ("reads", "write", "prerequisite"),
        ("writes", "read", "follow-up"),
    ):
        for artifact in experience[field][:32]:
            degree = session.scalar(
                select(func.count(func.distinct(ArtifactPosting.procedure_token))).where(
                    ArtifactPosting.tenant == tenant,
                    ArtifactPosting.namespace == namespace,
                    ArtifactPosting.direction.in_(["read", "write"]),
                    ArtifactPosting.value == artifact,
                )
            )
            if degree > settings.link_fanout_limit:
                continue
            rows = session.execute(
                select(Procedure, Revision)
                .join(
                    Revision,
                    (Revision.procedure_token == Procedure.procedure_token)
                    & (Revision.version == Procedure.current_version),
                )
                .join(
                    ArtifactPosting,
                    (ArtifactPosting.procedure_token == Revision.procedure_token)
                    & (ArtifactPosting.version == Revision.version),
                )
                .where(
                    Procedure.tenant == tenant,
                    Revision.tenant == tenant,
                    Procedure.namespace == namespace,
                    ArtifactPosting.tenant == tenant,
                    ArtifactPosting.namespace == namespace,
                    ArtifactPosting.direction == direction,
                    ArtifactPosting.value == artifact,
                    Procedure.procedure_token != source["procedure_token"],
                )
                .limit(settings.candidate_limit)
            ).all()
            for procedure, revision in rows:
                if compatibility_freshness(procedure, revision, profile, settings) is None:
                    continue
                related[procedure.procedure_token] = {
                    "procedure_token": procedure.procedure_token,
                    "procedure_version": revision.version,
                    "title": revision.definition["title"],
                    "relation": relation,
                    "artifact": artifact,
                }
    return sorted(related.values(), key=lambda item: (item["relation"], item["procedure_token"]))[
        :limit
    ]
