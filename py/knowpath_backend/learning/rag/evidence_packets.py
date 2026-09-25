"""Canonical whole-unit evidence partitions; summaries never become evidence."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType

from .units import build_units
from .context import conservative_tokens


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze(v) for v in value)
    return value


def thaw(value):
    if hasattr(value, 'items'):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class EvidencePacket:
    packet_id: str
    parent_id: str
    retrieval_version_id: str
    material_version_id: str
    tree_version_id: str
    unit_ids: tuple[str, ...]
    leaf_ids: tuple[str, ...]
    source_spans: tuple
    source_map: tuple
    source_text: str
    source_hash: str
    source_map_hash: str
    token_count: int
    ordinal: int


@dataclass(frozen=True)
class PacketBuild:
    packets: tuple[EvidencePacket, ...]
    unavailable: tuple


def _packet(units, by_id):
    first = units[0]
    ids = tuple(i for unit in units for i in unit.leaf_ids)
    source_map = [{'leaf_id': i, 'source_spans': by_id[i]['source_spans']} for i in ids]
    text = '\n'.join(by_id[i]['source_text'] for i in ids)
    source_hash, mapping_hash = digest(text), digest(source_map)
    identity = ['b4-packet-v1', first.retrieval_version_id, first.material_version_id,
                first.tree_version_id, first.parent_id, ids, source_hash, mapping_hash]
    return EvidencePacket(digest(identity), first.parent_id, first.retrieval_version_id,
                          first.material_version_id, first.tree_version_id,
                          tuple(unit.unit_id for unit in units), ids,
                          freeze([span for item in source_map for span in item['source_spans']]),
                          freeze(source_map), text, source_hash, mapping_hash,
                          conservative_tokens(text), first.ordinal)


def build_packets(rows, *, tree_version_id=None):
    rows = list(rows)
    units = build_units(rows, tree_version_id=tree_version_id)
    by_id = {row['chunk_id']: row for row in rows}
    packets, unavailable, pending = [], [], []
    count, tokens, parent = 0, 0, None
    for unit in units:
        # Count the same complete original text used by context packing.
        # Cached quality counts can be stale and omit continuation separators.
        unit_tokens = conservative_tokens(unit.source_text)
        key = (unit.retrieval_version_id, unit.material_version_id, unit.tree_version_id, unit.parent_id)
        reason = ('structure_unavailable' if not unit.parent_id else
                  'oversized' if len(unit.leaf_ids) > 8 or unit_tokens > 1600 else None)
        if pending and (reason or key != parent or count + len(unit.leaf_ids) > 8
                        or tokens + conservative_tokens('\n') + unit_tokens > 1600):
            packets.append(_packet(pending, by_id))
            pending, count, tokens = [], 0, 0
        if reason:
            unavailable.append(freeze({'unit_id': unit.unit_id, 'leaf_ids': unit.leaf_ids, 'reason': reason}))
            continue
        tokens += (conservative_tokens('\n') if pending else 0) + unit_tokens
        pending.append(unit)
        parent, count = key, count + len(unit.leaf_ids)
    if pending:
        packets.append(_packet(pending, by_id))
    return PacketBuild(tuple(packets), tuple(unavailable))


def reconstruct_packet(packet, rows):
    """Compare against the canonical partition of the complete authorized rows."""
    candidates = {p.packet_id: p for p in build_packets(rows).packets}
    current = candidates.get(packet.packet_id)
    if current != packet:
        raise ValueError('PACKET_SOURCE_MISMATCH')
    return current
