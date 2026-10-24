"""The physical chat boundary used by the function-calling execution loop.

Ports return SDK-shaped choices/message/tool_calls, finish_reason and usage.
Budget-aware applications wrap this boundary, including each physical retry.
"""
from typing import Protocol, Any
from types import SimpleNamespace


class ModelPort(Protocol):
    def complete(self, request: dict, *, stream: bool, on_text_chunk=None,
                 on_reasoning_chunk=None, should_cancel=None,
                 strict_callbacks: bool = False) -> Any: ...


class SDKModelPort:
    """Borrow an SDK client; its lifetime remains owned by the caller."""
    def __init__(self, client):
        self.client = client

    def complete(self, request, *, stream=True, on_text_chunk=None,
                 on_reasoning_chunk=None, should_cancel=None,
                 strict_callbacks=False):
        from knowpath_backend.core.turn import TurnCancelled
        if should_cancel is not None and should_cancel():
            if strict_callbacks:
                raise TurnCancelled('Turn cancelled before model call')
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=None), finish_reason='stop')],
                usage=None, _cancelled=True)
        if stream:
            from knowpath_backend.agents.function_call_agent import MyFunctionCallAgent
            return MyFunctionCallAgent._stream_chat_completion(
                self.client, request, on_text_chunk, on_reasoning_chunk,
                should_cancel=should_cancel, strict_callbacks=strict_callbacks)
        response = self.client.chat.completions.create(**dict(request, stream=False))
        cancelled = should_cancel is not None and should_cancel()
        if cancelled and strict_callbacks:
            raise TurnCancelled('Turn cancelled during model call')
        # Do not mutate an SDK/Pydantic response to attach runtime-only fields.
        result = SimpleNamespace(choices=response.choices, usage=getattr(response, 'usage', None),
                                 _cancelled=bool(cancelled))
        content = response.choices[0].message.content
        if content and on_text_chunk is not None and not cancelled:
            try:
                on_text_chunk(content)
            except Exception:
                if strict_callbacks:
                    raise
        return result
