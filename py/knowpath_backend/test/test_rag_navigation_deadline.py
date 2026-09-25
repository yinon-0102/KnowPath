from contextlib import contextmanager
from types import SimpleNamespace
import time

import httpx
import pytest

from knowpath_backend.learning.rag import vector
from knowpath_backend.learning.rag.flat_navigation import FlatNavigationPlugin
from knowpath_backend.learning.rag.navigation import BoundedNavigator
from knowpath_backend.learning.rag.runtime import DeadlineTransport
from knowpath_backend.learning.rag.retrieval import RetrievalError
from knowpath_backend.learning.rag.verification import VerificationError
from knowpath_backend.test.test_rag_b3 import leaf, request, Embedder, Dense
from knowpath_backend.test.test_rag_b4_navigation import Model


def test_scoped_dense_deadline_reaches_physical_transport_and_resets(monkeypatch):
    now=time.monotonic()
    seen=[]
    def handle(req):
        seen.append(req.extensions['timeout']['read'])
        return httpx.Response(200,json={'ok':True})
    monkeypatch.setattr(httpx,'HTTPTransport',lambda **kwargs:httpx.MockTransport(handle))
    with httpx.Client(transport=DeadlineTransport(now+240)) as client:
        with vector.dense_search_deadline(now+3):
            client.get('http://example.test/collection')
            client.get('http://example.test/query')
        client.get('http://example.test/ordinary')
    assert all(0<seconds<=3 for seconds in seen[:2])
    assert seen[2]>3


def test_scoped_deadline_caps_exchange_wait_not_only_socket_timeout(monkeypatch):
    from knowpath_backend.learning.rag import runtime
    observed=[]
    monkeypatch.setattr(DeadlineTransport,'_start',lambda self,deadline:None)
    def receive(exchange):
        observed.append(exchange.deadline)
        raise VerificationError('RAG_DEADLINE_EXCEEDED')
    monkeypatch.setattr(runtime._Exchange,'receive',receive)
    now=time.monotonic()
    with vector.dense_search_deadline(now+2):
        with pytest.raises(VerificationError):
            DeadlineTransport(now+240).handle_request(httpx.Request('GET','http://example.test/query'))
    assert observed==[now+2]


@pytest.mark.parametrize('overall_expired',[False,True])
def test_flat_focused_deadline_recovers_only_local_timeout(monkeypatch,overall_expired):
    clock=[0.]
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    row=leaf('a','alpha')
    card={'node_id':'card','title':'source','summary':'short','ordered_leaf_ids':['a']}
    class Index:
        plan=SimpleNamespace(manifest_ids=('manifest',))
        cards={'card':card}
        def validate(self,rows):pass
        def search(self,*args,**kwargs):return [card]
    class DenseWithDeadline(Dense):
        calls=0
        def search(self,*args,**kwargs):
            self.calls+=1
            if self.calls==2:
                assert vector.current_dense_search_deadline()==min(20.,15. if overall_expired else 240.)
                clock[0]=vector.current_dense_search_deadline()
                raise RetrievalError('VECTOR_UNAVAILABLE')
            assert vector.current_dense_search_deadline() is None
            return [('a',1.)]
    nav=BoundedNavigator(Model({'selections':[{'id':'card','question_part':'q'}]}))
    plugin=FlatNavigationPlugin(Embedder(),DenseWithDeadline(),navigation_index=Index(),navigator=nav)
    req=request().model_copy(update={'deadline':15. if overall_expired else 240.})
    if overall_expired:
        with pytest.raises(RetrievalError,match='RETRIEVAL_DEADLINE_EXCEEDED'):
            plugin.retrieve(req,[row])
    else:
        result=plugin.retrieve(req,[row])
        assert result['trace']['fallback_reason']=='navigation_timeout'
        assert [c['chunk_id'] for c in result['candidates']]==['a']
        assert len(result['trace']['navigation']['calls'])==1
        assert result['trace']['navigation']['wall_seconds']==20.
    assert vector.current_dense_search_deadline() is None
