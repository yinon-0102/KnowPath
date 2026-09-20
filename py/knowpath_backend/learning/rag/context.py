"""Select whole evidence dependency groups, never trim away conditions."""
from copy import deepcopy


def conservative_tokens(text):
    return len(text.encode("utf-8"))


def assemble_context(rows, *, max_tokens, count_tokens=conservative_tokens):
    if type(max_tokens) is not int or max_tokens < 0:
        raise ValueError("context budget must be nonnegative")
    by_id = {r["chunk_id"]: r for r in rows}
    if len(by_id) != len(rows):
        raise ValueError("duplicate context identity")
    groups = {}
    for row in rows:
        if row.get("evidence_group"):
            groups.setdefault(row["evidence_group"], set()).add(row["chunk_id"])
    selected, consumed = [], 0
    seen = set()
    for row in rows:
        pending, required, invalid = [row["chunk_id"]], set(), False
        while pending:
            identifier = pending.pop()
            if identifier in required:
                continue
            required.add(identifier)
            item = by_id.get(identifier)
            if item is None:
                invalid = True
                break
            pending.extend(item.get("requires", []))
            pending.extend(groups.get(item.get("evidence_group"), set()) - required)
        if invalid:
            continue
        additions = [r for r in rows if r["chunk_id"] in required - seen]
        # Count separators as well; complete-model prompt cost is checked again
        # by the caller together with question/instructions/history/output room.
        cost = sum(count_tokens(r["source_text"]) for r in additions)
        cost += max(0, len(additions) - (0 if selected else 1)) * count_tokens("\n")
        if consumed + cost <= max_tokens:
            selected.extend(deepcopy(additions))
            seen.update(required)
            consumed += cost
    return selected
