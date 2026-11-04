"""The model providers the settings page offers, and what each needs.

Picking one fills in the API address and the adapter, so people only paste a key and choose a
model. Most providers speak the OpenAI chat API; Claude has its own, and Azure OpenAI needs an
API version. Model names change often: the lists are suggestions, and any name can be typed in.
Model lists follow each provider's documentation as of 2026-11.
"""
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.parse import urlparse

OPENAI_COMPATIBLE = 'openai_compatible'
ANTHROPIC = 'anthropic'
AZURE_OPENAI = 'azure_openai'
ADAPTER_TYPES = (OPENAI_COMPATIBLE, ANTHROPIC, AZURE_OPENAI)
AZURE_API_VERSION = '2024-10-21'
# Keyless servers on this machine still need some value in the Authorization header.
LOCAL_KEY = 'not-needed'


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    label_en: str
    group: str  # global, china, local or custom
    api_base: str = ''
    adapter_type: str = OPENAI_COMPATIBLE
    models: tuple = ()
    key_required: bool = True
    base_editable: bool = False  # The address depends on the user's account, region or machine.
    api_version: Optional[str] = None
    docs: str = ''
    note: str = ''  # Shown under the model field.
    note_en: str = ''


PROVIDERS: tuple[Provider, ...] = (
    # International
    Provider('openai', 'OpenAI', 'OpenAI', 'global', 'https://api.openai.com/v1',
             models=('gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna'), docs='https://platform.openai.com/docs/models'),
    Provider('anthropic', 'Claude (Anthropic)', 'Claude (Anthropic)', 'global', '', ANTHROPIC,
             models=('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-1'),
             docs='https://docs.anthropic.com/en/docs/about-claude/models'),
    Provider('gemini', 'Google Gemini', 'Google Gemini', 'global',
             'https://generativelanguage.googleapis.com/v1beta/openai',
             models=('gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-2.5-flash-lite'),
             docs='https://ai.google.dev/gemini-api/docs/openai'),
    Provider('azure_openai', 'Azure OpenAI', 'Azure OpenAI', 'global', 'https://<resource>.openai.azure.com',
             AZURE_OPENAI, base_editable=True, api_version=AZURE_API_VERSION,
             note='模型填部署名（deployment）', note_en='Enter the deployment name as the model', docs='https://learn.microsoft.com/azure/ai-services/openai/'),
    Provider('xai', 'xAI Grok', 'xAI Grok', 'global', 'https://api.x.ai/v1', models=('grok-4', 'grok-3-mini'),
             docs='https://docs.x.ai/docs/models'),
    Provider('mistral', 'Mistral AI', 'Mistral AI', 'global', 'https://api.mistral.ai/v1',
             models=('mistral-large-latest', 'mistral-medium-latest', 'codestral-latest'),
             docs='https://docs.mistral.ai/getting-started/models/'),
    Provider('cohere', 'Cohere', 'Cohere', 'global', 'https://api.cohere.ai/compatibility/v1',
             models=('command-a-03-2025',), docs='https://docs.cohere.com/docs/compatibility-api'),
    Provider('groq', 'Groq', 'Groq', 'global', 'https://api.groq.com/openai/v1',
             models=('llama-3.3-70b-versatile', 'openai/gpt-oss-120b'), docs='https://console.groq.com/docs/models'),
    Provider('openrouter', 'OpenRouter', 'OpenRouter', 'global', 'https://openrouter.ai/api/v1',
             models=('openrouter/auto',), docs='https://openrouter.ai/models'),
    Provider('together', 'Together AI', 'Together AI', 'global', 'https://api.together.xyz/v1',
             models=('meta-llama/Llama-3.3-70B-Instruct-Turbo', 'deepseek-ai/DeepSeek-V3'),
             docs='https://docs.together.ai/docs/serverless-models'),
    # China
    Provider('deepseek', 'DeepSeek 深度求索', 'DeepSeek', 'china', 'https://api.deepseek.com/v1',
             models=('deepseek-flash', 'deepseek-v4-pro'), docs='https://api-docs.deepseek.com/'),
    Provider('qwen', 'Qwen 通义千问', 'Qwen (Alibaba Cloud)', 'china',
             'https://dashscope.aliyuncs.com/compatible-mode/v1', models=('qwen-plus', 'qwen-flash', 'qwen-max'),
             docs='https://help.aliyun.com/zh/model-studio/'),
    Provider('moonshot', 'Kimi 月之暗面', 'Kimi (Moonshot AI)', 'china', 'https://api.moonshot.cn/v1',
             models=('kimi-k2-turbo-preview', 'kimi-latest'), docs='https://platform.moonshot.cn/docs'),
    Provider('zhipu', '智谱 GLM', 'Zhipu GLM', 'china', 'https://open.bigmodel.cn/api/paas/v4',
             models=('glm-4.6', 'glm-4.5-air'), docs='https://docs.bigmodel.cn/'),
    Provider('doubao', '豆包（火山方舟）', 'Doubao (Volcengine Ark)', 'china', 'https://ark.cn-beijing.volces.com/api/v3',
             models=('doubao-seed-1-6-250615',), note='也可以填推理接入点 ID（ep-...）', note_en='An endpoint ID (ep-...) also works',
             docs='https://www.volcengine.com/docs/82379'),
    Provider('qianfan', '文心一言（百度千帆）', 'ERNIE (Baidu Qianfan)', 'china', 'https://qianfan.baidubce.com/v2',
             models=('ernie-4.5-turbo-128k', 'ernie-x1-turbo-32k'), docs='https://cloud.baidu.com/doc/qianfan/'),
    Provider('hunyuan', '腾讯混元', 'Tencent Hunyuan', 'china', 'https://api.hunyuan.cloud.tencent.com/v1',
             models=('hunyuan-turbos-latest', 'hunyuan-t1-latest'), docs='https://cloud.tencent.com/document/product/1729'),
    Provider('minimax', 'MiniMax', 'MiniMax', 'china', 'https://api.minimaxi.com/v1', models=('MiniMax-M2',),
             docs='https://platform.minimaxi.com/docs'),
    Provider('stepfun', '阶跃星辰', 'StepFun', 'china', 'https://api.stepfun.com/v1', models=('step-2-16k',),
             docs='https://platform.stepfun.com/docs'),
    Provider('siliconflow', '硅基流动', 'SiliconFlow', 'china', 'https://api.siliconflow.cn/v1',
             models=('deepseek-ai/DeepSeek-V3', 'Qwen/Qwen3-235B-A22B'), docs='https://docs.siliconflow.cn/'),
    # On this machine or in the user's network
    Provider('ollama', 'Ollama（本地）', 'Ollama (local)', 'local', 'http://127.0.0.1:11434/v1',
             models=('qwen3', 'llama3.1', 'deepseek-r1'), key_required=False, base_editable=True,
             docs='https://ollama.com/library'),
    Provider('lmstudio', 'LM Studio（本地）', 'LM Studio (local)', 'local', 'http://127.0.0.1:1234/v1',
             key_required=False, base_editable=True, docs='https://lmstudio.ai/docs'),
    Provider('vllm', 'vLLM / 自建 OpenAI 兼容服务', 'vLLM / self-hosted', 'local', 'http://127.0.0.1:8001/v1',
             key_required=False, base_editable=True, docs='https://docs.vllm.ai/'),
    # Anything else
    Provider('custom', '其它 OpenAI 兼容接口', 'Other OpenAI-compatible API', 'custom', '', base_editable=True),
    Provider('custom_anthropic', '其它 Anthropic 兼容接口', 'Other Anthropic-compatible API', 'custom', '', ANTHROPIC,
             base_editable=True),
)
BY_KEY = {provider.key: provider for provider in PROVIDERS}


def catalog() -> list[dict]:
    return [{**asdict(provider), 'models': list(provider.models)} for provider in PROVIDERS]


def is_local(api_base: Optional[str]) -> bool:
    host = (urlparse(api_base or '').hostname or '').lower()
    return host in ('localhost', '127.0.0.1', '::1') or host.endswith('.localhost')


def needs_key(config: dict) -> bool:
    """A key is needed unless the provider runs without one, or the endpoint is on this machine."""
    provider = BY_KEY.get(config.get('provider') or '')
    if provider is not None and not provider.key_required:
        return False
    return not is_local(config.get('api_base'))
