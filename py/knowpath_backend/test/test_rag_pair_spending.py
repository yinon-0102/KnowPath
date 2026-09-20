import httpx
import pytest
from knowpath_backend.learning.rag.spending import RequestSpending
from knowpath_backend.learning.rag.verification import VerificationError


def ledger(ceiling):
    return RequestSpending({'enabled':True,'ceiling':str(ceiling),'pricing':{
        'currency':'CNY','date':'2026-09-21','price_table':{'test':{
            'input_per_million':1,'output_per_million':2,'per_call':.001}}}})


def plan():
    return {'model':'test','input_bound':2000,'output_bound':20}


def request():
    return httpx.Request('POST','https://provider.invalid/chat/completions',
        json={'model':'test','messages':[],'max_tokens':20})


def test_pair_cost_is_admitted_atomically_before_any_call():
    book=ledger(.005)
    with pytest.raises(VerificationError,match='RAG_COST_BUDGET_EXCEEDED'):
        book.reserve_model_pair([plan(),plan()])
    assert book.reserved==0 and book.calls==0


def test_pair_credit_consumption_does_not_double_charge_or_refund():
    book=ledger(.01)
    book.reserve_model_pair([plan(),plan()])
    upper=book.reserved
    assert book.calls==0
    book.reserve(request())
    book.reserve(request())
    assert book.reserved==upper and book.calls==2
    assert book.trace()['pending_model_reservations']==0


def test_larger_than_preapproved_input_is_blocked_before_transport():
    book=ledger(1)
    book.reserve_model_pair([plan(),plan()])
    large=httpx.Request('POST','https://provider.invalid/chat/completions',
        json={'model':'test','messages':[{'role':'user','content':'x'*3000}],'max_tokens':20})
    with pytest.raises(VerificationError,match='RAG_COST_BUDGET_EXCEEDED'):
        book.reserve(large)
    assert book.calls==0 and book.trace()['pending_model_reservations']==2


def test_overlapping_pair_or_wrong_model_cannot_consume_credits():
    book=ledger(1)
    book.reserve_model_pair([plan(),plan()])
    with pytest.raises(VerificationError,match='RAG_SPEND_CONFIG_INVALID'):
        book.reserve_model_pair([plan(),plan()])
    assert book.calls==0
