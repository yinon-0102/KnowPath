"""Scoped projections of durable messages, with a bounded untrusted prompt."""
import copy
import json
import re

from knowpath_backend.context.engine import ContextEngine, ContextSegment, count_tokens, bigram_relevance
from knowpath_backend.memory.backends.inmemory import InMemoryVectorBackend
from knowpath_backend.memory.item import MemoryItem
from knowpath_backend.memory.summary import SummaryMemory
from knowpath_backend.learning.materials.source_access import original_source_refs
from knowpath_backend.learning.errors import DomainConflict


def _excerpt(text, limit=320):
    sentences = re.split(r"(?<=[.!?。！？])\s*|\n+", text.strip())
    return " ".join(sentences[:2])[:limit]


def _relevant_excerpt(text, query, limit=600):
    sentences = re.split(r"(?<=[.!?。！？])\s*|\n+", text.strip())
    windows = [sentence[start:start + limit] for sentence in sentences
               for start in range(0, len(sentence), max(1, limit // 2))]
    if not windows:
        return ""
    return max(windows, key=lambda part: bigram_relevance(part.casefold(), query.casefold()))


def project_memory(rows, space, conversation_id, query):
    """Only original completed turns enter the index; projections never recurse."""
    eligible = [r for r in rows if r["space_id"] == space["id"] and r["status"] == "completed"
                and r.get("response") and r["snapshot"].get("scope_version") == space["scope_version"]
                and r["snapshot"].get("bindings") == space["bindings"]]
    source_refs, safe = {}, []
    for row in eligible:
        try:
            message_source_refs(row, eligible, memo=source_refs)
            safe.append(row)
        except DomainConflict:
            # Old context with missing provenance must not silently enter a
            # fresh answer. Original messages remain available for inspection.
            continue
    eligible = safe
    own = sorted([r for r in eligible if r["conversation_id"] == conversation_id], key=lambda r: r["sequence"])
    recent = own[-5:]
    recent_ids = {r["id"] for r in recent}
    history = [part for r in recent for part in (
        {"role": "user", "content": r["message"]}, {"role": "assistant", "content": r["response"]["text"]})]
    index = InMemoryVectorBackend()
    records = {r["id"]: r for r in eligible if r["id"] not in recent_ids}
    for row in records.values():
        index.add(MemoryItem(id=row["id"], role="user", content=row["message"] + "\n" + row["response"]["text"]))
    recall = []
    for item, score in index.search(query, k=5):
        row = records[item.id]
        recall.append({"message_id": row["id"], "conversation_id": row["conversation_id"],
                       "user": _relevant_excerpt(row["message"], query),
                       "assistant": _relevant_excerpt(row["response"]["text"], query)})
    recalled = {r["message_id"] for r in recall}
    older = [r for r in own[:-5] if r["id"] not in recalled][-8:]
    summary = {}
    if older:
        # Bounded extractive compression: no inferred facts and no extra model call.
        tier = SummaryMemory(flush_threshold=100, max_tokens=2400,
                             summarizer=lambda batch, previous: "\n".join(item.content for item in batch))
        for row in older:
            tier.add(MemoryItem(role="user", content=f'{row["id"]}: user={_excerpt(row["message"])}; '
                               f'assistant={_excerpt(row["response"]["text"])}'))
        summary = {"method": "extractive", "text": tier.flush().content,
                   "message_ids": [r["id"] for r in older], "conversation_id": conversation_id}
    def refs(row):
        return source_refs[row["id"]]
    provenance = {"history": [refs(row) for row in recent],
                  "recall": {row["message_id"]: refs(records[row["message_id"]]) for row in recall},
                  "summary": [ref for row in older for ref in refs(row)]}
    return {"history": history, "memory": {"summary": summary, "recall": recall},
            "context_provenance": provenance}


def delivery_source_refs(snapshot):
    """Include original evidence carried by retained model context.

    Assistance conservatively covers every source supplied to the answer,
    including earlier explanations the model may reuse without reciting them.
    RAG receives history only; its unused recall/summary cannot taint evidence.
    """
    provenance = snapshot.get("context_provenance", {})
    refs = [*snapshot["sources"], *[ref for turn in provenance.get("history", []) for ref in turn]]
    if not snapshot.get("rag_mode"):
        refs.extend(ref for row in provenance.get("recall", {}).values() for ref in row)
        refs.extend(provenance.get("summary", []))
    return original_source_refs(refs)


def message_source_refs(message, rows=(), *, memo=None, visiting=None):
    """Recover pre-provenance messages from their retained original turns.

    Legacy history preserves exact turns; recall/summary preserve message IDs.
    Ambiguous identical turns contribute all predecessor source sets. Missing
    originals fail closed instead of treating reused help as independent.
    """
    memo = {} if memo is None else memo
    visiting = set() if visiting is None else visiting
    identifier = message["id"]
    def unavailable():
        return DomainConflict("MESSAGE_CONTEXT_UNAVAILABLE", "历史回答来源不可用，无法安全重放")
    if identifier in memo:
        if memo[identifier] is None:
            raise unavailable()
        return memo[identifier]
    if identifier in visiting:
        raise unavailable()
    visiting = visiting | {identifier}
    snapshot = message["snapshot"]
    if "delivered_source_refs" in snapshot:
        refs = original_source_refs(snapshot["delivered_source_refs"])
    elif "context_provenance" in snapshot:
        refs = delivery_source_refs(snapshot)
    else:
        refs = original_source_refs(snapshot["sources"])
        candidates = [row for row in rows if row["id"] != identifier and row["status"] == "completed"
            and row.get("response") and row["space_id"] == message["space_id"]
            and row["snapshot"].get("scope_version") == snapshot.get("scope_version")
            and row["snapshot"].get("bindings") == snapshot.get("bindings")
            and row["created_at"] <= message["created_at"]]
        by_id = {row["id"]: row for row in candidates}
        referenced = set()
        history = snapshot.get("history", [])
        if len(history) % 2:
            memo[identifier] = None
            raise unavailable()
        for pos in range(0, len(history), 2):
            matched = {row["id"] for row in candidates
                if row["conversation_id"] == message["conversation_id"] and row["sequence"] < message["sequence"]
                and row["message"] == history[pos]["content"]
                and row["response"]["text"] == history[pos + 1]["content"]}
            if not matched:
                memo[identifier] = None
                raise unavailable()
            referenced.update(matched)
        if not snapshot.get("rag_mode"):
            memory = snapshot.get("memory", {})
            referenced.update(row["message_id"] for row in memory.get("recall", []))
            referenced.update(memory.get("summary", {}).get("message_ids", []))
        if referenced - by_id.keys():
            memo[identifier] = None
            raise unavailable()
        for previous in referenced:
            refs.extend(message_source_refs(by_id[previous], rows, memo=memo, visiting=visiting))
        refs = original_source_refs(refs)
    memo[identifier] = refs
    return refs


def prompt_data(snapshot):
    return {k: v for k, v in snapshot.items() if k not in {"context_report", "profile_candidates", "retrieval_sources",
                                                        "context_provenance", "delivered_source_refs"}}


def prompt_tokens(snapshot):
    from knowpath_backend.learning.conversations.generation import SYSTEM_PROMPT
    return count_tokens(SYSTEM_PROMPT) + count_tokens(json.dumps(prompt_data(snapshot), ensure_ascii=False)) + 32


def bound_snapshot(snapshot, *, budget):
    from knowpath_backend.learning.conversations.generation import MessageGenerationError
    result = copy.deepcopy(snapshot)
    result.pop("context_report", None)
    history = result.get("history", [])
    memory = result.get("memory", {})
    provenance = result.get("context_provenance", {})
    result["history"] = []
    if "context_provenance" in result:
        result["context_provenance"] = {"history": [], "recall": {}, "summary": []}
    if "memory" in result:
        result["memory"] = {"summary": {}, "recall": []}
    sources = result["sources"]
    original_source_count = len(sources)
    while len(sources) > 1 and prompt_tokens(result) > budget:
        sources.pop()
    if not sources or prompt_tokens(result) > budget:
        raise MessageGenerationError("CONTEXT_BUDGET_EXCEEDED")

    candidates = []
    # Whole turns avoid a retained answer without its question. Provenance stays
    # outside the dedup key so identical turns can collapse across conversations.
    for pos in range(0, len(history), 2):
        turn = history[pos:pos + 2]
        refs = provenance.get("history", [])[pos // 2:pos // 2 + 1]
        candidates.append(("history", turn, json.dumps(turn, ensure_ascii=False), 0.8 + pos / 1000, refs))
    for row in memory.get("recall", []):
        text = json.dumps([{"role": "user", "content": row["user"]},
                           {"role": "assistant", "content": row["assistant"]}], ensure_ascii=False)
        candidates.append(("recall", row, text, 0.7, provenance.get("recall", {}).get(row["message_id"], [])))
    if memory.get("summary"):
        summary = memory["summary"]
        candidates.append(("summary", summary, json.dumps(summary, ensure_ascii=False), 0.6, provenance.get("summary", [])))
    segments = [ContextSegment("web-data", "user", text, priority,
                               count_tokens(json.dumps(value, ensure_ascii=False)) + 32,
                               False, 1, seq=i)
                for i, (_, value, text, priority, _) in enumerate(candidates)]
    engine = ContextEngine()
    selected = engine.build(segments, budget=max(0, budget - prompt_tokens(result)))
    kept = {message["content"] for message in selected.messages}
    used_keys = set()
    for kind, value, text, _, refs in candidates:
        if text not in kept or text in used_keys:
            continue
        before = copy.deepcopy(result)
        if kind == "history":
            result["history"].extend(value)
        elif kind == "recall":
            result["memory"]["recall"].append(value)
        else:
            result["memory"]["summary"] = value
        if "context_provenance" in result:
            if kind == "recall":
                result["context_provenance"][kind][value["message_id"]] = refs
            elif kind == "history":
                result["context_provenance"][kind].extend(refs)
            else:
                result["context_provenance"][kind] = refs
        if prompt_tokens(result) > budget:
            result = before
        used_keys.add(text)
    result["context_report"] = {"budget_tokens": budget, "estimated_tokens": prompt_tokens(result),
                                "deduplicated": len(segments) - len(engine._dedup(segments)),
                                "dropped_sources": original_source_count - len(sources),
                                "dropped_context": len(candidates) - len(used_keys)}
    return result
