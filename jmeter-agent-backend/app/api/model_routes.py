"""AI model settings: the models people added, their context windows and a connection test."""
import asyncio

from app.core.errors import ServiceError
from fastapi import APIRouter, HTTPException
from app.models.schemas import AIChatMessage, AIModelConfig
from app.core.config import (
    get_all_model_configs, load_user_model_configs, save_user_model_configs, sanitize_model_config,
)
from app.services.ai.adapters.factory import AIAdapterFactory, HEALTH_CHECK_TIMEOUT_SECONDS
from app.core.text import tr

router = APIRouter()


async def _fill_context_window(config: dict) -> dict:
    """Keep a user-entered size; otherwise ask the provider or use the documented size."""
    from app.services.ai.providers import needs_key
    has_key = config.get("api_key") not in (None, "", "***") or not needs_key(config)
    if not config.get("context_window") and has_key and config.get("adapter_type") != "azure_openai":
        from app.services.ai.model_limits import detect_context_window
        config = {**config, "context_window": await detect_context_window(config)}
    return config


def _load_and_merge_model_config(model_id: str, payload: dict) -> dict:
    """Merge an update payload with the existing config, preserving api_key when
    the frontend sends the redacted placeholder.
    """
    configs = load_user_model_configs()
    existing = next((c for c in configs if c.get("id") == model_id), None)
    if existing and payload.get("api_key") == "***":
        payload = {**payload, "api_key": existing.get("api_key", "")}
    return payload


# ===== AI Models =====
@router.get("/ai/providers", response_model=list)
async def get_providers():
    """设置页可选的模型服务商：地址、适配器、常用模型和是否需要 Key"""
    from app.services.ai.providers import catalog
    return catalog()


@router.get("/ai/models", response_model=list)
async def get_models():
    """获取可用的 AI 模型列表（仅用户已添加的模型，API Key 已脱敏）"""
    return [sanitize_model_config(c) for c in get_all_model_configs()]


# ===== AI Model Configs (frontend compatible REST API) =====
@router.get("/ai/model-configs", response_model=list)
async def get_model_configs():
    """获取所有 AI 模型配置（供前端设置页面使用，API Key 已脱敏）"""
    return [sanitize_model_config(c) for c in get_all_model_configs()]


@router.post("/ai/model-configs")
async def add_model_config(model: AIModelConfig):
    """添加或更新 AI 模型配置"""
    configs = load_user_model_configs()
    data = model.model_dump()
    for i, config in enumerate(configs):
        if config.get("id") == model.id:
            config = await _fill_context_window(_load_and_merge_model_config(model.id, data))
            configs[i] = config
            save_user_model_configs(configs)
            await AIAdapterFactory.invalidate(model.id)
            return {"success": True, "message": tr("模型配置已更新", "Model settings updated"), "config": sanitize_model_config(config)}
    data = await _fill_context_window(data)
    configs.append(data)
    save_user_model_configs(configs)
    await AIAdapterFactory.invalidate(model.id)
    return {"success": True, "message": tr("模型配置已添加", "Model added"), "config": sanitize_model_config(data)}


@router.put("/ai/model-configs/{model_id}")
async def update_model_config(model_id: str, model: AIModelConfig):
    """更新 AI 模型配置"""
    configs = load_user_model_configs()
    for i, config in enumerate(configs):
        if config.get("id") == model_id:
            data = await _fill_context_window(_load_and_merge_model_config(model_id, model.model_dump()))
            configs[i] = data
            save_user_model_configs(configs)
            await AIAdapterFactory.invalidate(model_id)
            return {"success": True, "message": tr("模型配置已更新", "Model settings updated"), "config": sanitize_model_config(data)}
    raise HTTPException(status_code=404, detail="Model config not found")


@router.delete("/ai/model-configs/{model_id}")
async def delete_model_config(model_id: str):
    """删除 AI 模型配置"""
    configs = load_user_model_configs()
    configs = [c for c in configs if c.get("id") != model_id]
    save_user_model_configs(configs)
    await AIAdapterFactory.invalidate(model_id)
    return {"success": True, "message": tr("模型配置已删除", "Model removed")}


@router.post("/ai/model-configs/{model_id}/test")
async def test_model_config(model_id: str):
    """测试 AI 模型配置是否可用"""
    try:
        config = AIAdapterFactory.get_model_config(model_id)
        if not config:
            raise HTTPException(status_code=404, detail="Model config not found")

        adapter = AIAdapterFactory.create(model_id)
        response = await asyncio.wait_for(adapter.chat([
            AIChatMessage(role="user", content="Hello, please reply with a short greeting.")
        ], max_tokens=32, temperature=0, max_retries=0), timeout=HEALTH_CHECK_TIMEOUT_SECONDS)
        return {"success": True, "message": tr("连接成功", "Connected"), "response": response}
    except (HTTPException, ServiceError):
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=tr(f"连接失败: {type(e).__name__}", f"Connection failed: {type(e).__name__}")) from e
