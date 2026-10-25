"""How much context a configured model accepts.

Prefer what the provider reports for the exact model, then the documented size of
well-known models, else None (callers fall back to the conservative global budget).
Hard-coding one number per provider went stale quickly: names and sizes change often.
"""
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Documented by each provider (checked 2026-10-25). Only a fallback for APIs that
# do not report a size; a user-entered value always wins.
KNOWN_CONTEXT_WINDOWS = {
    'deepseek-flash': 1_048_576, 'deepseek-v4-pro': 1_048_576,
    'gpt-6-astra': 1_050_000, 'gpt-6.1-sol': 1_050_000, 'gpt-6-luna': 1_050_000,
    'claude-fable-5-1': 1_000_000, 'claude-opus-5-5': 1_000_000,
    'claude-sonnet-5-5': 1_000_000, 'claude-haiku-5-5': 1_000_000,
    'qwen-plus': 1_000_000, 'qwen-flash': 1_000_000, 'qwen-max': 32_768,
}
# Field names model-listing APIs use for the input/context size.
_REPORTED_FIELDS = ('context_window', 'max_input_tokens', 'context_length', 'max_context_length', 'max_model_len')
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _reported_size(entry: dict) -> Optional[int]:
    for field in _REPORTED_FIELDS:
        value = entry.get(field)
        if isinstance(value, int) and value > 0:
            return value
    return None


async def _ask_provider(config: dict, transport=None) -> Optional[int]:
    model, key = config['model'], config['api_key']
    async with httpx.AsyncClient(timeout=_TIMEOUT, transport=transport, follow_redirects=False) as client:
        if config.get('adapter_type') == 'anthropic':
            base = (config.get('api_base') or 'https://api.anthropic.com').rstrip('/')
            response = await client.get(f'{base}/v1/models/{model}',
                                        headers={'x-api-key': key, 'anthropic-version': '2023-06-01'})
            response.raise_for_status()
            return _reported_size(response.json())
        base = (config.get('api_base') or 'https://api.openai.com/v1').rstrip('/')
        response = await client.get(f'{base}/models', headers={'Authorization': f'Bearer {key}'})
        response.raise_for_status()
        entry = next((m for m in response.json().get('data', []) if m.get('id') == model), None)
        return _reported_size(entry) if entry else None


async def detect_context_window(config: dict, *, transport=None) -> Optional[int]:
    """Best effort: never raises, so saving a model config cannot fail because of it."""
    try:
        reported = await _ask_provider(config, transport)
        if reported:
            return reported
    except Exception as error:
        logger.info('Context window not reported for %s: %s', config.get('model'), type(error).__name__)
    return KNOWN_CONTEXT_WINDOWS.get(config.get('model', ''))
