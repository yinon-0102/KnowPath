"""Scoped projections of durable messages, with a bounded untrusted prompt."""
import copy
import json
import re

from knowpath_backend.context.engine import ContextEngine, ContextSegment, count_tokens, bigram_relevance
from knowpath_backend.memory.backends.inmemory import InMemoryVectorBackend
from knowpath_backend.memory.item import MemoryItem
from knowpath_backend.memory.summary import SummaryMemory


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
    return {"history": history, "memory": {"summary": summary, "recall": recall}}


def prompt_data(snapshot):
    return {k: v for k, v in snapshot.items() if k not in {"context_report", "profile_candidates", "retrieval_sources"}}


def prompt_tokens(snapshot):
    from .message_generation import SYSTEM_PROMPT
    return count_tokens(SYSTEM_PROMPT) + count_tokens(json.dumps(prompt_data(snapshot), ensure_ascii=False)) + 32


def bound_snapshot(snapshot, *, budget):
    from .message_generation import MessageGenerationError
    result = copy.deepcopy(snapshot)
    result.pop("context_report", None)
    history = result.get("history", [])
    memory = result.get("memory", {})
    result["history"] = []
    if "memory" in result:
        result["memory"] = {"summary": {}, "recall": []}
    sources = result["sources"]
    original_source_count = len(sources)
    while len(sources) > 1 and prompt_tokens(result) > budget:
        sources.pop()
    while sources and prompt_tokens(result) > budget and len(sources[0]["text"]) > 128:
        sources[0]["text"] = sources[0]["text"][:max(128, len(sources[0]["text"]) // 2)]
    if not sources or prompt_tokens(result) > budget:
        raise MessageGenerationError("CONTEXT_BUDGET_EXCEEDED")

    candidates = []
    # Whole turns avoid a retained answer without its question. Provenance stays
    # outside the dedup key so identical turns can collapse across conversations.
    for pos in range(0, len(history), 2):
        turn = history[pos:pos + 2]
        candidates.append(("history", turn, json.dumps(turn, ensure_ascii=False), 0.8 + pos / 1000))
    for row in memory.get("recall", []):
        text = json.dumps([{"role": "user", "content": row["user"]},
                           {"role": "assistant", "content": row["assistant"]}], ensure_ascii=False)
        candidates.append(("recall", row, text, 0.7))
    if memory.get("summary"):
        summary = memory["summary"]
        candidates.append(("summary", summary, json.dumps(summary, ensure_ascii=False), 0.6))
    segments = [ContextSegment("web-data", "user", text, priority,
                               count_tokens(json.dumps(value, ensure_ascii=False)) + 32,
                               False, 1, seq=i)
                for i, (_, value, text, priority) in enumerate(candidates)]
    engine = ContextEngine()
    selected = engine.build(segments, budget=max(0, budget - prompt_tokens(result)))
    kept = {message["content"] for message in selected.messages}
    used_keys = set()
    for kind, value, text, _ in candidates:
        if text not in kept or text in used_keys:
            continue
        before = copy.deepcopy(result)
        if kind == "history":
            result["history"].extend(value)
        elif kind == "recall":
            result["memory"]["recall"].append(value)
        else:
            result["memory"]["summary"] = value
        if prompt_tokens(result) > budget:
            result = before
        used_keys.add(text)
    result["context_report"] = {"budget_tokens": budget, "estimated_tokens": prompt_tokens(result),
                                "deduplicated": len(segments) - len(engine._dedup(segments)),
                                "dropped_sources": original_source_count - len(sources),
                                "dropped_context": len(candidates) - len(used_keys)}
    return result
