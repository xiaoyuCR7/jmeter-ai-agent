import json
import asyncio
import logging
from typing import Optional
import httpx
import openai
import anthropic

from app.services.ai.adapters.factory import AIAdapterFactory
from app.services.ai.prompts.test_analyzer import TEST_ANALYZER_SYSTEM_PROMPT, build_analysis_prompt
from app.services.ai.memory.fact_ledger import FactLedger
from app.services.ai.memory.validators import fact_validator
from app.models.schemas import DetailedTestSummary, ReportAnalysis

logger = logging.getLogger(__name__)

# Timeout for LLM analysis call — prevents hanging on slow/unresponsive models
AI_ANALYSIS_TIMEOUT_SECONDS = 120
# Maximum retries for transient failures (rate limits, network errors)
AI_ANALYSIS_MAX_RETRIES = 2
# Thinking models spend this budget before they write; 4000 left some of them with an empty reply.
AI_ANALYSIS_MAX_TOKENS = 8000


def check_numbers(analysis: ReportAnalysis, summary: DetailedTestSummary, execution_id: str,
                  extra_values=()) -> ReportAnalysis:
    """Compare the numbers the model wrote with what the run measured. Only numbers: explanations stay hypotheses."""
    if analysis.analysis_status != 'completed':
        return analysis
    parts = [analysis.overall_assessment, analysis.timeline_analysis, analysis.conclusion]
    for items in (analysis.key_findings, analysis.specific_issues, analysis.resource_bottlenecks,
                  analysis.performance_bottlenecks, analysis.error_analysis, analysis.risk_areas):
        parts += items
    fact = FactLedger.compute_from_summary(summary, execution_id)
    result = fact_validator.validate('\n'.join(parts), fact, extra_values)
    analysis.fact_check = {'checked': len(result.claims), 'mismatches': result.mismatches}
    analysis.evidence_status = ('mismatch' if result.mismatches else 'verified' if result.claims else 'unverified')
    return analysis


class TestAnalyzer:
    """AI测试报告分析器"""

    def __init__(self, model_type: Optional[str] = None):
        self.adapter = AIAdapterFactory.create(model_type)

    async def analyze(self, summary_data: dict, test_config: dict, test_type: str,
                      evidence_text: str = '') -> ReportAnalysis:
        """分析测试报告；evidence_text 是确定性证据（需求判定、基线对比、服务端指标）"""
        prompt = build_analysis_prompt(summary_data, test_config, test_type, evidence_text)

        messages = [
            {"role": "system", "content": TEST_ANALYZER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]

        # Convert to AIChatMessage format
        from app.models.schemas import AIChatMessage
        chat_messages = [AIChatMessage(role=m["role"], content=m["content"]) for m in messages]
        try:
            chat_messages = self.adapter.prepare_chat_messages(chat_messages, response_tokens=AI_ANALYSIS_MAX_TOKENS)
        except ValueError:
            return ReportAnalysis(analysis_status="failed",
                overall_assessment="分析输入与输出预留超过上下文预算，未发送模型请求",
                conclusion="AI分析不可用，请参考原始指标数据人工判断",
                recommendations=["请缩小分析输入或核对模型上下文预算；无需重新执行压测"])

        # Call LLM with timeout + retry for transient failures
        last_error = None
        for attempt in range(AI_ANALYSIS_MAX_RETRIES + 1):
            try:
                response = await asyncio.wait_for(
                    self.adapter.chat(chat_messages, temperature=0.3, max_tokens=AI_ANALYSIS_MAX_TOKENS, max_retries=0),
                    timeout=AI_ANALYSIS_TIMEOUT_SECONDS,
                )
                try:
                    return self._parse_response(response)
                except (ValueError, TypeError):
                    if attempt < AI_ANALYSIS_MAX_RETRIES:
                        logger.warning("AI analysis attempt %d/%d returned unusable output",
                                       attempt + 1, AI_ANALYSIS_MAX_RETRIES + 1)
                        continue
                    return ReportAnalysis(analysis_status="invalid",
                        overall_assessment="AI分析输出未通过结构校验，未采纳为结论",
                        conclusion="AI分析不可用，请参考原始指标数据人工判断",
                        recommendations=["请检查AI模型输出格式；无需重新执行压测"])
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
        return ReportAnalysis(
            analysis_status="failed",
            overall_assessment=f"AI分析失败（{attempt + 1}次尝试后）: {last_error}",
            key_findings=[],
            specific_issues=[],
            timeline_analysis="",
            resource_bottlenecks=[],
            performance_bottlenecks=[],
            error_analysis=[],
            recommendations=["请检查AI模型配置和网络连接；无需重新执行压测"],
            improvement_plan=[],
            risk_areas=[],
            conclusion="AI分析不可用，请参考原始指标数据人工判断"
        )

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
        result = ReportAnalysis.model_validate(data)
        if not result.overall_assessment.strip() or not result.conclusion.strip():
            raise ValueError('Analysis assessment and conclusion are required')
        return result
