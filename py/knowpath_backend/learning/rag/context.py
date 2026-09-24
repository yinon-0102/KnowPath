"""Select whole evidence dependency groups, never trim away conditions."""
from copy import deepcopy
import re


def evidence_memberships(rows):
    """Union canonical evidence groups with locally validated reading packets."""
    buckets = {}
    for row in rows:
        for field in ('evidence_group', 'atomic_group'):
            if row.get(field):
                buckets.setdefault((field, row[field]), set()).add(row['chunk_id'])
    return {row['chunk_id']: set().union(*(buckets.get((field, row.get(field)), set())
                for field in ('evidence_group', 'atomic_group'))) for row in rows}


_ARTICLE_ANCHOR = re.compile(r"第\s*([0-9]{1,4}|[零〇一二两三四五六七八九十百千万亿]{1,16})\s*条")
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}


def _article_number(value):
    if value.isdecimal():
        return int(value)
    total, section, digit = 0, 0, 0
    for char in value:
        if char in _CN_DIGITS:
            digit = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            if unit < 10000:
                section += (digit or 1) * unit
            else:
                total += section + digit
                total *= unit
                section, digit = 0, 0
        else:
            return None
        if char in _CN_UNITS and char not in {"万", "亿"}:
            digit = 0
    return total + section + digit


def _article_anchors(text):
    if not isinstance(text, str):
        return set()
    return {number for match in _ARTICLE_ANCHOR.finditer(text)
            if (number := _article_number(match.group(1))) is not None}


def explicit_anchor_rows(question, rows):
    """Return retrieved rows whose text contains an article named by question."""
    anchors = _article_anchors(question)
    if not anchors:
        return []
    return [row for row in rows
            if anchors.intersection(_article_anchors("\n".join(
                str(row.get(key, "")) for key in ("source_text", "retrieval_text"))))]


def prioritize_explicit_anchors(question, rows):
    """Stable-promote retrieved rows explicitly named by the question.

    This is a hard evidence-preservation rule, not a relevance score. It only
    reorders rows already returned by retrieval and never loads a new source.
    """
    rows = list(rows)
    matched_ids = {row.get("chunk_id") for row in explicit_anchor_rows(question, rows)}
    if not matched_ids:
        return rows
    matched = [row for row in rows if row.get("chunk_id") in matched_ids]
    unmatched = [row for row in rows if row.get("chunk_id") not in matched_ids]
    return matched + unmatched


def anchor_repacking_plan(question, rows, baseline_rows, *, omitted_chunk_ids=None,
                          invalid_chunk_ids=()):
    """Plan one rescue after the largest dependency-closed proper prefix.

    Both directed requirements and complete evidence groups are resolved from
    all supplied candidates, never from the already packed baseline subset.
    A zero boundary cannot protect the primary evidence and disables rescue.
    """
    rows = list(rows)
    baseline_ids = [row["chunk_id"] for row in baseline_rows]
    baseline_set = set(baseline_ids)
    by_id = {row["chunk_id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("duplicate context identity")
    if len(baseline_set) != len(baseline_ids):
        raise ValueError("duplicate baseline identity")
    if not baseline_set <= by_id.keys():
        raise ValueError("baseline identity missing from candidates")
    groups = evidence_memberships(rows)

    def closure(identifier):
        pending, required = [identifier], set()
        while pending:
            current = pending.pop()
            if current in required:
                continue
            required.add(current)
            item = by_id.get(current)
            if item is None:
                return None
            pending.extend(item.get("requires", ()))
            pending.extend(groups.get(current, ()))
        return required

    omitted = set(by_id) - baseline_set if omitted_chunk_ids is None else set(omitted_chunk_ids)
    invalid = set(invalid_chunk_ids)
    anchors = [row for row in explicit_anchor_rows(question, rows)
               if row["chunk_id"] in omitted and row["chunk_id"] not in baseline_set]
    decision = {"reason": None, "attempted": False, "protected_chunk_ids": [],
                "missing_anchor_ids": [], "invalid_anchor_ids": [], "added_anchor_ids": []}
    if not anchors:
        decision["reason"] = "no_missing_anchor"
        return rows, decision
    valid_anchors = []
    for row in anchors:
        identifier = row["chunk_id"]
        if identifier in invalid or closure(identifier) is None:
            decision["invalid_anchor_ids"].append(identifier)
        else:
            valid_anchors.append(row)
    decision["missing_anchor_ids"] = [row["chunk_id"] for row in valid_anchors]
    if not valid_anchors:
        decision["reason"] = "invalid_dependency"
        return rows, decision

    prefix, required, insertion = set(), set(), 0
    for k, identifier in enumerate(baseline_ids[:-1], start=1):
        dependencies = closure(identifier)
        if dependencies is None:
            break
        prefix.add(identifier)
        required.update(dependencies)
        if required <= prefix:
            insertion = k
    if not insertion:
        decision["reason"] = "no_preservable_prefix"
        return rows, decision
    decision["protected_chunk_ids"] = baseline_ids[:insertion]
    baseline = [by_id[identifier] for identifier in baseline_ids]
    ordered, seen = [], set()
    for row in baseline[:insertion] + valid_anchors + baseline[insertion:] + rows:
        if row["chunk_id"] not in seen:
            seen.add(row["chunk_id"])
            ordered.append(row)
    return ordered, decision


def preserve_baseline_with_anchors(question, rows, baseline_rows):
    """Stable-promote valid missing anchors after a safe baseline boundary."""
    return anchor_repacking_plan(question, rows, baseline_rows)[0]


def conservative_tokens(text):
    return len(text.encode("utf-8"))


def assemble_context(rows, *, max_tokens, count_tokens=conservative_tokens):
    if type(max_tokens) is not int or max_tokens < 0:
        raise ValueError("context budget must be nonnegative")
    by_id = {r["chunk_id"]: r for r in rows}
    if len(by_id) != len(rows):
        raise ValueError("duplicate context identity")
    groups = evidence_memberships(rows)
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
            pending.extend(groups.get(identifier, set()) - required)
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
