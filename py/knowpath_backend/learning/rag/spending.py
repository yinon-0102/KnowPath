"""Conservative reservations at explicitly configured prices, before provider I/O."""
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock

from .verification import VerificationError


def _amount(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not money')
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError('invalid amount')
    return number


def spending_configuration():
    ceiling, filename = os.getenv('RAG_MAX_REQUEST_COST'), os.getenv('RAG_PRICING_FILE')
    if not ceiling and not filename:
        return {'enabled': False}
    try:
        if not ceiling or not filename or _amount(ceiling) <= 0:
            raise ValueError('both ceiling and pricing are required')
        pricing = json.loads(Path(filename).read_text(encoding='utf-8'))
        if not pricing.get('currency') or not pricing.get('date') or not pricing.get('price_table'):
            raise ValueError('incomplete pricing')
        for rate in pricing['price_table'].values():
            for key in ('input_per_million', 'output_per_million'):
                _amount(rate[key])
            _amount(rate.get('per_call', 0))
        return {'enabled': True, 'ceiling': str(_amount(ceiling)), 'pricing': pricing}
    except (ValueError, TypeError, KeyError, OSError, InvalidOperation, AttributeError):
        raise VerificationError('RAG_SPEND_CONFIG_INVALID') from None


class RequestSpending:
    def __init__(self, config):
        self.config = config
        self.reserved = Decimal(0)
        self.calls = 0
        self.lock = Lock()

    def reserve(self, request):
        if not self.config['enabled']:
            return
        try:
            body = json.loads(request.content)
            model = body['model']
            rate = self.config['pricing']['price_table'][model]
            # JSON bytes bound text tokens plus a deliberately conservative
            # protocol allowance. Rerank may bill the query once per document.
            input_bound = len(request.content) + 1024
            output_bound = 0
            if request.url.path.endswith('/chat/completions'):
                output_bound = body['max_tokens']
                if type(output_bound) is not int or output_bound <= 0:
                    raise ValueError('unbounded completion')
            elif request.url.path.endswith('/text-rerank'):
                input_bound += len(body['input']['query'].encode('utf-8')) * len(body['input']['documents'])
            elif not request.url.path.endswith('/embeddings'):
                raise ValueError('unrecognized billed endpoint')
            amount = (_amount(rate['input_per_million']) * input_bound
                + _amount(rate['output_per_million']) * output_bound) / 1_000_000 + _amount(rate.get('per_call', 0))
        except (ValueError, TypeError, KeyError, InvalidOperation, AttributeError):
            raise VerificationError('RAG_SPEND_CONFIG_INVALID') from None
        with self.lock:
            if self.reserved + amount > _amount(self.config['ceiling']):
                raise VerificationError('RAG_COST_BUDGET_EXCEEDED')
            self.reserved += amount
            self.calls += 1
        # Never refund reservations after failures or uncertain remote completion.

    def trace(self):
        if not self.config['enabled']:
            return {'enabled': False, 'reason': 'pricing_and_monetary_ceiling_not_configured'}
        with self.lock:
            return {'enabled': True, 'ceiling': self.config['ceiling'],
                'reserved_upper_bound': str(self.reserved), 'reserved_calls': self.calls,
                'currency': self.config['pricing']['currency'], 'price_date': self.config['pricing']['date'],
                'billing_statement': False}
