"""Immutable source-bound navigation cards; derived summaries are never evidence."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import time
from types import MappingProxyType
from collections.abc import MutableMapping

from .bm25 import BM25Index, validate_limit
from .evidence_packets import build_packets, digest, freeze, thaw
from .fusion import reciprocal_rank_fusion
from .retrieval import normalized, valid_vector

SCHEMA_VERSION = "b4-navigation-v1"
MAX_INNER_BYTES = 6000
MAX_SUMMARY_BYTES = 768
MAX_CALLS = 400


class NavigationCache(MutableMapping):
    """Atomic local cache: persist call starts before a paid adapter is invoked."""

    def __init__(self, path):
        self.path = Path(path)
        self._values = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(self._values, dict):
            raise ValueError("NAVIGATION_CACHE_INVALID")

    def __getitem__(self, key):
        return thaw(freeze(self._values[key]))

    def __setitem__(self, key, value):
        self._values[key] = thaw(freeze(value))
        self._persist()

    def __delitem__(self, key):
        del self._values[key]
        self._persist()

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(_json(self._values) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _ordered(rows):
    return sorted(rows, key=lambda r: (r["retrieval_version_id"], r["ordinal"], r["chunk_id"]))


def _fingerprints(rows):
    return {row["chunk_id"]: digest(row) for row in _ordered(rows)}


@dataclass(frozen=True)
class SummaryCall:
    call_id: str
    node_id: str
    source_text: str
    depends_on: tuple[str, ...]
    binding_hash: str

    def request(self, summaries=None):
        request = {"node_id": self.node_id, "binding_hash": self.binding_hash,
                   "task": "merge" if self.depends_on else "summarize",
                   "max_output_tokens": 512, "max_summary_utf8_bytes": MAX_SUMMARY_BYTES}
        if self.depends_on:
            request["parts"] = [{"call_id": i, "summary": summaries[i]} for i in self.depends_on]
        else:
            request["source_text"] = self.source_text
        return request


@dataclass(frozen=True)
class NavigationPlan:
    cards: tuple
    packets: tuple
    unavailable: tuple
    calls: tuple[SummaryCall, ...]
    final_calls: object
    source_fingerprints: object
    nodes_hash: str
    scope_snapshot_ids: tuple[str, ...]
    manifest_ids: tuple[str, ...]
    summary_model: str
    prompt_hash: str

    @property
    def call_count(self):
        return len(self.calls)

    @property
    def report(self):
        return {"card_count": len(self.cards), "packet_count": len(self.packets),
                "unavailable_units": len(self.unavailable), "summary_calls": self.call_count,
                "partition_calls": sum(not c.depends_on for c in self.calls),
                "merge_calls": sum(bool(c.depends_on) for c in self.calls),
                "max_input_units": 8000, "max_output_tokens": 512, "max_summary_calls": MAX_CALLS}


def _source_cards(rows, nodes, packets):
    by_id = {row["chunk_id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("STRUCTURE_UNAVAILABLE")
    versions = {row["retrieval_version_id"] for row in rows}
    by_node = {}
    for node in nodes:
        identifier = node.get("node_id")
        if (not isinstance(identifier, str) or not identifier or identifier in by_node
                or node.get("retrieval_version_id") not in versions
                or node.get("kind") not in {"document", "section", "leaf"}):
            raise ValueError("STRUCTURE_UNAVAILABLE")
        by_node[identifier] = node
    ancestors, leaf_nodes = {}, {}
    for identifier, node in by_node.items():
        lineage, seen, cursor = [], {identifier}, node.get("parent_node_id")
        while cursor is not None:
            parent = by_node.get(cursor)
            if (cursor in seen or parent is None or parent["kind"] == "leaf"
                    or parent["retrieval_version_id"] != node["retrieval_version_id"]):
                raise ValueError("STRUCTURE_UNAVAILABLE")
            seen.add(cursor)
            lineage.append(cursor)
            cursor = parent.get("parent_node_id")
        if node["kind"] != "document" and not lineage:
            raise ValueError("STRUCTURE_UNAVAILABLE")
        ancestors[identifier] = tuple(lineage)
        if node["kind"] == "leaf":
            leaf_id = node.get("chunk_id")
            row = by_id.get(leaf_id)
            if (row is None or leaf_id in leaf_nodes
                    or row["retrieval_version_id"] != node["retrieval_version_id"]
                    or (row.get("parent_id") and row["parent_id"] != node["parent_node_id"])):
                raise ValueError("STRUCTURE_UNAVAILABLE")
            leaf_nodes[leaf_id] = identifier
    if set(leaf_nodes) != set(by_id):
        raise ValueError("STRUCTURE_UNAVAILABLE")
    cards = []

    def card(identifier, parent, kind, title, members, **extra):
        members = _ordered(members)
        source_map = [{"leaf_id": r["chunk_id"], "source_spans": r["source_spans"]} for r in members]
        return dict(node_id=identifier, parent_id=parent, kind=kind, title=title,
                    ordered_leaf_ids=[r["chunk_id"] for r in members],
                    source_hash=digest("\n".join(r["source_text"] for r in members)),
                    source_map_hash=digest(source_map), source_map=source_map,
                    retrieval_version_id=members[0]["retrieval_version_id"],
                    material_version_id=members[0]["material_version_id"],
                    tree_version_id=members[0]["tree_version_id"],
                    ordinal=members[0]["ordinal"], children=[], **extra)

    for identifier, node in by_node.items():
        if node["kind"] != "section":
            continue
        path = node.get("section_path")
        if not isinstance(path, list) or not path or any(not isinstance(t, str) or not t for t in path):
            raise ValueError("STRUCTURE_UNAVAILABLE")
        members = [r for r in rows if identifier in ancestors[leaf_nodes[r["chunk_id"]]]]
        if not members:
            continue
        descendant = [p.packet_id for p in packets
                      if identifier == p.parent_id or identifier in ancestors.get(p.parent_id, ())]
        cards.append(card(identifier, node.get("parent_node_id"), "section", path[-1], members,
                          section_path=path, descendant_packet_ids=descendant, packet_id=None))
    for packet in packets:
        parent = by_node.get(packet.parent_id)
        if parent is None or parent["kind"] != "section":
            raise ValueError("STRUCTURE_UNAVAILABLE")
        cards.append(card(packet.packet_id, packet.parent_id, "packet",
                          parent["section_path"][-1], [by_id[i] for i in packet.leaf_ids],
                          section_path=parent["section_path"],
                          descendant_packet_ids=[packet.packet_id], packet_id=packet.packet_id))
    for item in cards:
        item["children"] = [c["node_id"] for c in cards if c["parent_id"] == item["node_id"]]
    return sorted(cards, key=lambda c: (c["retrieval_version_id"], c["ordinal"], c["node_id"]))


def _plan_calls(card, source, model, prompt):
    binding = digest([card, model, prompt])
    calls, position = [], 0
    while position < len(source):
        low, high, best = 1, len(source) - position, 0
        while low <= high:
            middle = (low + high) // 2
            trial = SummaryCall("", card["node_id"], source[position:position + middle], (), binding)
            if len(_json(trial.request()).encode("utf-8")) <= MAX_INNER_BYTES:
                best, low = middle, middle + 1
            else:
                high = middle - 1
        if not best:
            raise ValueError("SUMMARY_INPUT_BUDGET")
        text = source[position:position + best]
        call_id = digest([binding, "partition", position, text])
        calls.append(SummaryCall(call_id, card["node_id"], text, (), binding))
        position += best
    frontier = [call.call_id for call in calls]
    while len(frontier) > 1:
        next_frontier = []
        for start in range(0, len(frontier), 3):
            group = tuple(frontier[start:start + 3])
            if len(group) == 1:
                next_frontier.append(group[0])
                continue
            call_id = digest([binding, "merge", group])
            calls.append(SummaryCall(call_id, card["node_id"], "", group, binding))
            next_frontier.append(call_id)
        frontier = next_frontier
    if not calls:
        raise ValueError("STRUCTURE_UNAVAILABLE")
    return calls, frontier[0]


def plan_navigation(rows, nodes, *, scope_snapshot_ids, manifest_ids,
                    summary_model, prompt_hash, max_calls=400):
    """Freeze every source partition and merge before any paid model call."""
    rows, nodes = list(rows), list(nodes)
    if (type(max_calls) is not int or not 1 <= max_calls <= MAX_CALLS
            or not scope_snapshot_ids or not manifest_ids or not summary_model or not prompt_hash):
        raise ValueError("NAVIGATION_PROFILE_INVALID")
    result = build_packets(rows)
    cards = _source_cards(rows, nodes, result.packets)
    by_id, calls, final_calls = {r["chunk_id"]: r for r in rows}, [], {}
    for card in cards:
        text = "\n".join(by_id[i]["source_text"] for i in card["ordered_leaf_ids"])
        planned, final_id = _plan_calls(card, text, summary_model, prompt_hash)
        calls.extend(planned)
        final_calls[card["node_id"]] = final_id
    if len(calls) > max_calls:
        raise ValueError("SUMMARY_CALL_BUDGET")
    return NavigationPlan(freeze(cards), result.packets, result.unavailable, tuple(calls),
                          freeze(final_calls), freeze(_fingerprints(rows)),
                          digest(sorted(nodes, key=lambda n: n["node_id"])),
                          tuple(sorted(set(scope_snapshot_ids))), tuple(sorted(set(manifest_ids))),
                          summary_model, prompt_hash)


def _summary(result):
    if not isinstance(result, dict) or set(result) - {"summary", "usage"}:
        raise ValueError("SUMMARY_INVALID_RESPONSE")
    text = result.get("summary")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_SUMMARY_BYTES:
        raise ValueError("SUMMARY_INVALID_RESPONSE")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        raise ValueError("SUMMARY_INVALID_RESPONSE")
    usage = result.get("usage", {})
    if not isinstance(usage, dict):
        raise ValueError("SUMMARY_INVALID_RESPONSE")
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    if output_tokens is not None and (type(output_tokens) is not int or not 0 <= output_tokens <= 512):
        raise ValueError("SUMMARY_INVALID_RESPONSE")
    return text


@dataclass(frozen=True)
class NavigationIndex:
    plan: NavigationPlan
    _cards: object
    _embeddings: object
    embedding_model: str
    dimension: int
    usage: tuple

    @property
    def cards(self):
        return thaw(self._cards)

    @property
    def packets(self):
        return MappingProxyType({p.packet_id: p for p in self.plan.packets})

    @property
    def index_id(self):
        return digest(self._artifact())

    @classmethod
    def build(cls, plan, *, summarizer, embedder, cache=None):
        if not isinstance(plan, NavigationPlan) or plan.call_count > MAX_CALLS:
            raise ValueError("SUMMARY_CALL_BUDGET")
        cache = {} if cache is None else cache
        summaries, usage = {}, []
        for call in plan.calls:
            request = call.request(summaries)
            if len(_json(request).encode("utf-8")) > MAX_INNER_BYTES:
                raise ValueError("SUMMARY_INPUT_BUDGET")
            key = "summary:" + digest([SCHEMA_VERSION, plan.summary_model, plan.prompt_hash, call.call_id, request])
            cached = key in cache
            if cached:
                entry = cache[key]
                if entry.get("status") != "complete":
                    raise ValueError("SUMMARY_CACHE_ATTEMPT_UNRESOLVED")
                result = entry["result"]
            else:
                cache[key] = {"status": "started", "call_id": call.call_id}
                started = time.monotonic()
                try:
                    result = summarizer(request)
                    _summary(result)
                except Exception as error:
                    from .verification import VerificationError
                    safe = {'type':type(error).__name__}
                    if isinstance(error,VerificationError):
                        safe.update(code=error.code,details=error.details)
                    cache[key] = {'status':'started','call_id':call.call_id,'error':safe,
                                  'elapsed_seconds':time.monotonic()-started}
                    raise
                cache[key] = {"status": "complete", "call_id": call.call_id,
                              "result": result, "elapsed_seconds": time.monotonic() - started}
            summaries[call.call_id] = _summary(result)
            usage.append({"kind": "summary", "call_id": call.call_id, "cached": cached,
                          "usage": result.get("usage", {}), "model": plan.summary_model,
                          "elapsed_seconds": cache[key].get("elapsed_seconds")})
        cards, vectors = {}, {}
        for frozen_card in plan.cards:
            card = thaw(frozen_card)
            card.update(summary=summaries[plan.final_calls[card["node_id"]]],
                        summary_model=plan.summary_model, prompt_hash=plan.prompt_hash, summary_status="ready")
            cards[card["node_id"]] = card
        requests = {}
        for identifier, card in cards.items():
            key = "embedding:" + digest([SCHEMA_VERSION, identifier, card["source_hash"],
                                         card["source_map_hash"], card["summary"],
                                         embedder.model_version, embedder.dimension])
            requests[key] = (identifier, key, card["summary"])
            if key in cache:
                # Old vector-only caches cannot establish original paid usage.
                # Fail closed instead of silently replaying or pricing as free.
                raise ValueError("EMBEDDING_CACHE_USAGE_MISSING")
        covered = set()
        for cache_key in cache:
            if not cache_key.startswith("embedding_batch:"):
                continue
            entry = cache[cache_key]
            keys = entry.get("keys")
            if (not isinstance(keys, list) or not keys or len(set(keys)) != len(keys)
                    or any(not isinstance(key, str) for key in keys)):
                raise ValueError("NAVIGATION_EMBEDDING_INVALID")
            relevant = set(keys) & requests.keys()
            if not relevant:
                continue
            if entry.get("status") != "complete":
                raise ValueError("EMBEDDING_CACHE_ATTEMPT_UNRESOLVED")
            expected_id = digest([SCHEMA_VERSION, embedder.model_version, embedder.dimension, keys])
            if (cache_key != "embedding_batch:" + expected_id or entry.get("call_id") != expected_id
                    or entry.get("model") != embedder.model_version
                    or entry.get("dimension") != embedder.dimension or covered & relevant):
                raise ValueError("NAVIGATION_EMBEDDING_INVALID")
            output = entry.get("vectors")
            if (not isinstance(output, list) or len(output) != len(keys)
                    or any(not valid_vector(vector, embedder.dimension) for vector in output)):
                raise ValueError("NAVIGATION_EMBEDDING_INVALID")
            for key, vector in zip(keys, output):
                if key in relevant:
                    vectors[requests[key][0]] = vector
            covered.update(relevant)
            usage.append({"kind": "embedding", "call_id": expected_id, "cached": True,
                          "model": entry["model"], "input_count": len(keys),
                          "usage": entry.get("usage"), "elapsed_seconds": entry.get("elapsed_seconds")})
        missing = [item for key, item in requests.items() if key not in covered]
        for start in range(0, len(missing), 10):
            batch = missing[start:start + 10]
            keys = [item[1] for item in batch]
            call_id = digest([SCHEMA_VERSION, embedder.model_version, embedder.dimension, keys])
            key = "embedding_batch:" + call_id
            entry = {"status": "started", "call_id": call_id, "keys": keys,
                     "model": embedder.model_version, "dimension": embedder.dimension}
            cache[key] = entry
            started = time.monotonic()
            output = embedder.embed([item[2] for item in batch])
            if (not isinstance(output, list) or len(output) != len(batch)
                    or any(not valid_vector(v, embedder.dimension) for v in output)):
                raise ValueError("NAVIGATION_EMBEDDING_INVALID")
            elapsed = time.monotonic() - started
            observed_usage = getattr(embedder, "last_usage", None)
            # One durable completion contains all vectors and original usage.
            # A failure before this replacement leaves a non-replayable start.
            cache[key] = {**entry, "status": "complete", "vectors": output,
                          "usage": observed_usage, "elapsed_seconds": elapsed}
            for (identifier, _, _), vector in zip(batch, output):
                vectors[identifier] = vector
            usage.append({"kind": "embedding", "call_id": call_id, "cached": False, "model": embedder.model_version,
                          "input_count": len(batch), "usage": observed_usage,
                          "elapsed_seconds": elapsed})
        return cls(plan, freeze(cards), freeze(vectors), embedder.model_version, embedder.dimension, freeze(usage))

    def validate(self, rows):
        for identifier, fingerprint in _fingerprints(list(rows)).items():
            if self.plan.source_fingerprints.get(identifier) != fingerprint:
                raise ValueError("NAVIGATION_SOURCE_MISMATCH")
        return True

    def search(self, query, query_vector, *, scope_snapshot_id, allowed_leaf_ids=None, limit=12):
        validate_limit(limit)
        if scope_snapshot_id not in self.plan.scope_snapshot_ids:
            raise ValueError("NAVIGATION_SCOPE_MISMATCH")
        if len(self.plan.scope_snapshot_ids) > 1 and allowed_leaf_ids is None:
            raise ValueError("NAVIGATION_SCOPE_MISMATCH")
        if not valid_vector(query_vector, self.dimension):
            raise ValueError("NAVIGATION_EMBEDDING_INVALID")
        allowed = set(self.plan.source_fingerprints) if allowed_leaf_ids is None else set(allowed_leaf_ids)
        cards = {i: c for i, c in self._cards.items() if set(c["ordered_leaf_ids"]) <= allowed}
        if not cards:
            return []
        lexical = BM25Index([{"chunk_id": i, "retrieval_text": c["title"] + "\n" + c["summary"]}
                             for i, c in cards.items()])
        lexical_rank = lexical.search(query, cards, limit=min(1000, len(cards)))
        q = normalized(query_vector)
        dense_rank = sorted([(i, sum(a * b for a, b in zip(q, normalized(self._embeddings[i]))))
                             for i in cards], key=lambda item: (-item[1], item[0]))
        ranking = reciprocal_rank_fusion([lexical_rank, dense_rank], limit=limit)
        return [thaw(cards[i]) for i, _ in ranking]

    def _artifact(self):
        return {"schema_version": SCHEMA_VERSION, "cards": self.cards,
                "embeddings": thaw(self._embeddings), "embedding_model": self.embedding_model,
                "dimension": self.dimension, "usage": thaw(self.usage),
                "source_fingerprints": thaw(self.plan.source_fingerprints),
                "nodes_hash": self.plan.nodes_hash, "scope_snapshot_ids": list(self.plan.scope_snapshot_ids),
                "manifest_ids": list(self.plan.manifest_ids), "summary_model": self.plan.summary_model,
                "prompt_hash": self.plan.prompt_hash, "plan_report": self.plan.report}

    def save(self, path):
        path = Path(path)
        artifact = self._artifact()
        artifact["index_id"] = digest(artifact)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(_json(artifact) + "\n", encoding="utf-8")
        temporary.replace(path)

    @classmethod
    def load(cls, path, *, rows, nodes, scope_snapshot_ids, manifest_ids,
             summary_model=None, prompt_hash=None, embedding_model=None, dimension=None):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        identifier = data.pop("index_id", None)
        if data.get("schema_version") != SCHEMA_VERSION or digest(data) != identifier:
            raise ValueError("NAVIGATION_ARTIFACT_INVALID")
        if (sorted(set(scope_snapshot_ids)) != data["scope_snapshot_ids"]
                or sorted(set(manifest_ids)) != data["manifest_ids"]):
            raise ValueError("NAVIGATION_SCOPE_MISMATCH")
        for expected, actual in ((summary_model, data["summary_model"]), (prompt_hash, data["prompt_hash"]),
                                 (embedding_model, data["embedding_model"]), (dimension, data["dimension"])):
            if expected is not None and expected != actual:
                raise ValueError("NAVIGATION_PROFILE_MISMATCH")
        rows, nodes = list(rows), list(nodes)
        if (_fingerprints(rows) != data["source_fingerprints"]
                or digest(sorted(nodes, key=lambda n: n["node_id"])) != data["nodes_hash"]):
            raise ValueError("NAVIGATION_SOURCE_MISMATCH")
        plan = plan_navigation(rows, nodes, scope_snapshot_ids=scope_snapshot_ids, manifest_ids=manifest_ids,
                               summary_model=data["summary_model"], prompt_hash=data["prompt_hash"])
        expected_ids = {c["node_id"] for c in plan.cards}
        if expected_ids != set(data["cards"]) or expected_ids != set(data["embeddings"]):
            raise ValueError("NAVIGATION_ARTIFACT_INVALID")
        for original in plan.cards:
            card = data["cards"][original["node_id"]]
            if any(card.get(k) != thaw(v) for k, v in original.items()):
                raise ValueError("NAVIGATION_SOURCE_MISMATCH")
            _summary({"summary": card.get("summary")})
            if (card.get("summary_status") != "ready" or card.get("summary_model") != data["summary_model"]
                    or card.get("prompt_hash") != data["prompt_hash"]
                    or not valid_vector(data["embeddings"][card["node_id"]], data["dimension"])):
                raise ValueError("NAVIGATION_ARTIFACT_INVALID")
        return cls(plan, freeze(data["cards"]), freeze(data["embeddings"]),
                   data["embedding_model"], data["dimension"], freeze(data["usage"]))
