from copy import deepcopy

import pytest

from knowpath_backend.learning.rag import evidence_packets as ep
from knowpath_backend.test.test_rag_b3 import leaf


def rows(count=10):
    return [leaf(str(i), '原文' + str(i), ordinal=i) for i in range(count)]


def test_greedy_packets_preserve_full_units_order_and_parent_boundaries():
    source = rows()
    source[7]['continuation_of'] = '6'
    source[8]['parent_id'] = source[9]['parent_id'] = 'second-parent'
    result = ep.build_packets(reversed(source))
    assert [p.leaf_ids for p in result.packets] == [tuple(str(i) for i in range(8)), ('8', '9')]
    assert result == ep.build_packets(source)
    assert not result.unavailable


def test_budget_flushes_whole_continuations_and_excludes_oversized_units():
    source = [leaf(str(i), 'x' * (1601 if i == 3 else 600), ordinal=i * 2000,
                   token_count=1601 if i == 3 else 600) for i in range(5)]
    source[2]['continuation_of'] = '1'
    source[3]['quality']['token_count'] = 1601
    result = ep.build_packets(source)
    assert [p.leaf_ids for p in result.packets] == [('0',), ('1', '2'), ('4',)]
    assert result.unavailable[0]['reason'] == 'oversized'
    assert result.unavailable[0]['leaf_ids'] == ('3',)


def test_packet_metadata_is_immutable_and_source_reconstructible():
    source = rows(2)
    before = deepcopy(source)
    packet = ep.build_packets(source).packets[0]
    source[0]['source_spans'][0]['start'] = 99
    assert packet.source_spans[0]['start'] == before[0]['source_spans'][0]['start']
    with pytest.raises(TypeError):
        packet.source_spans[0]['start'] = 4
    assert ep.reconstruct_packet(packet, before) == packet
    before[0]['source_text'] = 'tampered'
    with pytest.raises(ValueError, match='PACKET_SOURCE_MISMATCH'):
        ep.reconstruct_packet(packet, before)


def test_unknown_parent_and_duplicate_leaf_never_form_packets():
    source = rows(1)
    source[0]['parent_id'] = None
    assert ep.build_packets(source).unavailable[0]['reason'] == 'structure_unavailable'
    with pytest.raises(ValueError):
        ep.build_packets(source + source)


def test_two_800_byte_units_flush_before_the_separator_exceeds_1600():
    source = [leaf('a', 'a' * 800, ordinal=0, token_count=800),
              leaf('b', 'b' * 800, ordinal=1000, token_count=800)]
    result = ep.build_packets(source)
    assert [packet.leaf_ids for packet in result.packets] == [('a',), ('b',)]
    assert [packet.token_count for packet in result.packets] == [800, 800]


def test_continuation_internal_separator_counts_toward_oversize():
    source = [leaf('a', 'a' * 800, ordinal=0, token_count=800),
              leaf('b', 'b' * 800, unit='a', ordinal=1000, token_count=800)]
    result = ep.build_packets(source)
    assert not result.packets
    assert result.unavailable[0]['leaf_ids'] == ('a', 'b')
    assert result.unavailable[0]['reason'] == 'oversized'


def test_exact_1600_byte_continuation_fits_and_reports_joined_count():
    source = [leaf('a', 'a' * 800, ordinal=0, token_count=800),
              leaf('b', 'b' * 799, unit='a', ordinal=1000, token_count=799)]
    packet = ep.build_packets(source).packets[0]
    assert packet.leaf_ids == ('a', 'b')
    assert packet.token_count == len(packet.source_text.encode('utf-8')) == 1600


@pytest.mark.parametrize('quality_count', [0, 1, 5000])
def test_actual_utf8_source_counts_replace_stale_quality_counts(quality_count):
    source = [leaf('a', '原' * 400, ordinal=0, token_count=quality_count),
              leaf('b', '文' * 400, ordinal=2000, token_count=quality_count)]
    result = ep.build_packets(source)
    assert [packet.leaf_ids for packet in result.packets] == [('a',), ('b',)]
    assert all(packet.token_count == 1200 for packet in result.packets)
