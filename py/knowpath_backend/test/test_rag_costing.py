import pytest

from knowpath_backend.rag_eval.costing import estimate


def test_cost_requires_complete_usage_and_frozen_prices():
    pricing={'currency':'CNY','date':'2026-09-20','price_table':{'model':{'input_per_million':2,'output_per_million':4}}}
    trace={'generation_calls':1,'verification_calls':0,'usage':[{'model':'model','prompt_tokens':100,'completion_tokens':50}]}
    assert estimate(trace,pricing)['amount']==.0004
    assert estimate({**trace,'generation_calls':2},pricing) is None
    assert estimate({**trace,'rerank_calls':1},pricing) is None
    assert estimate(trace,{**pricing,'price_table':{}}) is None


def test_cost_includes_fixed_fees_for_each_physical_model_request():
    pricing = {'currency': 'CNY', 'date': '2026-09-20', 'price_table': {
        'chat': {'input_per_million': 2, 'output_per_million': 4, 'per_call': .01},
        'embed': {'input_per_million': 1, 'per_call': .02},
        'rank': {'input_per_million': 3, 'per_call': .03},
    }}
    trace = {'generation_calls': 1, 'verification_calls': 1,
        'usage': [{'model': 'chat', 'input_tokens': 100, 'output_tokens': 50},
                  {'model': 'chat', 'prompt_tokens': 200, 'completion_tokens': 100}],
        'retrieval': {'embedding_calls': 1},
        'embedding_usage': {'model': 'embed', 'input_tokens': 300, 'complete': True, 'calls': 3},
        'rerank_calls': 1, 'rerank_usage': {'model': 'rank', 'total_tokens': 400}}
    result = estimate(trace, pricing)
    assert result['amount'] == pytest.approx(.1127)
    assert result['complete'] is True
    assert result['billing_statement'] is False


@pytest.mark.parametrize('fee', [-1, True, None, float('nan'), float('inf')])
def test_invalid_fixed_fee_keeps_cost_unknown(fee):
    pricing = {'currency': 'CNY', 'date': '2026-09-20', 'price_table': {
        'model': {'input_per_million': 2, 'output_per_million': 4, 'per_call': fee}}}
    trace = {'generation_calls': 1, 'usage': [
        {'model': 'model', 'prompt_tokens': 100, 'completion_tokens': 50}]}
    assert estimate(trace, pricing) is None


@pytest.mark.parametrize('count', [None, 0, -1, True, 1.5, '3'])
def test_embedding_fixed_fee_requires_physical_batch_count(count):
    pricing = {'currency': 'CNY', 'date': '2026-09-20', 'price_table': {
        'model': {'input_per_million': 2, 'per_call': .01}}}
    trace = {'retrieval': {'embedding_calls': 1}, 'embedding_usage': {
        'model': 'model', 'input_tokens': 100, 'complete': True, 'calls': count}}
    assert estimate(trace, pricing) is None


def test_chat_missing_output_price_keeps_cost_unknown():
    pricing = {'currency': 'CNY', 'date': '2026-09-20', 'price_table': {
        'model': {'input_per_million': 2}}}
    trace = {'generation_calls': 1, 'usage': [
        {'model': 'model', 'prompt_tokens': 100, 'completion_tokens': 50}]}
    assert estimate(trace, pricing) is None


def test_multiple_reranks_with_only_last_usage_keep_cost_unknown():
    pricing = {'currency': 'CNY', 'date': '2026-09-20', 'price_table': {
        'model': {'input_per_million': 2, 'per_call': .01}}}
    trace = {'rerank_calls': 2, 'rerank_usage': {'model': 'model', 'total_tokens': 100}}
    assert estimate(trace, pricing) is None


def test_known_fixed_fee_does_not_replace_missing_token_usage():
    pricing = {'currency': 'CNY', 'date': '2026-09-20', 'price_table': {
        'model': {'input_per_million': 2, 'output_per_million': 4, 'per_call': .01}}}
    trace = {'generation_calls': 1, 'usage': [{'model': 'model'}]}
    assert estimate(trace, pricing) is None


def test_token_only_embedding_price_does_not_require_batch_count():
    pricing = {'currency': 'CNY', 'date': '2026-09-20', 'price_table': {
        'model': {'input_per_million': 2}}}
    trace = {'retrieval': {'embedding_calls': 1}, 'embedding_usage': {
        'model': 'model', 'input_tokens': 100, 'complete': True}}
    assert estimate(trace, pricing)['amount'] == pytest.approx(.0002)
