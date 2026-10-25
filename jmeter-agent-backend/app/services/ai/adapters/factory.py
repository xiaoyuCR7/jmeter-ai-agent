import logging
import asyncio
from typing import Optional, Dict

from app.core.config import settings, get_all_model_configs
from app.services.ai.adapters.base import BaseAIAdapter
from app.services.ai.adapters.openai_adapter import OpenAIAdapter
from app.services.ai.adapters.anthropic_adapter import AnthropicAdapter

logger = logging.getLogger(__name__)
HEALTH_CHECK_TIMEOUT_SECONDS = 15


class AIAdapterFactory:
    """AI adapter factory with caching and health check."""

    _adapters: Dict[str, BaseAIAdapter] = {}
    _retired: set[BaseAIAdapter] = set()

    @classmethod
    async def invalidate(cls, model_type: Optional[str] = None):
        """Drop cached adapter(s) so the next create() rebuilds from disk config.

        Call this after model configs are added, updated, or deleted.
        Pass None to clear the entire cache.
        """
        if model_type is None:
            retired = list(cls._adapters.values())
            cls._adapters.clear()
        else:
            adapter = cls._adapters.pop(model_type, None)
            retired = [adapter] if adapter is not None else []
        cls._retired.update(retired)
        for adapter in retired:
            await adapter.retire()
        cls._retired = {adapter for adapter in cls._retired if not getattr(adapter, '_closed', False)}

    @classmethod
    async def close_all(cls):
        await cls.invalidate()
        for adapter in list(cls._retired):
            await adapter.retire()
        cls._retired = {adapter for adapter in cls._retired if not getattr(adapter, '_closed', False)}
        if cls._retired:
            raise RuntimeError('AI clients still active or could not be closed')

    @classmethod
    def create(cls, model_type: Optional[str] = None) -> BaseAIAdapter:
        """Create or return a cached adapter for the given model."""
        model = model_type or settings.DEFAULT_AI_MODEL

        if model in cls._adapters:
            return cls._adapters[model]

        configs = get_all_model_configs()
        config_map = {c["id"]: c for c in configs}

        cfg = config_map.get(model)
        if not cfg and not model_type:
            # Model IDs come from the UI (e.g. "deepseek-flash"), so a fixed default name rarely
            # exists; without this, post-run analysis failed for anyone who had not set one.
            cfg = next((c for c in configs if c.get("enabled", True)), None)
            if cfg is None:
                raise ValueError("No AI model configured; add one in Settings -> AI models")
            model = cfg["id"]
            if model in cls._adapters:
                return cls._adapters[model]
        if not cfg:
            raise ValueError(f"AI model not configured: {model}")

        if not cfg.get("enabled", True):
            raise ValueError(f"AI model is disabled: {model}")

        adapter_type = cfg.get("adapter_type", "openai_compatible")
        api_key = cfg["api_key"]
        model_name = cfg["model"]
        api_base = cfg.get("api_base")

        if not api_key:
            raise ValueError(f"API key not configured for model: {model}")

        if adapter_type == "anthropic":
            adapter = AnthropicAdapter(
                api_key=api_key, model=model_name, api_base=api_base,
            )
        else:
            adapter = OpenAIAdapter(
                api_key=api_key, model=model_name, api_base=api_base,
            )

        adapter.set_context_window(cfg.get("context_window"))
        cls._adapters[model] = adapter
        return adapter

    @classmethod
    async def health_check(cls, model_id: str) -> dict:
        """Send a minimal request to verify the model is reachable."""
        import time
        from app.models.schemas import AIChatMessage
        try:
            adapter = cls.create(model_id)
            start = time.monotonic()
            await asyncio.wait_for(adapter.chat(
                [AIChatMessage(role="user", content="ping")],
                max_tokens=5, temperature=0, max_retries=0,
            ), timeout=HEALTH_CHECK_TIMEOUT_SECONDS)
            latency = (time.monotonic() - start) * 1000
            return {"healthy": True, "latency_ms": round(latency, 1), "error": None}
        except Exception as e:
            return {"healthy": False, "latency_ms": 0, "error": type(e).__name__}

    @staticmethod
    def get_adapter(model_type: Optional[str] = None) -> BaseAIAdapter:
        return AIAdapterFactory.create(model_type)

    @staticmethod
    def get_available_models() -> list:
        configs = get_all_model_configs()
        return [
            {"id": c["id"], "name": c["name"], "model": c["model"]}
            for c in configs if c.get("enabled", True)
        ]

    @staticmethod
    def get_model_config(model_id: str) -> Optional[dict]:
        configs = get_all_model_configs()
        for cfg in configs:
            if cfg["id"] == model_id:
                return cfg
        return None
