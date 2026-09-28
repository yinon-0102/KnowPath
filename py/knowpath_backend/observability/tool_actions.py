"""Tool observations never serialize arguments, returned text or approval previews."""
import logging
from contextvars import ContextVar
from contextlib import contextmanager

from .actions import Action
from .events import bind_context, context_fields

logger = logging.getLogger(__name__)
_dispatch = ContextVar('observed_tool_dispatch', default=None)


@contextmanager
def tool_dispatch(registry, name):
    """Consume one observed registry entry, allowing nested tools their own action."""
    observed = _dispatch.get() == (registry, name)
    token = _dispatch.set(None)
    try:
        yield observed
    finally:
        _dispatch.reset(token)


class ToolAction(Action):
    def __init__(self, registry, name, *, supervised=False):
        self.supervised = supervised
        # Model-proposed unregistered names may themselves contain private text.
        known = name in registry.list_tools()
        origin = context_fields().get('origin_model_call_id') or context_fields().get('model_call_id')
        super().__init__(logger, 'tool.call', tool_name=name if known else '<unregistered>',
                         origin_model_call_id=origin, execution_started=False)

    def log_context(self):
        return {**self.context, 'tool_call_id':self.fields['tool_call_id'],
                'origin_model_call_id':self.fields['origin_model_call_id']}

    def invoke(self, registry, name, args):
        with bind_context(**self.log_context()):
            self.fields['execution_started'] = True
            self.record('executing')
            token = _dispatch.set((registry, name))
            try:
                return registry.execute_tool(name, args)
            except BaseException as exc:
                if not self.supervised:
                    self.finish('failed', exc=exc)
                raise
            finally:
                _dispatch.reset(token)

    def returned(self, result):
        status = 'error_reported' if isinstance(result, str) and result.startswith('❌') else 'returned'
        self.finish(result_status=status)
