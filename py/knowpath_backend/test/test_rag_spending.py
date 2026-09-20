import json
import httpx
import pytest
from knowpath_backend.learning.rag.spending import RequestSpending, spending_configuration
from knowpath_backend.learning.rag.verification import VerificationError


def request():
    return httpx.Request('POST', 'https://provider.example/chat/completions',
        json={'model': 'test', 'messages': [], 'max_tokens': 20})


def test_shared_reservations_stop_later_calls_and_never_refund_failures():
    ledger = RequestSpending({'enabled': True, 'ceiling': '.0015', 'pricing': {'currency': 'CNY',
        'date': '2026-09-20', 'price_table': {'test': {'input_per_million': 1, 'output_per_million': 2}}}})
    ledger.reserve(request())
    with pytest.raises(VerificationError, match='RAG_COST_BUDGET_EXCEEDED'):
        ledger.reserve(request())
    assert ledger.trace()['reserved_calls'] == 1


def test_unknown_provider_price_blocks_call():
    ledger = RequestSpending({'enabled': True, 'ceiling': '1', 'pricing': {'price_table': {}}})
    with pytest.raises(VerificationError, match='RAG_SPEND_CONFIG_INVALID'):
        ledger.reserve(request())


def test_transport_rejects_over_budget_before_provider_io(monkeypatch):
    import time
    from knowpath_backend.learning.rag.runtime import DeadlineTransport
    ledger = RequestSpending({'enabled': True, 'ceiling': '.00001', 'pricing': {'currency':'CNY',
        'date':'2026-09-20','price_table': {'test': {'input_per_million': 1, 'output_per_million': 2}}}})
    calls = []
    transport = DeadlineTransport(time.monotonic()+10, ledger)
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs:
        httpx.MockTransport(lambda req: (calls.append(req),httpx.Response(200))[1]))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(VerificationError, match='RAG_COST_BUDGET_EXCEEDED'):
            client.send(request())
    assert calls == []


def test_ceiling_without_prices_fails_closed(monkeypatch):
    monkeypatch.setenv('RAG_MAX_REQUEST_COST', '1')
    monkeypatch.delenv('RAG_PRICING_FILE', raising=False)
    with pytest.raises(VerificationError, match='RAG_SPEND_CONFIG_INVALID'):
        spending_configuration()


def test_price_table_is_loaded_into_freezable_configuration(monkeypatch, tmp_path):
    path = tmp_path/'prices.json'
    path.write_text(json.dumps({'currency':'CNY','date':'2026-09-20',
        'price_table':{'test':{'input_per_million':1,'output_per_million':2}}}),encoding='utf-8')
    monkeypatch.setenv('RAG_MAX_REQUEST_COST', '0.1')
    monkeypatch.setenv('RAG_PRICING_FILE', str(path))
    assert spending_configuration()['pricing']['price_table']['test']['input_per_million'] == 1
