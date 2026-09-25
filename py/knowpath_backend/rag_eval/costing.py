"""Usage-based estimates at frozen unit prices; missing usage is never free."""
from .dataset import numeric


def _chat_tokens(usage):
    if not isinstance(usage, dict):
        return None
    values = (usage.get('input_tokens', usage.get('prompt_tokens')),
              usage.get('output_tokens', usage.get('completion_tokens')))
    return values if all(type(v) is int and v >= 0 for v in values) else None


def navigation_accounting(trace):
    """Reconcile the native navigator ledger with optional physical journal rows.

    The journal strips model names and may normalize token-key aliases. Compare
    actual token counters, not provider-only metadata; never count both ledgers.
    Empty journal navigation rows are allowed because older journals did not
    expose this stage. Native per-call usage remains authoritative in that case.
    """
    retrieval = trace.get('retrieval') or {}
    navigation = trace.get('navigation', retrieval.get('navigation'))
    native = navigation.get('calls') if isinstance(navigation, dict) else None
    journal = [row for row in (trace.get('call_journal') or {}).get('calls', [])
               if isinstance(row, dict) and str(row.get('stage', '')).startswith('navigation')]
    declared = retrieval.get('navigation_calls', trace.get('navigation_calls'))
    if isinstance(navigation, dict):
        declared = navigation.get('navigation_calls', navigation.get('call_count', declared))
    mismatch = False
    if native is not None and (not isinstance(native, list) or any(not isinstance(c, dict) for c in native)):
        return dict(calls=[], journal_calls=journal, complete=False, disagreement=True, declared=declared)
    if native is not None and journal:
        mismatch = len(native) != len(journal)
        if not mismatch:
            mismatch = any(_chat_tokens(n.get('usage')) != _chat_tokens(j.get('usage'))
                           for n, j in zip(native, journal))
    selected = native if native is not None else journal
    if declared is not None:
        mismatch |= type(declared) is not int or declared < 0 or declared != len(selected)
    return dict(calls=selected, journal_calls=journal, declared=declared,
                complete=not mismatch and all(_chat_tokens(row.get('usage')) is not None for row in selected),
                disagreement=mismatch)


def estimate(trace, pricing):
    table = pricing.get('price_table') or {}
    if not table or not pricing.get('currency') or not pricing.get('date'):
        return None
    usage = trace.get('usage', [])
    if len(usage) != trace.get('generation_calls',0)+trace.get('verification_calls',0):
        return None
    calls = [(row, True, 1) for row in usage]
    navigation = navigation_accounting(trace)
    if not navigation['complete']:
        return None
    calls.extend((row.get('usage'), True, 1) for row in navigation['calls'])
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
