"""Reciprocal rank fusion; raw channel score scales never mix."""
from collections import defaultdict
import math

from .bm25 import validate_limit


def reciprocal_rank_fusion(rankings, limit=40, k=60, weights=None):
    validate_limit(limit)
    if type(k) not in (int, float) or not math.isfinite(k) or k < 0:
        raise ValueError("RRF k must be finite and nonnegative")
    rankings = list(rankings)
    weights = [1.] * len(rankings) if weights is None else list(weights)
    if (len(weights) != len(rankings) or any(type(w) not in (int, float)
            or not math.isfinite(w) or w < 0 for w in weights)):
        raise ValueError("RRF requires one finite nonnegative weight per channel")
    scores = defaultdict(float)
    for ranking, weight in zip(rankings, weights):
        seen = set()
        for item in ranking:
            identifier = item if isinstance(item, str) else item[0]
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("candidate ID must be a nonempty string")
            if identifier in seen:
                continue
            seen.add(identifier)
            if weight:
                scores[identifier] += weight / (k + len(seen))
                if not math.isfinite(scores[identifier]):
                    raise ValueError("RRF score overflow")
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
