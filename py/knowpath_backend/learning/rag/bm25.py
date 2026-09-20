"""Okapi BM25 over a versioned exploratory Chinese lexical tokenizer.

Han unigrams and adjacent bigrams are lexical features, not semantic word
segmentation. Latin identifiers, decimal numbers, and formula operators survive.
"""
from __future__ import annotations

from collections import Counter
import math
import re

from .retrieval import RetrievalError, content_hash

TOKENIZER_VERSION = "han-lexical-exploratory-v1"
_PARTS = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0003134f]+|[a-z_][a-z0-9_]*|\d+(?:\.\d+)?|[=+*/^<>≤≥≠√∑∫−-]", re.IGNORECASE)
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0003134f]")


def tokenize(text):
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    result = []
    for token in _PARTS.findall(text.casefold()):
        if _HAN.match(token):
            result.extend(token)
            result.extend(token[i:i + 2] for i in range(len(token) - 1))
        else:
            result.append(token)
    return result


def validate_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("candidate limit must be an integer between 1 and 1000")


class BM25Index:
    def __init__(self, chunks, profile=None):
        if profile is not None and not isinstance(profile, dict):
            raise ValueError("BM25 profile must be an object")
        self.profile = {"tokenizer": TOKENIZER_VERSION, "k1": 1.5, "b": .75}
        if profile:
            if set(profile) - set(self.profile):
                raise ValueError("unknown BM25 profile field")
            self.profile.update(profile)
        self.k1, self.b = self.profile["k1"], self.profile["b"]
        if (self.profile["tokenizer"] != TOKENIZER_VERSION
                or type(self.k1) not in (int, float) or not math.isfinite(self.k1) or not 0 < self.k1 <= 100
                or type(self.b) not in (int, float) or not math.isfinite(self.b) or not 0 <= self.b <= 1):
            raise ValueError("unsupported BM25 profile")
        self._terms, self._lengths, self._hashes = {}, {}, {}
        for row in chunks:
            identifier = row["chunk_id"]
            if not isinstance(identifier, str) or not identifier or identifier in self._terms:
                raise ValueError("chunk IDs must be unique nonempty strings")
            terms = Counter(tokenize(row["retrieval_text"]))
            self._terms[identifier] = terms
            self._lengths[identifier] = sum(terms.values())
            self._hashes[identifier] = content_hash(row["retrieval_text"])

    def verify(self, chunks):
        receipt, seen = [], set()
        for row in chunks:
            identifier = row["chunk_id"]
            digest = content_hash(row["retrieval_text"])
            if identifier in seen or self._hashes.get(identifier) != digest:
                raise RetrievalError("BM25_INDEX_NOT_READY")
            seen.add(identifier)
            receipt.append({"chunk_id": identifier, "content_hash": digest})
        return {"verified": True, "profile": dict(self.profile), "chunks": receipt}

    def search(self, query, allowed_ids, limit=30):
        validate_limit(limit)
        query_terms = set(tokenize(query))
        # Both document frequency and average length use only the allowed corpus.
        allowed = sorted(set(allowed_ids) & self._terms.keys())
        if not allowed or not query_terms:
            return []
        average = sum(self._lengths[i] for i in allowed) / len(allowed)
        if not average:
            return []
        frequency = Counter(term for i in allowed for term in self._terms[i] if term in query_terms)
        inverse = {term: math.log1p((len(allowed) - n + .5) / (n + .5)) for term, n in frequency.items()}
        scores = []
        for identifier in allowed:
            norm = self.k1 * (1 - self.b + self.b * self._lengths[identifier] / average)
            score = sum(inverse[term] * count * (self.k1 + 1) / (count + norm)
                        for term, count in self._terms[identifier].items() if term in inverse)
            if score > 0:
                scores.append((identifier, score))
        return sorted(scores, key=lambda item: (-item[1], item[0]))[:limit]
