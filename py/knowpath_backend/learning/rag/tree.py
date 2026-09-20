"""B1: bounded, nonrecursive structural extension of identical hybrid recall."""
from __future__ import annotations

from collections import Counter, defaultdict

from .bm25 import tokenize
from .contracts import Candidate
from .plugins import OrdinaryPlugin, check_deadline


def _parent_key(row):
    parent = row.get("parent_id")
    return (row["retrieval_version_id"], row["material_version_id"], parent) if parent else None


class TreePlugin(OrdinaryPlugin):
    name = "tree"

    def retrieve(self, request, chunks):
        rows = list(chunks)
        base = super().retrieve(request, rows)
        check_deadline(request)
        by_id = {row["chunk_id"]: row for row in rows}
        total_limit = min(40, request.budget.rerank_candidates)
        seed_limit = min(30, total_limit)
        extension_limit = min(10, max(0, total_limit - seed_limit))
        trace = base["trace"]
        trace.update(seeds=[], extensions=[], fill=[], rejected=[])
        trace["budgets"].update(seeds=seed_limit, extensions=extension_limit, per_parent=3)
        selected, selected_ids, parent_counts = [], set(), Counter()

        def reject(identifier, reason, seed_id=None):
            trace["rejected"].append({"chunk_id": identifier, "reason": reason, "seed_id": seed_id})

        def add(candidate, origin, seed_id=None, reason=None):
            identifier = candidate["chunk_id"]
            row = by_id[identifier]
            if identifier in selected_ids:
                return False
            parent = _parent_key(row)
            if origin == "extensions" and parent is not None and parent_counts[parent] >= 3:
                reject(identifier, "parent_limit", seed_id)
                return False
            if len(selected) >= total_limit:
                return False
            selected_ids.add(identifier)
            if origin == "extensions" and parent is not None:
                parent_counts[parent] += 1
            selected.append(dict(candidate, rank=len(selected) + 1))
            entry = {"chunk_id": identifier, "parent_id": row.get("parent_id")}
            if seed_id is not None:
                entry.update(seed_id=seed_id, reason=reason)
            trace[origin].append(entry)
            return True

        for candidate in base["candidates"]:
            if len(trace["seeds"]) >= seed_limit:
                break
            add(candidate, "seeds")

        # Build relationships only from already authorized rows. Never load a
        # target by ID from storage, and never expand a newly added extension.
        continuations, adjacent = defaultdict(list), defaultdict(list)
        for row in rows:
            if isinstance(row.get("continuation_of"), str):
                continuations[row["continuation_of"]].append(row["chunk_id"])
            if _parent_key(row) is not None and type(row.get("ordinal")) is int:
                adjacent[(_parent_key(row), row["ordinal"])].append(row["chunk_id"])
        query_terms = set(tokenize(request.query))
        all_proposals = []
        priority = {"requires": 0, "continuation_of": 1, "adjacent_lexical": 2}
        for seed_rank, seed in enumerate(trace["seeds"]):
            check_deadline(request)
            seed_id = seed["chunk_id"]
            source = by_id[seed_id]
            proposals = []
            requires = source.get("requires", [])
            if isinstance(requires, (list, tuple)):
                proposals.extend((target, "requires") for target in requires if isinstance(target, str))
            if isinstance(source.get("continuation_of"), str):
                proposals.append((source["continuation_of"], "continuation_of"))
            proposals.extend((target, "continuation_of") for target in sorted(continuations[seed_id]))
            if _parent_key(source) is not None and type(source.get("ordinal")) is int:
                for ordinal in (source["ordinal"] - 1, source["ordinal"] + 1):
                    proposals.extend((target, "adjacent_lexical") for target in sorted(adjacent[(_parent_key(source), ordinal)]))
            for identifier, reason in proposals:
                ordinal = by_id.get(identifier, {}).get("ordinal", 0)
                all_proposals.append((priority[reason], seed_rank, ordinal, identifier, reason, seed_id))
        proposed = set()
        for _, _, _, identifier, reason, seed_id in sorted(all_proposals):
                check_deadline(request)
                if identifier in selected_ids or (seed_id, identifier) in proposed:
                    continue
                proposed.add((seed_id, identifier))
                source = by_id[seed_id]
                if len(trace["extensions"]) >= extension_limit or len(selected) >= total_limit:
                    reject(identifier, "extension_budget", seed_id)
                    continue
                target = by_id.get(identifier)
                if target is None:
                    # Do not expose a disallowed target's contents in tracing.
                    reject(identifier, "outside_scope", seed_id)
                    continue
                if _parent_key(source) is None or _parent_key(source) != _parent_key(target):
                    reject(identifier, "different_or_unknown_parent_version", seed_id)
                    continue
                if reason == "adjacent_lexical" and not query_terms.intersection(tokenize(target["source_text"])):
                    reject(identifier, "no_query_overlap", seed_id)
                    continue
                candidate = Candidate(chunk_id=identifier, retrieval_version_id=target["retrieval_version_id"],
                    material_version_id=target["material_version_id"], parent_id=target.get("parent_id"), channel="tree", rank=1).model_dump()
                add(candidate, "extensions", seed_id, reason)
        for candidate in base["candidates"]:
            if len(selected) >= total_limit:
                break
            add(candidate, "fill")
        check_deadline(request)
        trace["extra_read_count"] = len(trace["extensions"])
        return {"candidates": selected, "trace": trace}
