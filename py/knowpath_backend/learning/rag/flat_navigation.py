"""Same-card/two-call control without online ancestry or packet reading."""
from .tree_navigation import TreeNavigationPlugin
from .navigation import NavigationFailure
from .units import build_units
from .vector import dense_search_deadline
from .retrieval import RetrievalError
from .verification import VerificationError
from .plugins import check_deadline
import time


class FlatNavigationPlugin(TreeNavigationPlugin):
    name = 'f'
    tree_navigation = False

    def _groups(self, request, rows, selected_cards, query_vector, trace):
        allowed = {i for card in selected_cards for i in card['ordered_leaf_ids']}
        scoped = [r for r in rows if r['chunk_id'] in allowed]
        navigation_deadline = getattr(self.navigator, 'deadline', None)
        if navigation_deadline is None:
            # Production uses BoundedNavigator; deterministic injected selectors
            # still receive a bounded local retrieval phase.
            navigation_deadline = min(request.deadline, time.monotonic()+20)
        focused_request = request.model_copy(update={'deadline': min(request.deadline, navigation_deadline)})
        try:
            with dense_search_deadline(focused_request.deadline):
                ranked = self._hybrid_rankings(focused_request, scoped, query_vector=query_vector, total_limit=40)
        except (RetrievalError, VerificationError) as error:
            check_deadline(request)
            if time.monotonic() >= navigation_deadline:
                if isinstance(error, VerificationError) and (
                        error.code in {'RAG_COST_BUDGET_EXCEEDED', 'RAG_SPEND_CONFIG_INVALID'}
                        or error.details.get('failure_kind') == 'authentication'):
                    raise
                raise NavigationFailure('navigation_timeout') from None
            raise
        units = build_units(scoped)
        by_leaf = {i: u for u in units for i in u.leaf_ids}
        ordered, seen = [], set()
        for identifier, _ in ranked['fused']:
            unit = by_leaf[identifier]
            if unit.anchor_id not in seen:
                seen.add(unit.anchor_id)
                ordered.append(unit)
        menus = []
        for unit in ordered[:24]:
            locators = [c for c in self.navigation_index.cards.values() if set(unit.leaf_ids)<=set(c['ordered_leaf_ids'])]
            locator = min(locators,key=lambda c:(len(c['ordered_leaf_ids']),c['node_id']))
            # Shared cached card describes a broader source range; no new
            # relation or per-question summary is created for the flat unit.
            menus.append({'id':unit.anchor_id,'title':locator['title'], 'summary':locator['summary'],
                'locator_scope':'broader_card','ordinal':unit.ordinal,
                'source_range':[{'block':s.get('block'),'start':s['start'],'end':s['end']} for s in unit.source_spans]})
        selected = self.navigator.select(request.query, menus, deadline=request.deadline)
        by_id = {u.anchor_id: u for u in ordered}
        if any(i not in {m['id'] for m in menus} for i in selected):
            raise NavigationFailure('invalid_selection')
        trace['frontier_omitted_ids'] = [u.anchor_id for u in ordered[24:]]
        trace['selected_flat_unit_ids'] = selected
        return [list(by_id[i].leaf_ids) for i in selected] + [list(u.leaf_ids) for u in ordered if u.anchor_id not in selected]
