"""The language of the person a message is for.

Messages the backend writes itself (why a run was stopped, why a request was refused, what a plan check
found) used to be Chinese whatever the UI showed. Each now carries both wordings:

    raise ValueError(tr('计划没有启用的线程组。', 'The plan has no enabled thread group.'))

``tr`` picks by the current request's language, which ``RequestLanguageMiddleware`` reads from the
``lang`` query parameter or the Accept-Language header the UI sends with every call. Work started by a
request (a run, a capacity search) inherits it, since a task copies its creator's context. With no
request in sight, as for an MCP client or a scheduled job, I18N_DEFAULT_LANG decides.

Text written for a model (prompts, evidence) stays in one language; the model is told separately which
language to answer in.
"""
from contextvars import ContextVar
from typing import Optional
from urllib.parse import parse_qs

_request_lang: ContextVar[Optional[str]] = ContextVar('request_lang', default=None)


def normalize_lang(value) -> Optional[str]:
    """'en' or 'zh-CN' for the spellings people and browsers use, None for anything else."""
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in ('en', 'english') or lowered.startswith('en-') or lowered.startswith('en_'):
        return 'en'
    if lowered in ('zh', 'cn', 'chinese') or lowered.startswith('zh-') or lowered.startswith('zh_'):
        return 'zh-CN'
    return None


def current_lang() -> str:
    lang = _request_lang.get()
    if lang:
        return lang
    from app.core.config import settings
    return normalize_lang(settings.I18N_DEFAULT_LANG) or 'zh-CN'


def set_lang(lang: Optional[str]):
    """Set the language for the current context; returns the token ``reset_lang`` takes."""
    return _request_lang.set(normalize_lang(lang))


def reset_lang(token) -> None:
    _request_lang.reset(token)


def tr(zh: str, en: str, lang: Optional[str] = None) -> str:
    """``en`` when the reader's language is English, else ``zh``."""
    return en if (normalize_lang(lang) or current_lang()) == 'en' else zh


def language_of(scope) -> Optional[str]:
    """The language an ASGI request asks for: ``?lang=`` first, then the first Accept-Language entry."""
    try:
        query = parse_qs(scope.get('query_string', b'').decode('latin-1'))
        lang = normalize_lang((query.get('lang') or [''])[0])
        if lang:
            return lang
        for name, value in scope.get('headers', []):
            if name.lower() == b'accept-language':
                first = value.decode('latin-1').split(',')[0].split(';')[0]
                return normalize_lang(first)
    except Exception:
        return None
    return None


class RequestLanguageMiddleware:
    """Makes the request's language the current one for everything the request does or starts."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        token = _request_lang.set(language_of(scope))
        try:
            return await self.app(scope, receive, send)
        finally:
            _request_lang.reset(token)
