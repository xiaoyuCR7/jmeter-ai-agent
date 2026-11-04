import json
import asyncio
import logging
from typing import Optional
import httpx
import openai
import anthropic

from app.services.ai.adapters.factory import AIAdapterFactory
from app.core.lang_utils import language_instruction
from app.services.ai.prompts.test_analyzer import REPORT_SCHEMA, TEST_ANALYZER_SYSTEM_PROMPT, build_analysis_prompt
from app.services.ai.memory.fact_ledger import FactLedger
from app.services.ai.memory.validators import correction_request, fact_validator
from app.models.schemas import AIChatMessage, DetailedTestSummary, ReportAnalysis

logger = logging.getLogger(__name__)

# What the report says when the model could not write it, in the report's language.
FALLBACK = {
    'zh-CN': {'over_budget': '分析输入与输出预留超过上下文预算，未发送模型请求',
              'unusable': 'AI分析输出未通过结构校验，未采纳为结论',
              'failed': 'AI分析失败（{attempts}次尝试后）: {error}',
              'conclusion': 'AI分析不可用，请参考原始指标数据人工判断',
              'shrink': '请缩小分析输入或核对模型上下文预算；无需重新执行压测',
              'format': '请检查AI模型输出格式；无需重新执行压测',
              'config': '请检查AI模型配置和网络连接；无需重新执行压测'},
    'en': {'over_budget': 'The analysis input and reserved output exceed the context budget; the model was not called',
           'unusable': 'The model output did not pass the structure check and was not used',
           'failed': 'AI analysis failed after {attempts} attempts: {error}',
           'conclusion': 'AI analysis is unavailable; judge from the measured metrics',
           'shrink': 'Reduce the input or check the model context window; the test need not be rerun',
           'format': 'Check the model output format; the test need not be rerun',
           'config': 'Check the AI model settings and network access; the test need not be rerun'},
}


def _unavailable(lang: str, status: str, assessment: str, advice: str, **values) -> ReportAnalysis:
    text = FALLBACK['en' if lang == 'en' else 'zh-CN']
    return ReportAnalysis(analysis_status=status, overall_assessment=text[assessment].format(**values),
                          conclusion=text['conclusion'], recommendations=[text[advice]], language=lang)

# Timeout for LLM analysis call — prevents hanging on slow/unresponsive models
AI_ANALYSIS_TIMEOUT_SECONDS = 120
# Maximum retries for transient failures (rate limits, network errors)
AI_ANALYSIS_MAX_RETRIES = 2
# Thinking models spend this budget before they write; 4000 left some of them with an empty reply.
AI_ANALYSIS_MAX_TOKENS = 8000
# A short report gets read. What a model writes past these limits is what it thought least of.
FIELD_LIMITS = {'key_findings': 5, 'performance_bottlenecks': 3, 'recommendations': 3, 'risk_areas': 3,
                'limitations': 3, 'hypotheses': 3}


def check_numbers(analysis: ReportAnalysis, summary: DetailedTestSummary, execution_id: str,
                  extra_values=()) -> ReportAnalysis:
    """Compare the numbers the model wrote with what the run measured. Only numbers: explanations stay hypotheses."""
    if analysis.analysis_status != 'completed':
        return analysis
    parts = [analysis.overall_assessment, analysis.timeline_analysis, analysis.conclusion]
    for items in (analysis.key_findings, analysis.specific_issues, analysis.resource_bottlenecks,
                  analysis.performance_bottlenecks, analysis.error_analysis, analysis.risk_areas, analysis.limitations):
        parts += items
    fact = FactLedger.compute_from_summary(summary, execution_id)
    result = fact_validator.validate('\n'.join(parts), fact, extra_values)
    analysis.fact_check = {'checked': len(result.claims), 'mismatches': result.mismatches}
    analysis.evidence_status = ('mismatch' if result.mismatches else 'verified' if result.claims else 'unverified')
    return analysis


class TestAnalyzer:
    """AI测试报告分析器"""

    lang = 'zh-CN'  # The language of the report being written; analyze() sets it.

    def __init__(self, model_type: Optional[str] = None):
        self.adapter = AIAdapterFactory.create(model_type)
        self._exchange: list = []  # The prompt and the model's reply, for one correction round.

    async def analyze(self, summary_data: dict, test_config: dict, test_type: str,
                      evidence_text: str = '', lang: str = 'zh-CN') -> ReportAnalysis:
        """分析测试报告；evidence_text 是确定性证据（需求判定、基线对比、服务端指标）"""
        self.lang = lang
        prompt = build_analysis_prompt(summary_data, test_config, test_type, evidence_text, lang)

        chat_messages = [
            AIChatMessage(role="system", content=TEST_ANALYZER_SYSTEM_PROMPT + "\n\n" + language_instruction(lang)),
            AIChatMessage(role="user", content=prompt),
        ]
        try:
            chat_messages = self.adapter.prepare_chat_messages(chat_messages, response_tokens=AI_ANALYSIS_MAX_TOKENS)
        except ValueError:
            return _unavailable(lang, 'failed', 'over_budget', 'shrink')

        # Call LLM with timeout + retry for transient failures
        last_error = None
        for attempt in range(AI_ANALYSIS_MAX_RETRIES + 1):
            try:
                response = await asyncio.wait_for(
                    self.adapter.chat(chat_messages, temperature=0.3, max_tokens=AI_ANALYSIS_MAX_TOKENS, max_retries=0,
                                      json_mode=True, json_schema=REPORT_SCHEMA),
                    timeout=AI_ANALYSIS_TIMEOUT_SECONDS,
                )
                try:
                    parsed = self._parse_response(response)
                    self._exchange = [*chat_messages, AIChatMessage(role='assistant', content=response)]
                    return parsed
                except (ValueError, TypeError):
                    if attempt < AI_ANALYSIS_MAX_RETRIES:
                        logger.warning("AI analysis attempt %d/%d returned unusable output",
                                       attempt + 1, AI_ANALYSIS_MAX_RETRIES + 1)
                        continue
                    return _unavailable(lang, 'invalid', 'unusable', 'format')
            except asyncio.TimeoutError:
                last_error = f"LLM analysis timed out after {AI_ANALYSIS_TIMEOUT_SECONDS}s"
                logger.warning("AI analysis attempt %d/%d: %s",
                              attempt + 1, AI_ANALYSIS_MAX_RETRIES + 1, last_error)
            except Exception as e:
                # Provider messages may contain credentials, URLs or request content.
                last_error = type(e).__name__
                logger.warning("AI analysis attempt %d/%d failed: %s",
                              attempt + 1, AI_ANALYSIS_MAX_RETRIES + 1, last_error)
                status = getattr(e, 'status_code', None)
                retryable = isinstance(e, (httpx.TransportError, openai.APIConnectionError,
                                          anthropic.APIConnectionError)) or status in (408, 429) or (
                    isinstance(status, int) and 500 <= status < 600)
                if not retryable:
                    break
            if attempt < AI_ANALYSIS_MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)

        # All retries exhausted — return fallback
        return _unavailable(lang, 'failed', 'failed', 'config', attempts=attempt + 1, error=last_error)

    async def correct_numbers(self, mismatches: list[dict]) -> Optional[ReportAnalysis]:
        """The last analysis rewritten with its mismatched numbers fixed or removed; None if that fails."""
        if not self._exchange:
            return None
        messages = [*self._exchange, AIChatMessage(role='user', content=correction_request(
            mismatches, '输出改正后的完整 JSON，字段和结构与上次相同。', self.lang))]
        try:
            messages = self.adapter.prepare_chat_messages(messages, response_tokens=AI_ANALYSIS_MAX_TOKENS)
            response = await asyncio.wait_for(
                self.adapter.chat(messages, temperature=0.0, max_tokens=AI_ANALYSIS_MAX_TOKENS, max_retries=0,
                                  json_mode=True, json_schema=REPORT_SCHEMA),
                timeout=AI_ANALYSIS_TIMEOUT_SECONDS)
            return self._parse_response(response)
        except Exception as error:
            logger.warning("Number correction failed: %s", type(error).__name__)
            return None

    @staticmethod
    def _extract_balanced_json(text: str) -> Optional[str]:
        """Return the first top-level balanced {...} object in text, or None."""
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start=start):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None

    def _parse_response(self, response: str) -> ReportAnalysis:
        """Extract a balanced JSON object, including fenced or nested output."""
        text = response.strip()
        # Keep a top-level array invalid; do not silently extract one of its objects.
        if text.startswith('['):
            raise ValueError('Expected one analysis object')
        data = json.loads(self._extract_balanced_json(text) or text)
        if not isinstance(data, dict):
            raise ValueError('Expected one analysis object')
        # Validation status belongs to the application, never the model.
        data['analysis_status'] = 'completed'
        data['evidence_status'] = 'unverified'
        for name, limit in FIELD_LIMITS.items():
            if isinstance(data.get(name), list):
                data[name] = data[name][:limit]
        data['language'] = self.lang
        result = ReportAnalysis.model_validate(data)
        if not result.overall_assessment.strip() or not result.conclusion.strip():
            raise ValueError('Analysis assessment and conclusion are required')
        return result
