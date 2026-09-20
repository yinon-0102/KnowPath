"""Usage-based estimates at frozen unit prices; missing usage is never free."""
from .dataset import numeric


def estimate(trace, pricing):
    table = pricing.get('price_table') or {}
    if not table or not pricing.get('currency') or not pricing.get('date'):
        return None
    usage = trace.get('usage', [])
    if len(usage) != trace.get('generation_calls',0)+trace.get('verification_calls',0):
        return None
    calls = [(row, True, 1) for row in usage]
    if trace.get('retrieval',{}).get('embedding_calls',0):
        embedding=trace.get('embedding_usage')
        if not embedding or not embedding.get('complete'): return None
        calls.append((embedding, False, embedding.get('calls')))
    if trace.get('rerank_calls',0):
        # The runtime records only the single rerank response. A later extension
        # with multiple calls needs aggregate usage before its cost is complete.
        if trace['rerank_calls'] != 1:
            return None
        calls.append((trace.get('rerank_usage'), False, 1))
    amount=0.0
    for row, chat, physical_calls in calls:
        if not isinstance(row,dict): return None
        rate=table.get(row.get('model'))
        if not isinstance(rate,dict): return None
        input_tokens=row.get('input_tokens',row.get('prompt_tokens',row.get('total_tokens') if not chat else None))
        output_tokens=row.get('output_tokens',row.get('completion_tokens')) if chat else 0
        output_rate = rate.get('output_per_million') if chat else rate.get('output_per_million', 0)
        fixed_fee = rate.get('per_call', 0)
        if not all(numeric(value) for value in (input_tokens, output_tokens,
                rate.get('input_per_million'), output_rate, fixed_fee)):
            return None
        if fixed_fee and (type(physical_calls) is not int or physical_calls <= 0):
            return None
        amount += (input_tokens * rate['input_per_million'] + output_tokens * output_rate) / 1_000_000
        if fixed_fee:
            amount += fixed_fee * physical_calls
    if not numeric(amount):
        return None
    return dict(amount=amount,currency=pricing['currency'],complete=True,
                method='actual_usage_at_frozen_unit_prices',billing_statement=False)
