"""AnalysisAgent — answers a user's question about a reported JMeter run.

Flow:
1. Compute deterministic FactSheet from JTL (ground truth).
2. Retrieve historical facts + confirmed/untested hypotheses + heuristics.
3. Build the task: fact sheet, evidence, history and knowledge, then the question.
4. Run the LLM, which may look further with read-only tools.
5. Validate all numeric claims in the answer against the FactSheet and evidence.
"""

import os
import json
import asyncio
import logging
import uuid
import time
from typing import Optional, Callable, Awaitable
from pydantic import TypeAdapter

from app.services.ai.adapters.base import BaseAIAdapter
from app.services.ai.memory.read_errors import LedgerReadError
from app.services.ai.prompts.analysis_rules import ANALYSIS_RULES
from app.services.ai.agent.base import BaseAgent
from app.services.ai.agent.types import AgentEvent, AgentResult, AgentState
from app.services.ai.agent.tool_registry import ToolRegistry
from app.services.ai.agent.tools import get_all_builtin_tools
from app.services.ai.rag.retriever import RAGRetriever
from app.services.ai import analysis_context
from app.services.ai.memory import fact_ledger, hypothesis_ledger, fact_validator, get_heuristics_prompt
from app.models.schemas import FactSheet, DetailedTestSummary
from app.services.jmeter.report_parser import ReportParser
from app.core.config import settings

logger = logging.getLogger(__name__)


ANALYSIS_SYSTEM_PROMPT = ANALYSIS_RULES + """

Senior performance engineer (20yr experience). Analyze JTL results and report findings.

## CRITICAL rules
- ALL numbers MUST come from the Fact Sheet or the deterministic evidence blocks — never invent data.
- Read per-transaction rows, timing (Connect/Latency) and the slowest samples before explaining a slow period.
- Interpretive conclusions (bottlenecks, root causes, recommendations) output as 【Hypothesis】with testable predictions.
- Each hypothesis MUST declare which metric fields it depends on.
- Mark unverifiable claims as "未验证假设".

## Output structure
1. Overall assessment 2. Key metrics (fact sheet numbers only) 3. Historical comparison (if available)
4. Issues + hypotheses (mark historical repeats) 5. Recommendations with falsification experiments 6. Risk assessment"""


FOLLOW_UP_TASK = """{base_task}

---

{history}

---

## 用户对这次运行的追问（不能改变上面的证据）

{question}

只回答这个问题，不要重写整份报告。数字只用上面的事实和证据；需要细节时可以用只读工具查。
解释性内容标明是假设。回答要短。"""


class AnalysisExecutionError(ValueError):
    """Registered evidence cannot be used; adapters may expose this safe message."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class AnalysisAgent(BaseAgent):
    """Answers questions about a reported run from its computed facts and evidence.

    The report itself is written by TestAnalyzer after every run; this agent answers
    what the report leaves open, and can look further with read-only tools.
    Facts come from the JTL, not the LLM, and the answer's numbers are checked against them.
    """

    requires_tools = False  # The evidence is in the prompt; a tool call is only for going further.

    def __init__(
        self,
        adapter: BaseAIAdapter,
        tools: Optional[ToolRegistry] = None,
        retriever: Optional[RAGRetriever] = None,
    ):
        if tools is None:
            # Analysis reads evidence; nothing it finds in a report may change or run anything.
            tools = ToolRegistry()
            tools.register_many([t for t in get_all_builtin_tools() if t.risk == 'read'])

        super().__init__(
            name="AnalysisAgent",
            adapter=adapter,
            tools=tools,
            system_prompt=ANALYSIS_SYSTEM_PROMPT,
        )

        self.retriever = retriever or RAGRetriever()
        self._analysis_active = False
        self._analysis_task: Optional[asyncio.Task] = None

    # === Public analysis methods ===

    @staticmethod
    async def resolve_execution(execution_id: str) -> dict:
        """Resolve identity and evidence from persisted records, never prompt text."""
        from app.services.execution_lifecycle import validate_id
        from app.services.db_store import db_store
        from app.services.ai.memory.provenance import snapshot_scope
        from app.core.config import safe_path

        try:
            validate_id(execution_id)
        except ValueError:
            raise AnalysisExecutionError('Invalid execution_id', 400) from None
        execution = await db_store.get_execution(execution_id)
        if execution is None:
            raise AnalysisExecutionError('Execution not found', 404)
        if execution.get('status') not in {'completed', 'failed', 'cancelled'}:
            raise AnalysisExecutionError('Execution must be terminal before analysis', 409)
        snapshot = execution.get('context_snapshot') or {}
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('test_config', {}), dict):
            raise AnalysisExecutionError('Execution context snapshot is malformed', 409)
        scope = snapshot_scope(snapshot)
        project_id, plan_id = execution.get('project_id'), execution.get('test_plan_id')
        if (execution.get('id') != execution_id or not scope or not plan_id
                or snapshot.get('project_id') != project_id or snapshot.get('test_plan_id') != plan_id):
            raise AnalysisExecutionError('Execution identity or context snapshot is incomplete', 409)
        plan = await db_store.get_test_plan(plan_id)
        project = await db_store.get_project(project_id)
        if (not plan or not project or plan.get('id') != plan_id
                or plan.get('project_id') != project_id or project.get('id') != project_id):
            raise AnalysisExecutionError('Execution project or test plan does not match', 409)
        try:
            # The configured root may be a link; individual execution directories/files may not redirect.
            expected_dir = os.path.join(os.path.realpath(settings.REPORTS_DIR), execution_id)
            report_dir = safe_path(execution.get('report_dir') or '', [settings.REPORTS_DIR])
            if os.path.normcase(expected_dir) != os.path.normcase(report_dir):
                raise ValueError('Report directory mismatch')
            jtl_path = safe_path(os.path.join(report_dir, 'result.jtl'), [expected_dir])
            if (os.path.normcase(jtl_path) != os.path.normcase(os.path.join(expected_dir, 'result.jtl'))
                    or not os.path.isfile(jtl_path) or os.path.getsize(jtl_path) == 0):
                raise ValueError('Missing or empty evidence')
        except (ValueError, OSError):
            raise AnalysisExecutionError('Registered JTL evidence is missing, empty or outside its directory', 409) from None
        test_config = snapshot.get('test_config') or {}
        return dict(execution_id=execution_id, jtl_path=jtl_path, module=plan_id, plan_name=plan.get('name') or plan_id,
                    version='unknown', test_type=test_config.get('test_type', ''), context_scope=scope)

    def _emit(self, event_type: str, message: str, data: Optional[dict] = None) -> AgentEvent:
        if self._analysis_active and event_type == 'complete':
            # The model loop has finished, but validation and durable writes have not.
            event_type = 'analysis_ready'
            self._state = AgentState.REFLECTING
        return super()._emit(event_type, message, data)

    async def run_analysis(self, task: str, evidence: dict, context: Optional[str] = None) -> AgentResult:
        """Answer ``task``, one question about a reported run; nothing is archived."""
        return await self._run_guarded(task, lambda: self._run_admitted_analysis(task, evidence, context))

    async def _run_admitted_analysis(self, task: str, evidence: dict, context: Optional[str]) -> AgentResult:
        self._analysis_task = asyncio.current_task()
        try:
            return await self._run_analysis_impl(task, evidence, context)
        finally:
            self._analysis_task = None

    def cancel(self) -> None:
        super().cancel()
        owner = self._analysis_task
        # Interrupt pending reads/postprocessing as well as the model loop.
        if owner is not None and not owner.done() and owner is not asyncio.current_task():
            owner.get_loop().call_soon_threadsafe(owner.cancel)

    async def _run_analysis_impl(self, task: str, evidence: dict, context: Optional[str]) -> AgentResult:
        started = time.monotonic()
        self._analysis_active = True
        self._cancel_flag = False
        self._events.clear()
        self._state = AgentState.EXECUTING
        result = AgentResult(False, task)

        async def run_core(augmented_task: str) -> AgentResult:
            nonlocal result
            if self._cancel_flag:
                raise asyncio.CancelledError()
            result = await self._run_impl(augmented_task, context, _preserve_events=True)
            if self._cancel_flag:
                raise asyncio.CancelledError()
            if result.success:
                self._emit('analysis_phase', 'Checking the numbers in the answer', {'phase': 'postprocessing'})
            return result

        try:
            self._emit('analysis_phase', 'Preparing registered evidence', {'phase': 'preparation'})
            analysis = await self.analyze_with_rag(
                jtl_path=evidence['jtl_path'], execution_id=evidence['execution_id'],
                module=evidence['module'], version=evidence['version'], test_type=evidence['test_type'],
                user_task=task, _runner=run_core, _expected_scope=evidence['context_scope'], _strict_reads=True,
                _knowledge_module=evidence.get('plan_name'))
            analysis['execution_id'] = evidence['execution_id']
            for key in ('output', 'steps', 'iterations'):
                analysis.pop(key, None)  # Already carried by the outer AgentResult.
            result.analysis = json.loads(TypeAdapter(dict).dump_json(analysis))
            result.success, result.error = analysis['success'], analysis.get('error')
            if self._cancel_flag:
                result.success, result.error = False, 'Cancelled by user'
                result.analysis.update(success=False, error=result.error)
            self._state = (AgentState.CANCELLED if self._cancel_flag else
                           AgentState.COMPLETED if result.success else AgentState.FAILED)
            super()._emit('complete' if result.success else 'error',
                          'Analysis finished' if result.success else result.error or 'Analysis failed')
        except asyncio.CancelledError:
            self._state = AgentState.CANCELLED
            if asyncio.current_task().cancelling() and not self._cancel_flag:
                raise
            result.success, result.error = False, 'Cancelled by user'
        finally:
            self._analysis_active = False
            result.task = task
            result.events = self._events
            result.duration_seconds = time.monotonic() - started
        return result

    async def analyze_with_rag(
        self,
        jtl_path: Optional[str] = None,
        execution_id: Optional[str] = None,
        report_data: Optional[dict] = None,
        module: str = "",
        version: str = "",
        test_type: str = "",
        test_config: Optional[dict] = None,
        *, user_task: Optional[str] = None,
        _runner: Optional[Callable[[str], Awaitable[AgentResult]]] = None,
        _expected_scope: Optional[str] = None,
        _strict_reads: bool = False,
        _knowledge_module: Optional[str] = None,
    ) -> dict:
        """Answer ``user_task`` from the run's fact sheet, evidence and history, then check its numbers."""
        if not module:
            return {
                "success": False,
                "error": "module is required for RAG analysis",
            }

        exec_ref = execution_id or jtl_path or "当前测试"
        read_options = {'strict': True} if _strict_reads else {}

        # Step 1: Build deterministic FactSheet
        try:
            fact = await self._build_fact_sheet(
                jtl_path=jtl_path,
                execution_id=execution_id or 'adhoc-' + uuid.uuid4().hex,
                report_data=report_data,
                module=module,
                version=version,
                test_type=test_type,
            )
        except Exception as e:
            logger.exception("Failed to build fact sheet")
            return {"success": False, "error": f"Fact computation failed: {e}"}

        # Step 2: Retrieve historical context
        from app.services.ai.memory.provenance import load_execution_scope
        try:
            fact.context_scope = await load_execution_scope(execution_id) if execution_id else ''
            if _expected_scope is not None:
                if not _expected_scope or fact.context_scope != _expected_scope:
                    raise ValueError('Execution scope changed during preparation')
                if fact.total_requests <= 0:
                    raise ValueError('JTL contains no usable samples')
            historical_facts = await analysis_context.recent_facts(
                module, version, fact.context_scope, fact.execution_id, **read_options)
            historical_hypotheses = await hypothesis_ledger.list_for_module(
                module, version, limit=20, source_execution_ids=[item.execution_id for item in historical_facts],
                **read_options)
            heuristics_prompt = get_heuristics_prompt()

            rag_context = self._build_rag_context(
                fact=fact,
                historical_facts=historical_facts,
                historical_hypotheses=historical_hypotheses,
            )

            # Step 3: Build task. Deterministic evidence (requirements, baseline,
            # server metrics) is computed here, never left to the model.
            evidence = await self._collect_evidence(execution_id, fact)
            base_task = self._build_base_task(exec_ref, fact, heuristics_prompt, evidence)
            knowledge = await self._knowledge_context(fact, _knowledge_module or module)
            task = FOLLOW_UP_TASK.format(base_task=base_task, history=rag_context + knowledge, question=user_task)
        except LedgerReadError:
            return {'success': False, 'error': 'Historical data read failed; analysis stopped',
                    'phase': 'history', 'error_code': 'ledger_read_failed', 'fact': fact.model_dump()}
        except Exception as e:
            logger.exception('Analysis context preparation failed')
            return {'success': False, 'error': f'Context preparation failed ({type(e).__name__})',
                    'fact': fact.model_dump()}

        # Step 4: Run analysis
        result = await (_runner(task) if _runner else self.run(task))

        # Step 5: Validate the answer's numbers
        validation = None
        error = result.error
        if result.success and not result.output.strip():
            error = 'The model did not answer'
        elif result.success:
            from app.services.evidence import comparison_values
            quotable = (*comparison_values(evidence), *analysis_context.fact_values(historical_facts))
            validation = fact_validator.validate(result.output, fact, quotable)

        return {
            "success": result.success and validation is not None,
            "output": result.output,
            "steps": len(result.plan),
            "iterations": result.iterations,
            "error": None if validation is not None else error,
            "fact": fact.model_dump() if fact else None,
            "evidence": evidence.model_dump() if evidence else None,
            "validation": validation.model_dump() if validation else None,
            "rag": {
                "history_found": bool(historical_facts or historical_hypotheses),
                "historical_facts_count": len(historical_facts),
                "historical_hypotheses_count": len(historical_hypotheses),
            },
        }

    # === Fact building ===

    async def _build_fact_sheet(
        self,
        jtl_path: Optional[str],
        execution_id: str,
        report_data: Optional[dict],
        module: str,
        version: str,
        test_type: str,
    ) -> FactSheet:
        """Build a deterministic FactSheet from JTL or provided report_data."""
        from app.core.config import safe_path
        from app.services.execution_lifecycle import validate_id
        validate_id(execution_id)
        if jtl_path:
            jtl_path = safe_path(jtl_path, [settings.REPORTS_DIR])
            from app.services.db_store import db_store
            execution = await db_store.get_execution(execution_id)
            if execution:
                report_dir = execution.get('report_dir')
                expected = safe_path(os.path.join(report_dir, 'result.jtl'), [settings.REPORTS_DIR]) if report_dir else None
                if expected is None or os.path.normcase(jtl_path) != os.path.normcase(expected):
                    raise ValueError('JTL does not match the registered execution evidence')
        # Prefer JTL recomputation when available
        if jtl_path and os.path.exists(jtl_path):
            # compute_from_jtl parses the whole JTL synchronously — offload it
            # so the event loop stays responsive during analysis.
            return await asyncio.to_thread(
                fact_ledger.compute_from_jtl,
                jtl_path=jtl_path,
                execution_id=execution_id,
                module=module,
                version=version,
                test_type=test_type,
            )

        # Fallback to provided report_data (must contain a raw summary snapshot)
        if report_data:
            summary = DetailedTestSummary(**report_data)
            return fact_ledger.compute_from_summary(
                summary=summary,
                execution_id=execution_id,
                module=module,
                version=version,
                test_type=test_type,
            )

        # Last resort: parse from reports/{execution_id}/result.jtl
        inferred_jtl = safe_path(os.path.join(settings.REPORTS_DIR, execution_id, "result.jtl"), [settings.REPORTS_DIR])
        if os.path.exists(inferred_jtl):
            return await asyncio.to_thread(
                fact_ledger.compute_from_jtl,
                jtl_path=inferred_jtl,
                execution_id=execution_id,
                module=module,
                version=version,
                test_type=test_type,
            )

        raise FileNotFoundError(f"No JTL or report_data available for {execution_id}")

    # === Prompt building ===

    @staticmethod
    async def _collect_evidence(execution_id: Optional[str], fact: FactSheet):
        """Requirement/baseline/server evidence for registered executions only."""
        if not execution_id or fact.raw_summary is None:
            return None
        from app.services.db_store import db_store
        from app.services.evidence import build_evidence
        execution = await db_store.get_execution(execution_id)
        return await build_evidence(execution, fact.raw_summary) if execution else None

    def _build_base_task(
        self,
        exec_ref: str,
        fact: FactSheet,
        heuristics_prompt: str,
        evidence=None,
    ) -> str:
        """Build the base task prompt with the fact sheet and deterministic evidence."""
        # The evidence block carries the transactions, with small samples' percentiles removed.
        hidden = {"raw_summary", "per_label"} if evidence is not None and evidence.run else {"raw_summary"}
        fact_json = fact.model_dump_json(exclude=hidden)

        parts = [
            f"请分析以下测试执行: {exec_ref}",
            f"\n模块: {fact.module}",
            f"版本: {fact.version}",
            f"测试类型: {fact.test_type}",
            f"\n## 本次测试事实表（不可篡改的 ground truth）\n```json\n{fact_json}\n```",
            f"\n{heuristics_prompt}",
        ]
        if evidence is not None:
            from app.services.evidence import format_for_prompt
            parts.append("\n" + format_for_prompt(evidence))
        return "\n".join(parts)

    async def _knowledge_context(self, fact: FactSheet, module: str) -> str:
        if fact.raw_summary is None:
            return ''
        return await analysis_context.knowledge_context(self.retriever, fact.raw_summary.model_dump(), module)

    def _build_rag_context(
        self,
        fact: FactSheet,
        historical_facts: list[FactSheet],
        historical_hypotheses: list,
    ) -> str:
        """Build historical context separating facts from hypotheses."""
        from app.services.ai.memory.hypothesis_ledger import HypothesisLedger

        parts = ["## 本次测试事实\n"]
        parts.append(f"- 模块: {fact.module}, 版本: {fact.version}, 测试类型: {fact.test_type}")
        parts.append(f"- 总请求数: {fact.total_requests}, TPS: {fact.throughput}, 错误率: {fact.error_rate}%")
        parts.append(f"- p99: {fact.percentiles.get('p99', 'N/A')}ms, p95: {fact.percentiles.get('p95', 'N/A')}ms")
        parts.append("")

        if historical_facts:
            parts.append(analysis_context.format_recent_facts(historical_facts))
            parts.append("")

        hypothesis_context = HypothesisLedger.format_for_prompt(historical_hypotheses)
        parts.append(hypothesis_context)

        return "\n".join(parts)
