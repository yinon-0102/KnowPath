"""Deterministic semantic retrieval units mapped to immutable source artifacts."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable

from .parsing import MappedDocument, semantic_units

DEFAULT_MAX_TOKENS = 512
DEFAULT_TOKENIZER = "utf8-byte-upper-bound-v1"


class SemanticChunkingError(ValueError):
    """A source unit cannot fit the selected profile without losing evidence."""


def default_count_tokens(text: str) -> int:
    """Conservative explicit fallback; production may inject its real tokenizer."""
    return len(text.encode("utf-8"))


def _identifier(*values):
    encoded = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _inline_atomic(text, offset):
    # Never cut through inline code, dollar math, or LaTeX math delimiters.
    pattern = r"(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$|(?<!\\)\$(?!\$)[^\n$]+?(?<!\\)\$|`+[^`\n]+`+|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]"
    return [(offset + m.start(), offset + m.end()) for m in re.finditer(pattern, text)]


def _bounded(document, unit, max_tokens, count_tokens):
    text = document.text[unit.start:unit.end]
    atoms = [(unit.start, unit.end)] if unit.atomic else _inline_atomic(text, unit.start)
    for a, b in atoms:
        if count_tokens(document.source_text(a, b)) > max_tokens:
            raise SemanticChunkingError("atomic code/math unit exceeds max_tokens")
    if count_tokens(document.source_text(unit.start, unit.end)) <= max_tokens:
        return [(unit.start, unit.end)]
    # Prefer natural paragraphs first, then sentence endings. Only exceptionally
    # long unbroken prose uses character fallback; semantic units are never padded.
    paragraphs = [unit.start + m.end() for m in re.finditer(r"\n\s*\n", text)]
    sentences = [unit.start + m.end() for m in re.finditer(r"[。！？!?；;]+[”’」』\"']*|\.(?:\s+|$)", text)]
    def safe(position):
        return not any(a < position < b for a, b in atoms)
    result = []
    cursor = unit.start
    while cursor < unit.end:
        if not document.text[cursor:unit.end].strip():
            # Keep real trailing whitespace when affordable, otherwise it is not evidence.
            if result and count_tokens(document.source_text(result[-1][0], unit.end)) <= max_tokens:
                result[-1] = (result[-1][0], unit.end)
            break
        if count_tokens(document.source_text(cursor, unit.end)) <= max_tokens:
            result.append((cursor, unit.end))
            break
        chosen = None
        for boundaries in (paragraphs, sentences):
            fitting = [p for p in boundaries if cursor < p <= unit.end and safe(p)
                       and count_tokens(document.source_text(cursor, p)) <= max_tokens]
            if fitting:
                chosen = max(fitting)
                break
        if chosen is None:
            # Counts need not be perfectly additive or monotonic. Scan bounded
            # prefixes and verify the actual selected source text on each step.
            for p in range(cursor + 1, unit.end + 1):
                if not safe(p):
                    continue
                if count_tokens(document.source_text(cursor, p)) > max_tokens:
                    break
                chosen = p
        if chosen is None:
            raise SemanticChunkingError("one source character or atomic unit exceeds max_tokens")
        result.append((cursor, chosen))
        cursor = chosen
    return result


def semantic_chunks(version, retrieval_version_id: str, *, max_tokens: int = DEFAULT_MAX_TOKENS,
                    count_tokens: Callable[[str], int] = default_count_tokens,
                    document_title: str = "") -> list[dict]:
    """Build auditable chunks; the budget bounds source text, excluding metadata.

    IDs bind to retrieval version, immutable source coordinates, and exact text.
    Metadata prefixes are retrieval hints only and never appear in source_text.
    """
    if not retrieval_version_id or type(max_tokens) is not int or max_tokens <= 0:
        raise ValueError("retrieval_version_id and positive integer max_tokens required")
    def count(text):
        value = count_tokens(text)
        if type(value) is not int or value < 0:
            raise ValueError("count_tokens must return a nonnegative integer")
        return value
    document = MappedDocument(version)
    chunks = []
    for unit in semantic_units(document, version.filename):
        first = None
        parent = (_identifier("section", retrieval_version_id, version.id, unit.section_path)
                  if unit.section_path else None)
        for start, end in _bounded(document, unit, max_tokens, count):
            source = document.source_text(start, end)
            if not source.strip():
                continue
            spans = [span.model_dump() for span in document.spans(start, end)]
            identifier = _identifier(retrieval_version_id, spans, source)
            prefix = " > ".join(x for x in (document_title, *unit.section_path) if x)
            chunks.append(dict(retrieval_version_id=retrieval_version_id, chunk_id=identifier,
                material_version_id=version.id, source_text=source,
                retrieval_text=(prefix + "\n" if prefix else "") + source,
                source_spans=spans, section_path=list(unit.section_path), parent_id=parent,
                ordinal=len(chunks), continuation_of=first,
                quality={"structure": "known" if unit.section_path else "unknown",
                         'content_status': 'damaged' if unit.damaged or '\ufffd' in source else 'unchecked',
                         "unit_kind": unit.kind, "atomic": unit.atomic, "token_count": count(source)}))
            if first is None:
                first = identifier
    return chunks
