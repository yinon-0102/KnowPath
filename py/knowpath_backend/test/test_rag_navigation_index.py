from copy import deepcopy
import json

import pytest

from knowpath_backend.learning.rag import navigation_index as nav
from knowpath_backend.test.test_rag_evidence_packets import rows


def source(count=10):
    leaves = rows(count)
    for row in leaves:
        row['section_path'] = ['Chapter']
    nodes = [{'node_id':'doc','parent_node_id':None,'kind':'document','retrieval_version_id':'r1'},
             {'node_id':'p1','parent_node_id':'doc','kind':'section','section_path':['Chapter'],'retrieval_version_id':'r1'}]
    nodes += [{'node_id':'node-'+r['chunk_id'],'parent_node_id':'p1','kind':'leaf',
               'chunk_id':r['chunk_id'],'retrieval_version_id':'r1'} for r in leaves]
    return leaves, nodes


def plan(leaves=None, nodes=None, **kwargs):
    if leaves is None:
        leaves, nodes = source()
    return nav.plan_navigation(leaves,nodes,scope_snapshot_ids=('scope',),manifest_ids=('manifest',),
                               summary_model='model',prompt_hash='prompt-v1',**kwargs)


class Summary:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return {'summary':'Chapter 原文 navigation', 'usage':{'input_tokens':10,'output_tokens':5}}


class Embedding:
    model_version = 'embedding-v1'
    dimension = 2

    def __init__(self):
        self.inputs = []

    def embed(self,texts):
        self.inputs.extend(texts)
        return [[1.,0.] for _ in texts]


def test_build_summarizes_real_chapters_and_packets_embeds_summary_and_caches():
    prepared = plan()
    assert len(prepared.cards) == 3
    summary,embedding,cache = Summary(),Embedding(),{}
    index = nav.NavigationIndex.build(prepared,summarizer=summary,embedder=embedding,cache=cache)
    assert len(summary.requests) == prepared.call_count == 3
    assert all('navigation' in text for text in embedding.inputs)
    assert len(index.cards['p1']['descendant_packet_ids']) == 2
    assert index.cards['p1']['ordered_leaf_ids'] == [str(i) for i in range(10)]
    nav.NavigationIndex.build(prepared,summarizer=summary,embedder=embedding,cache=cache)
    assert len(summary.requests) == 3
    assert len(embedding.inputs) == 3
    assert all(len(json.dumps(request,ensure_ascii=False,separators=(',',':')).encode()) <= 6000
               and request['max_output_tokens']==512 for request in summary.requests)


def test_plan_covers_full_ordered_source_in_partitions_and_preflights_all_merges():
    leaves,nodes = source(1)
    leaves[0]['source_text'] = '连续原文' * 3000
    prepared = plan(leaves,nodes)
    chapter_calls = [call for call in prepared.calls if call.node_id=='p1']
    assert len(chapter_calls)>2
    assert ''.join(call.source_text for call in chapter_calls if not call.depends_on) == leaves[0]['source_text']
    assert chapter_calls[-1].depends_on
    with pytest.raises(ValueError,match='SUMMARY_CALL_BUDGET'):
        plan(leaves,nodes,max_calls=1)


def test_index_rejects_changed_sources_versions_and_scope(tmp_path):
    leaves,nodes=source()
    prepared=plan(leaves,nodes)
    index=nav.NavigationIndex.build(prepared,summarizer=Summary(),embedder=Embedding())
    path=tmp_path/'index.json'
    index.save(path)
    loaded=nav.NavigationIndex.load(path,rows=leaves,nodes=nodes,scope_snapshot_ids=('scope',),manifest_ids=('manifest',))
    assert loaded.index_id==index.index_id
    leaves[0]['source_text']='changed'
    with pytest.raises(ValueError,match='NAVIGATION_SOURCE_MISMATCH'):
        nav.NavigationIndex.load(path,rows=leaves,nodes=nodes,scope_snapshot_ids=('scope',),manifest_ids=('manifest',))
    with pytest.raises(ValueError,match='NAVIGATION_SCOPE_MISMATCH'):
        index.search('原文',[1.,0.],scope_snapshot_id='other')


def test_search_filters_whole_cards_and_returns_detached_metadata():
    index=nav.NavigationIndex.build(plan(),summarizer=Summary(),embedder=Embedding())
    hits=index.search('原文',[1.,0.],scope_snapshot_id='scope',allowed_leaf_ids={'0'})
    assert not hits
    hits=index.search('原文',[1.,0.],scope_snapshot_id='scope')
    hits[0]['summary']='tamper'
    assert all(card['summary']!='tamper' for card in index.cards.values())
    assert index.search('unknown query',[1.,0.],scope_snapshot_id='scope')


@pytest.mark.parametrize('bad',[{}, {'summary':''}, {'summary':'x'*769}, {'summary':'valid','leaf_ids':['unknown']}])
def test_bad_summary_fails_before_embedding(bad):
    embedder=Embedding()
    with pytest.raises(ValueError,match='SUMMARY_INVALID_RESPONSE'):
        nav.NavigationIndex.build(plan(),summarizer=lambda request:bad,embedder=embedder)
    assert not embedder.inputs


def test_unknown_hierarchy_fails_before_summary_planning():
    leaves,nodes=source()
    nodes[1]['parent_node_id']='invented'
    with pytest.raises(ValueError,match='STRUCTURE_UNAVAILABLE'):
        plan(leaves,nodes)


def test_all_planned_partitions_and_merges_execute_with_source_bindings():
    leaves,nodes=source(1)
    leaves[0]['source_text']='long original text ' * 1000
    prepared=plan(leaves,nodes)
    summary=Summary()
    nav.NavigationIndex.build(prepared,summarizer=summary,embedder=Embedding())
    assert len(summary.requests)==prepared.call_count
    assert any(request['task']=='merge' for request in summary.requests)
    assert all(request['binding_hash'] for request in summary.requests)
    assert all(len(json.dumps(request,ensure_ascii=False,separators=(',',':')).encode())<=6000
               for request in summary.requests)


def test_summary_interruption_is_durably_cached_and_not_replayed(tmp_path):
    path=tmp_path/'summary-cache.json'
    cache=nav.NavigationCache(path)
    def interrupted(request):
        raise RuntimeError('provider interrupted')
    with pytest.raises(RuntimeError):
        nav.NavigationIndex.build(plan(),summarizer=interrupted,embedder=Embedding(),cache=cache)
    summary=Summary()
    with pytest.raises(ValueError,match='SUMMARY_CACHE_ATTEMPT_UNRESOLVED'):
        nav.NavigationIndex.build(plan(),summarizer=summary,embedder=Embedding(),
                                  cache=nav.NavigationCache(path))
    assert not summary.requests


def test_cached_embedding_is_bound_to_summary_model_and_source_changes():
    original,nodes=source()
    cache,summary,embedding={},Summary(),Embedding()
    nav.NavigationIndex.build(plan(original,nodes),summarizer=summary,embedder=embedding,cache=cache)
    original[0]['source_text']='new original'
    nav.NavigationIndex.build(plan(original,nodes),summarizer=summary,embedder=embedding,cache=cache)
    assert len(summary.requests)==5
    assert len(embedding.inputs)==5


def test_multi_scope_search_requires_authorized_leaf_set():
    leaves,nodes=source()
    prepared=nav.plan_navigation(leaves,nodes,scope_snapshot_ids=('one','two'),
        manifest_ids=('m',),summary_model='m',prompt_hash='p')
    index=nav.NavigationIndex.build(prepared,summarizer=Summary(),embedder=Embedding())
    with pytest.raises(ValueError,match='NAVIGATION_SCOPE_MISMATCH'):
        index.search('text',[1.,0.],scope_snapshot_id='one')


def test_query_vector_changes_dense_only_card_ranking():
    class DifferentEmbedding(Embedding):
        def embed(self,texts):
            return [[1.,0.],[-1.,0.],[0.,1.]][:len(texts)]
    index=nav.NavigationIndex.build(plan(),summarizer=Summary(),embedder=DifferentEmbedding())
    positive=index.search('unrelated',[1.,0.],scope_snapshot_id='scope')
    negative=index.search('unrelated',[-1.,0.],scope_snapshot_id='scope')
    assert positive[0]['node_id']!=negative[0]['node_id']


def test_tampered_mapping_rejected_even_with_rehashed_artifact(tmp_path):
    from knowpath_backend.learning.rag.evidence_packets import digest
    leaves,nodes=source()
    index=nav.NavigationIndex.build(plan(leaves,nodes),summarizer=Summary(),embedder=Embedding())
    path=tmp_path/'index.json'
    index.save(path)
    data=json.loads(path.read_text())
    data['cards']['p1']['ordered_leaf_ids']=['invented']
    data.pop('index_id')
    data['index_id']=digest(data)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='NAVIGATION_SOURCE_MISMATCH'):
        nav.NavigationIndex.load(path,rows=leaves,nodes=nodes,scope_snapshot_ids=('scope',),manifest_ids=('manifest',))


def test_summary_control_characters_rejected_before_merge():
    with pytest.raises(ValueError,match='SUMMARY_INVALID_RESPONSE'):
        nav.NavigationIndex.build(plan(),summarizer=lambda request:{'summary':'text\u0001'},
                                  embedder=Embedding())


def test_embedding_started_is_durable_before_send_and_unknown_never_replays(tmp_path):
    path = tmp_path / 'cache.json'
    class Interrupted(Embedding):
        def embed(self, texts):
            on_disk = json.loads(path.read_text())
            batches = [v for k, v in on_disk.items() if k.startswith('embedding_batch:')]
            assert len(batches) == 1 and batches[0]['status'] == 'started'
            raise RuntimeError('interrupted after send')
    with pytest.raises(RuntimeError, match='interrupted'):
        nav.NavigationIndex.build(plan(), summarizer=Summary(), embedder=Interrupted(),
                                  cache=nav.NavigationCache(path))
    retry = Embedding()
    with pytest.raises(ValueError, match='EMBEDDING_CACHE_ATTEMPT_UNRESOLVED'):
        nav.NavigationIndex.build(plan(), summarizer=Summary(), embedder=retry,
                                  cache=nav.NavigationCache(path))
    assert not retry.inputs


def test_rebuild_preserves_each_completed_embedding_batch_usage_once(tmp_path):
    class Metered(Embedding):
        def embed(self, texts):
            self.last_usage = {'calls': 1, 'input_tokens': 37, 'complete': True}
            return super().embed(texts)
    path, prepared = tmp_path / 'cache.json', plan()
    first = nav.NavigationIndex.build(prepared, summarizer=Summary(), embedder=Metered(),
                                      cache=nav.NavigationCache(path))
    retry = Metered()
    rebuilt = nav.NavigationIndex.build(prepared, summarizer=Summary(), embedder=retry,
                                        cache=nav.NavigationCache(path))
    original = [u for u in first.usage if u['kind'] == 'embedding']
    reused = [u for u in rebuilt.usage if u['kind'] == 'embedding']
    assert len(original) == len(reused) == 1
    assert original[0]['call_id'] == reused[0]['call_id']
    assert dict(reused[0]['usage']) == {'calls': 1, 'input_tokens': 37, 'complete': True}
    assert reused[0]['cached'] is True and not retry.inputs


def test_interrupted_completion_write_keeps_started_batch_without_partial_vectors(tmp_path):
    path = tmp_path / 'cache.json'
    class CompletionFailure(nav.NavigationCache):
        def __setitem__(self, key, value):
            if key.startswith('embedding_batch:') and value.get('status') == 'complete':
                raise RuntimeError('completion write failed')
            super().__setitem__(key, value)
    with pytest.raises(RuntimeError, match='completion write'):
        nav.NavigationIndex.build(plan(), summarizer=Summary(), embedder=Embedding(),
                                  cache=CompletionFailure(path))
    values = json.loads(path.read_text())
    assert not any(key.startswith('embedding:') for key in values)
    assert any(key.startswith('embedding_batch:') and value['status'] == 'started'
               for key, value in values.items())
    retry = Embedding()
    with pytest.raises(ValueError, match='EMBEDDING_CACHE_ATTEMPT_UNRESOLVED'):
        nav.NavigationIndex.build(plan(), summarizer=Summary(), embedder=retry,
                                  cache=nav.NavigationCache(path))
    assert not retry.inputs


def test_cache_fsyncs_before_paid_summary_or_embedding(tmp_path, monkeypatch):
    import os
    real_fsync, syncs = os.fsync, []
    def observed(fd):
        syncs.append(fd)
        real_fsync(fd)
    monkeypatch.setattr(os, 'fsync', observed)
    class SyncedSummary(Summary):
        def __call__(self, request):
            assert syncs
            return super().__call__(request)
    class SyncedEmbedding(Embedding):
        def embed(self, texts):
            assert len(syncs) >= 2 * plan().call_count + 1
            return super().embed(texts)
    nav.NavigationIndex.build(plan(), summarizer=SyncedSummary(), embedder=SyncedEmbedding(),
                              cache=nav.NavigationCache(tmp_path / 'cache.json'))


def test_multiple_embedding_batches_restore_usage_once_and_missing_usage_stays_unknown(tmp_path):
    leaves, nodes = source(90)
    prepared, path = plan(leaves, nodes), tmp_path / 'cache.json'
    first = nav.NavigationIndex.build(prepared, summarizer=Summary(), embedder=Embedding(),
                                      cache=nav.NavigationCache(path))
    retry = Embedding()
    rebuilt = nav.NavigationIndex.build(prepared, summarizer=Summary(), embedder=retry,
                                        cache=nav.NavigationCache(path))
    original = [u for u in first.usage if u['kind'] == 'embedding']
    reused = [u for u in rebuilt.usage if u['kind'] == 'embedding']
    assert len(original) == len(reused) == 2
    assert {u['call_id'] for u in original} == {u['call_id'] for u in reused}
    assert sum(u['input_count'] for u in reused) == len(prepared.cards)
    assert all(u['usage'] is None for u in reused)
    assert not retry.inputs


def test_completed_first_batch_survives_unknown_second_batch_without_replay(tmp_path):
    leaves, nodes = source(90)
    prepared, path = plan(leaves, nodes), tmp_path / 'cache.json'
    class SecondFailure(Embedding):
        calls = 0
        def embed(self, texts):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError('second batch interrupted')
            return super().embed(texts)
    with pytest.raises(RuntimeError, match='second batch'):
        nav.NavigationIndex.build(prepared, summarizer=Summary(), embedder=SecondFailure(),
                                  cache=nav.NavigationCache(path))
    entries = [v for k, v in json.loads(path.read_text()).items() if k.startswith('embedding_batch:')]
    assert sorted(v['status'] for v in entries) == ['complete', 'started']
    retry = Embedding()
    with pytest.raises(ValueError, match='EMBEDDING_CACHE_ATTEMPT_UNRESOLVED'):
        nav.NavigationIndex.build(prepared, summarizer=Summary(), embedder=retry,
                                  cache=nav.NavigationCache(path))
    assert not retry.inputs


def test_summary_provider_failure_keeps_safe_cause_and_remains_non_replayable(tmp_path):
    from knowpath_backend.learning.rag.verification import VerificationError
    path=tmp_path/'failed-cache.json'
    def failed(request):
        raise VerificationError('MODEL_UNAVAILABLE',details={'failure_kind':'rate_limited'})
    with pytest.raises(VerificationError):
        nav.NavigationIndex.build(plan(),summarizer=failed,embedder=Embedding(),cache=nav.NavigationCache(path))
    entry=next(iter(json.loads(path.read_text()).values()))
    assert entry['status']=='started'
    assert entry['error']['code']=='MODEL_UNAVAILABLE'
    assert entry['error']['details']['failure_kind']=='rate_limited'
    assert entry['elapsed_seconds']>=0
    with pytest.raises(ValueError,match='SUMMARY_CACHE_ATTEMPT_UNRESOLVED'):
        nav.NavigationIndex.build(plan(),summarizer=Summary(),embedder=Embedding(),cache=nav.NavigationCache(path))
