"""Tokens a piece of AI work used, as the providers report them.

``with metered() as used:`` counts every model call made inside it, including calls from tasks it
starts, into ``used``. Calls outside any meter are not counted.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

_meter: ContextVar[Optional[dict]] = ContextVar('ai_usage', default=None)


def empty() -> dict:
    return {'calls': 0, 'input_tokens': 0, 'output_tokens': 0}


@contextmanager
def metered():
    used = empty()
    token = _meter.set(used)
    try:
        yield used
    finally:
        try:
            _meter.reset(token)
        except ValueError:
            pass  # A streaming response closed from another context; that context never saw the meter.


def record(input_tokens, output_tokens) -> None:
    used = _meter.get()
    if used is None:
        return
    used['calls'] += 1
    used['input_tokens'] += int(input_tokens or 0)
    used['output_tokens'] += int(output_tokens or 0)


def record_openai(response) -> None:
    usage = getattr(response, 'usage', None)
    record(getattr(usage, 'prompt_tokens', 0), getattr(usage, 'completion_tokens', 0))


def record_anthropic(response) -> None:
    usage = getattr(response, 'usage', None)
    record(getattr(usage, 'input_tokens', 0), getattr(usage, 'output_tokens', 0))
