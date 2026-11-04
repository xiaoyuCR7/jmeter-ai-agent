"""Language detection utilities — extract user's preferred language from request.

Priority: query param 'lang' > Accept-Language header > body field 'lang' > default.

Usage:
    from app.core.lang_utils import get_request_lang, get_lang_system_prompt

    lang = get_request_lang(request)
    # 'zh-CN' or 'en'

    prompt_suffix = get_lang_system_prompt(lang)
    # Appends language instruction to agent system prompt.
"""

from fastapi import Request


def get_request_lang(request: Request = None, body: dict = None, default: str = "zh-CN") -> str:
    """Extract preferred language from the request context.

    Checks, in order:
    1. Query parameter: ?lang=zh-CN or ?lang=en
    2. Accept-Language header
    3. Request body field: {"lang": "en"}
    4. Default: "zh-CN"
    """
    lang = None

    # 1. Query parameter
    if request is not None:
        try:
            qp_lang = request.query_params.get("lang")
            if qp_lang in ("zh-CN", "zh", "cn", "chinese"):
                return "zh-CN"
            if qp_lang in ("en", "en-US", "en-GB", "english"):
                return "en"
        except Exception:
            pass

        # 2. Header
        try:
            header_lang = request.headers.get("Accept-Language", "")
            if header_lang.lower().startswith("en"):
                return "en"
            if header_lang.lower().startswith("zh"):
                return "zh-CN"
        except Exception:
            pass

    # 3. Body field
    if body is not None:
        body_lang = body.get("lang", "")
        if body_lang in ("en", "en-US", "en-GB", "english"):
            return "en"
        if body_lang in ("zh-CN", "zh", "cn", "chinese"):
            return "zh-CN"

    return default


def get_lang_system_prompt(lang: str) -> str:
    """Get the language instruction to append to agent system prompts.

    Args:
        lang: 'zh-CN' or 'en'

    Returns a string that instructs the LLM which language to use.
    """
    if lang == "en":
        return (
            "\n\n## Language Requirement\n"
            "You MUST respond entirely in **English**. All analysis, recommendations, "
            "test plan descriptions, and comments must be written in English. "
            "Do NOT use Chinese or any other language in your output."
        )
    else:
        return (
            "\n\n## 语言要求\n"
            "你必须使用**中文**回复。所有分析、建议、测试计划描述和注释都必须用中文撰写。"
            "不要在输出中使用英文或其他语言。"
        )


def localize_prompt(prompt: str, lang: str) -> str:
    """Wrap a prompt with language instructions for the given language.

    Args:
        prompt: The original prompt.
        lang: 'zh-CN' or 'en'

    Returns the prompt with language instructions appended.
    """
    return prompt + get_lang_system_prompt(lang)


def normalize_lang(value) -> "str | None":
    """'en' or 'zh-CN' for the spellings people use, None for anything else."""
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in ("en", "en-us", "en-gb", "english"):
        return "en"
    if lowered in ("zh-cn", "zh", "cn", "chinese"):
        return "zh-CN"
    return None


def report_lang(value=None) -> str:
    """The language a report is written in: the one asked for, else REPORT_LANGUAGE, else the default."""
    from app.core.config import settings
    return (normalize_lang(value) or normalize_lang(settings.REPORT_LANGUAGE)
            or normalize_lang(settings.I18N_DEFAULT_LANG) or "zh-CN")


def language_instruction(lang: str) -> str:
    """The line that tells a model which language to write in."""
    return ("Write every sentence of your answer in English, whatever language the data is in."
            if lang == "en" else "用中文写回答。")
