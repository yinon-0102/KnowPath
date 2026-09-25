"""B4 cross-layer entry, real descendant navigation and original packet reading."""
from copy import deepcopy

from .contracts import Candidate
from .plugins import OrdinaryPlugin, validate_chunks, check_deadline
from .navigation import NavigationFailure, merge_navigation_candidates
from .bm25 import BM25Index
from .units import build_units
from .retrieval import RetrievalError
import time


def card_menu(card, *, paths):
    value = {'id': card['node_id'], 'title': card.get('title', ''), 'summary': card['summary']}
    if paths:
        value['parent_id'] = card.get('parent_id')
        value['children'] = card.get('children', [])
    return value


class TreeNavigationPlugin(OrdinaryPlugin):
    name = 'b4'
    tree_navigation = True

    def __init__(self, embedder, dense, *, navigation_index=None, navigator=None, **kwargs):
        super().__init__(embedder, dense, **kwargs)
        self.navigation_index, self.navigator = navigation_index, navigator

    def _groups(self, request, rows, selected_cards, query_vector, trace):
        index = self.navigation_index
        allowed = {r['chunk_id'] for r in rows}
        packet_ids = list(dict.fromkeys(pid for card in selected_cards
            for pid in card.get('descendant_packet_ids', [])
            if pid in index.packets and set(index.packets[pid].leaf_ids) <= allowed))
        if not packet_ids:
            raise NavigationFailure('empty_frontier')
        # Same frozen q/lexical ordering across the full authorized card scope.
        ranked_cards = index.search(request.query, query_vector, scope_snapshot_id=request.scope_snapshot_id,
                                    allowed_leaf_ids=allowed, limit=max(12, len(index.cards)))
        ranks = {card['node_id']: i for i, card in enumerate(ranked_cards)}
        packet_ids.sort(key=lambda pid: (ranks.get(pid, len(ranks)), pid))
        trace['frontier_omitted_ids'] = packet_ids[24:]
        frontier = packet_ids[:24]
        selected = self.navigator.select(request.query, [card_menu(index.cards[pid], paths=True) for pid in frontier],
                                         deadline=request.deadline)
        if any(pid not in frontier for pid in selected):
            raise NavigationFailure('invalid_selection')
        trace['selected_packet_ids'] = selected
        return [list(index.packets[pid].leaf_ids) for pid in selected]

    def retrieve(self, request, chunks):
        rows = validate_chunks(chunks)
        check_deadline(request)
        if not rows:
            return {'candidates': [], 'trace': {'plugin': self.name}}
        by_id = {r['chunk_id']: r for r in rows}
        limit = min(40, request.budget.rerank_candidates)
        ranked = self._hybrid_rankings(request, rows, total_limit=limit)
        baseline = [identifier for identifier, _ in ranked['fused']]
        trace = {'plugin': self.name, 'embedding_calls': ranked['embedding_calls'],
                 'embedding_usage': deepcopy(getattr(self.embedder, 'last_usage', None)),
                 'baseline_candidate_ids': baseline, 'fallback': False,
                 'baseline_candidate_sources': [{'chunk_id': i, 'source_spans': by_id[i]['source_spans']} for i in baseline],
                 'keyword_count': len(ranked['keyword']), 'vector_count': len(ranked['dense']),
                 'a_retention_at_40': 1.0, 'generation_source': 'leaf'}
        chosen = baseline
        try:
            if self.navigation_index is None or self.navigator is None:
                raise NavigationFailure('structure_unavailable')
            if not set(request.manifest_ids) <= set(self.navigation_index.plan.manifest_ids):
                raise NavigationFailure('manifest_mismatch')
            self.navigation_index.validate(rows)
            cards = self.navigation_index.search(request.query, ranked['query_vector'],
                scope_snapshot_id=request.scope_snapshot_id, allowed_leaf_ids=set(by_id), limit=12)
            card_by_id = {card['node_id']: card for card in cards}
            selected = self.navigator.select(request.query, [card_menu(c, paths=self.tree_navigation) for c in cards],
                                             deadline=request.deadline)
            if any(identifier not in card_by_id for identifier in selected):
                raise NavigationFailure('invalid_selection')
            trace['selected_node_ids'] = selected
            groups = self._groups(request, rows, [card_by_id[i] for i in selected], ranked['query_vector'], trace)
            chosen, accepted, skipped = merge_navigation_candidates(baseline, groups, rows, limit=limit)
            trace['skipped_packets'] = skipped
            if not accepted:
                raise NavigationFailure('no_admitted_packet')
            if self.tree_navigation:
                packet_map = {tuple(packet.leaf_ids): packet for packet in self.navigation_index.packets.values()}
                covered, rerank_groups = set(), []
                for group in accepted:
                    packet = packet_map.get(tuple(group))
                    if packet is None:
                        raise NavigationFailure('packet_identity_invalid')
                    rerank_groups.append({'group_id': packet.packet_id, 'leaf_ids': group, 'kind': 'packet'})
                    covered.update(group)
                # Complete minimal units retain existing authoritative grouping.
                for unit in build_units(rows):
                    if set(unit.leaf_ids) <= set(chosen)-covered:
                        rerank_groups.append({'group_id': unit.anchor_id, 'leaf_ids': list(unit.leaf_ids)})
                        covered.update(unit.leaf_ids)
                rerank_groups += [{'group_id': i, 'leaf_ids': [i]} for i in chosen if i not in covered]
                trace.update(rerank_mode='packet_atomic', rerank_groups=rerank_groups)
            trace.update(a_retention_at_40=len(set(chosen)&set(baseline))/len(baseline) if baseline else None,
                         structural_additions=len(set(chosen)-set(baseline)), a_top20_retained=set(baseline[:20])<=set(chosen))
        except (NavigationFailure, ValueError, RetrievalError) as error:
            if isinstance(error, RetrievalError) and error.code in {
                    'RETRIEVAL_DEADLINE_EXCEEDED','RAG_DEADLINE_EXCEEDED','RAG_COST_BUDGET_EXCEEDED','RAG_SPEND_CONFIG_INVALID'}:
                raise
            chosen = baseline
            trace.pop('rerank_groups', None)
            trace.pop('rerank_mode', None)
            trace.update(fallback=True, fallback_reason=(str(error) if isinstance(error, NavigationFailure)
                         else error.code if isinstance(error, RetrievalError) else 'navigation_index_invalid'),
                         a_top20_retained=True, structural_additions=0, a_retention_at_40=1.0)
        finally:
            trace['navigation'] = deepcopy(getattr(self.navigator, 'trace', {}))
            if getattr(self.navigator, 'started', None) is not None:
                trace['navigation']['wall_seconds'] = time.monotonic()-self.navigator.started
        check_deadline(request)
        candidates = [Candidate(chunk_id=i, retrieval_version_id=by_id[i]['retrieval_version_id'],
            material_version_id=by_id[i]['material_version_id'], parent_id=by_id[i].get('parent_id'),
            channel='hybrid' if i in baseline else 'tree', rank=n).model_dump() for n,i in enumerate(chosen,1)]
        return {'candidates': candidates, 'trace': trace}
