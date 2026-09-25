import json
import time
from types import SimpleNamespace

import pytest

from knowpath_backend.learning.rag.navigation import BoundedNavigator, NavigationFailure, merge_navigation_candidates
from knowpath_backend.learning.rag.context import assemble_context


class Model:
    max_input_tokens = 4000
    max_output_tokens = 512
    def __init__(self, result):
        self.result, self.calls = result, []
        self.last_usage = {'prompt_tokens': 100, 'completion_tokens': 10}
        self.counting_profile = SimpleNamespace(count_request=lambda body: SimpleNamespace(value=len(json.dumps(body))))
    def request_body(self, messages, schema=None):
        return {'messages': messages}
    def generate_json(self, messages, *, deadline, response_schema=None):
        self.calls.append((messages, deadline))
        return self.result


def test_navigation_unknown_id_rejected_without_retry():
    model = Model({'selections': [{'id': 'unknown', 'question_part': 'why'}]})
    navigator = BoundedNavigator(model)
    with pytest.raises(NavigationFailure, match='invalid_selection'):
        navigator.select('why', [{'id': 'n1', 'title': 'safe', 'summary': 'text'}], deadline=time.monotonic()+30)
    assert len(model.calls) == 1
    assert navigator.trace['calls'][0]['usage']['prompt_tokens'] == 100


def test_navigation_two_calls_share_one_deadline_and_no_third_call():
    model = Model({'selections': [{'id': 'n1', 'question_part': 'why'}]})
    nav = BoundedNavigator(model)
    cards = [{'id': 'n1', 'title': 'safe', 'summary': 'text'}]
    deadline = time.monotonic()+240
    assert nav.select('why', cards, deadline=deadline) == ['n1']
    nav.select('why', cards, deadline=deadline)
    with pytest.raises(NavigationFailure, match='call_limit'):
        nav.select('why', cards, deadline=deadline)
    assert model.calls[0][1] == model.calls[1][1]
    assert model.calls[0][1] < deadline-210


def test_navigation_menu_never_slices_card_and_records_omissions():
    model = Model({'selections': [{'id': 'small', 'question_part': 'why'}]})
    nav = BoundedNavigator(model)
    assert nav.select('why', [{'id': 'large', 'summary': 'x'*5000}, {'id': 'small', 'summary': 'ok'}], deadline=time.monotonic()+30) == ['small']
    assert nav.trace['calls'][0]['omitted_ids'] == ['large']


def test_merge_keeps_a_top20_and_complete_navigation_groups():
    rows = [{'chunk_id': str(i), 'requires': []} for i in range(60)]
    ids, accepted, skipped = merge_navigation_candidates([str(i) for i in range(40)],
        [[str(i) for i in range(40, 48)], [str(i) for i in range(48, 56)], [str(i) for i in range(56, 60)]], rows)
    assert ids[:20] == [str(i) for i in range(20)]
    assert set(ids[20:]) == {str(i) for i in range(40, 60)}
    assert len(accepted) == 3 and not skipped


def test_merge_rejects_missing_dependency_without_damaging_baseline():
    rows = [{'chunk_id': str(i), 'requires': []} for i in range(42)]
    rows[40]['requires'] = ['absent']
    base = [str(i) for i in range(40)]
    ids, accepted, skipped = merge_navigation_candidates(base, [['40', '41']], rows)
    assert ids == base and not accepted and skipped


def test_packet_packing_preserves_original_metadata_and_whole_packet():
    rows = [dict(chunk_id='a', source_text='abc', requires=[], evidence_group='original-a', atomic_group='packet'),
            dict(chunk_id='b', source_text='def', requires=[], evidence_group='original-b', atomic_group='packet'),
            dict(chunk_id='c', source_text='ok', requires=[])]
    packed = assemble_context(rows, max_tokens=4, count_tokens=len)
    assert [r['chunk_id'] for r in packed] == ['c']
    assert rows[0]['evidence_group'] == 'original-a'


def test_tree_can_select_packet_with_no_a_seed_and_fallback_is_exact():
    from knowpath_backend.learning.rag.tree_navigation import TreeNavigationPlugin
    from knowpath_backend.learning.rag.evidence_packets import build_packets
    from knowpath_backend.test.test_rag_b3 import leaf, request, Embedder, Dense
    rows = [leaf(str(i), 'alpha', ordinal=i, parent='p' if i<40 else 'other') for i in range(48)]
    packet = next(p for p in build_packets(rows).packets if set(p.leaf_ids) == {str(i) for i in range(40,48)})
    card = {'node_id': packet.packet_id, 'kind': 'packet', 'parent_id': 'other',
            'title': 'packet', 'summary': 'alpha', 'ordered_leaf_ids': list(packet.leaf_ids),
            'descendant_packet_ids': [packet.packet_id]}
    class Index:
        plan = SimpleNamespace(manifest_ids=('manifest',))
        cards = {packet.packet_id: card}
        packets = {packet.packet_id: packet}
        def validate(self, rows): pass
        def search(self, *args, **kwargs): return [card]
    class Navigator:
        trace = {'calls': [], 'elapsed_seconds': 0}
        def select(self, query, cards, *, deadline): return [cards[0]['id']]
    embedder = Embedder()
    plugin = TreeNavigationPlugin(embedder, Dense(), navigation_index=Index(), navigator=Navigator())
    result = plugin.retrieve(request(), rows)
    ids = [c['chunk_id'] for c in result['candidates']]
    assert set(packet.leaf_ids) <= set(ids)
    assert ids[:20] == result['trace']['baseline_candidate_ids'][:20]
    assert len(ids) <=40 and len(embedder.calls)==1
    assert result['trace']['rerank_mode'] == 'packet_atomic'
    assert any(g['group_id']==packet.packet_id for g in result['trace']['rerank_groups'])
    class Bad:
        trace = {'calls': [{'failure':'invalid_selection'}]}
        def select(self, *args, **kwargs): raise NavigationFailure('invalid_selection')
    plugin.navigator = Bad()
    result = plugin.retrieve(request(), rows)
    assert [c['chunk_id'] for c in result['candidates']] == result['trace']['baseline_candidate_ids']
    assert result['trace']['fallback_reason'] == 'invalid_selection'
    assert not result['trace'].get('rerank_groups')


def test_authoritative_packet_rerank_rebuilds_from_full_source_partition():
    from knowpath_backend.learning.rag.evidence_packets import build_packets
    from knowpath_backend.learning.rag.pipeline import authoritative_rerank_groups
    from knowpath_backend.learning.rag.verification import VerificationError
    from knowpath_backend.test.test_rag_b3 import leaf
    rows = [leaf(str(i), 'source', ordinal=i) for i in range(3)]
    packet = build_packets(rows).packets[0]
    groups = [{'group_id': packet.packet_id, 'leaf_ids': list(packet.leaf_ids), 'kind': 'packet'}]
    rebuilt = authoritative_rerank_groups(groups, rows, {r['chunk_id']:r for r in rows})
    assert rebuilt[0]['retrieval_text'] == packet.source_text
    with pytest.raises(VerificationError):
        authoritative_rerank_groups([{**groups[0], 'leaf_ids':['0','1']}], rows[:2], {r['chunk_id']:r for r in rows})


def test_pipeline_snapshot_survives_verifier_failure(build_workspace):
    from knowpath_backend.test.test_rag_pipeline import pipeline
    from knowpath_backend.learning.rag.verification import VerificationError
    instance, _ = pipeline(build_workspace)
    class Failed:
        def answer(self, *args, **kwargs): raise VerificationError('MODEL_UNAVAILABLE')
    instance.verifier = Failed()
    observed=[]
    instance.retrieval_snapshot_callback = observed.append
    with pytest.raises(VerificationError):
        instance.answer('学校应该告知谁？', space_id=build_workspace[2]['id'])
    assert instance.last_retrieval_snapshot['context_sources']
    assert instance.last_retrieval_snapshot['context_hash']
    assert observed[-1]['stage'] == 'context'


from knowpath_backend.test.test_rag_building import build_workspace


def test_b4_modes_registered_with_original_budget():
    from knowpath_backend.learning.rag.registry import REGISTRY
    from knowpath_backend.learning.rag.runtime import retrieval_budget_for_mode
    for mode in ('a0','f','b4'):
        assert mode in REGISTRY
        assert retrieval_budget_for_mode(mode).rerank_candidates == 40


def test_navigation_stage_journal_is_retained():
    from knowpath_backend.learning.rag.diagnostics import RequestJournal
    journal = RequestJournal(time.monotonic()+30)
    call = journal.begin_call('navigation')
    journal.finish_call(call, status='succeeded')
    assert journal.snapshot()['calls'][0]['stage'] == 'navigation'


def test_empty_menu_records_unsent_attempt_omissions():
    nav=BoundedNavigator(Model({}))
    with pytest.raises(NavigationFailure,match='empty_menu'):
        nav.select('why',[{'id':'huge','summary':'x'*5000}],deadline=time.monotonic()+30)
    assert not nav.trace['calls']
    assert nav.trace['menus'][0]['omitted_ids']==['huge']


def test_flat_menu_uses_short_source_locator_and_recovers_second_search_failure():
    from knowpath_backend.learning.rag.flat_navigation import FlatNavigationPlugin
    from knowpath_backend.learning.rag.retrieval import RetrievalError
    from knowpath_backend.test.test_rag_b3 import leaf,request,Embedder,Dense
    row=leaf('x','alpha'*1000)
    card={'node_id':'card','title':'actual section','summary':'topic','ordered_leaf_ids':['x']}
    class Index:
        plan=SimpleNamespace(manifest_ids=('manifest',))
        cards={'card':card}
        def validate(self,rows):pass
        def search(self,*args,**kwargs):return [card]
    class Nav:
        trace={}
        def select(self,query,cards,*,deadline):
            if cards[0]['id']=='x':
                assert len(json.dumps(cards))<1500
                assert cards[0]['title']=='actual section'
                assert cards[0]['locator_scope']=='broader_card'
            return [cards[0]['id']]
    plugin=FlatNavigationPlugin(Embedder(),Dense(),navigation_index=Index(),navigator=Nav())
    assert not plugin.retrieve(request(),[row])['trace']['fallback']
    class Broken(Dense):
        calls=0
        def search(self,*args,**kwargs):
            self.calls+=1
            if self.calls==2:raise RetrievalError('VECTOR_UNAVAILABLE')
            return super().search(*args,**kwargs)
    plugin.dense=Broken()
    result=plugin.retrieve(request(),[row])
    assert result['trace']['fallback'] and result['candidates'][0]['chunk_id']=='x'
    Index.plan=SimpleNamespace(manifest_ids=('other',))
    result=plugin.retrieve(request(),[row])
    assert result['trace']['fallback_reason']=='manifest_mismatch'
