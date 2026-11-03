"""What every finished run gets, whatever started it: a report, its archive and a place in the history.

The lifecycle calls ``finish_run`` for runs from the Web, agents, the scheduler and imports alike,
so a run's history does not depend on the button that started it.
"""
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime

from app.core.config import settings
from app.models.schemas import ReportAnalysis, TestExecutionInfo, TestExecutionStatus
from app.services.ai import usage
from app.services.ai.analyzer import TestAnalyzer, check_numbers
from app.services.execution_lifecycle import execution_lifecycle
from app.services.storage import store

logger = logging.getLogger(__name__)


def _fact_check_note(analysis: dict) -> str:
    check = analysis.get('fact_check') or {}
    fixed = f"初稿有 {check['corrected_from']} 处数字不符，已让模型改写。" if check.get('corrected_from') else ''
    return fixed + _fact_check_status(analysis, check)


def _fact_check_status(analysis: dict, check: dict) -> str:
    if analysis.get('evidence_status') == 'mismatch':
        wrong = '；'.join(f"{m['metric']} 写的是 {m['claimed']}，实测 {m['actual']}" for m in check.get('mismatches', []))
        return f"有数字与实测不符，请以原始指标为准：{wrong}。"
    if analysis.get('evidence_status') == 'verified':
        return f"已核对文中 {check.get('checked', 0)} 处指标数字，均与实测相符；解释性内容仍是推测，不等于已验证根因。"
    return "AI文本尚未逐项事实核验，请结合原始指标；推测不等于已验证根因。"


def save_analysis_markdown(report_dir: str, execution_id: str, analysis) -> str:
    """Save AI analysis as a readable Markdown file in the report directory.

    Returns the file path, or empty string on failure.
    """
    if not report_dir or not os.path.isdir(report_dir):
        return ""
    try:
        ad = analysis.model_dump() if hasattr(analysis, 'model_dump') else analysis
        md = f"""# AI 性能测试分析报告

**执行ID**: {execution_id}
**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

**分析状态**: {ad.get('analysis_status', 'completed')}。{_fact_check_note(ad)}

---

## 整体评估

{ad.get('overall_assessment', '无')}

## 关键发现

"""
        for item in ad.get('key_findings', []):
            md += f"- {item}\n"

        if ad.get('limitations'):
            md += "\n## 分析限制\n\n"
            for item in ad['limitations']:
                md += f"- {item}\n"

        if ad.get('specific_issues'):
            md += "\n## 具体问题定位\n\n"
            for item in ad['specific_issues']:
                md += f"- {item}\n"

        tl = ad.get('timeline_analysis', '')
        if tl:
            md += f"\n## 性能时间线分析\n\n{tl}\n"

        if ad.get('resource_bottlenecks'):
            md += "\n## 资源瓶颈推断\n\n"
            for item in ad['resource_bottlenecks']:
                md += f"- {item}\n"

        md += "\n## 性能瓶颈点\n\n"
        for item in ad.get('performance_bottlenecks', []):
            md += f"- {item}\n"

        md += "\n## 错误根因分析\n\n"
        for item in ad.get('error_analysis', []):
            md += f"- {item}\n"

        md += "\n## 优化建议\n\n"
        for item in ad.get('recommendations', []):
            md += f"- {item}\n"

        ip_list = ad.get('improvement_plan', [])
        if ip_list:
            md += "\n## 分步改进计划\n\n"
            for i, item in enumerate(ip_list, 1):
                md += f"{i}. {item}\n"

        risk = ad.get('risk_areas', [])
        if risk:
            md += "\n## 风险区域\n\n"
            for item in risk:
                md += f"- {item}\n"

        md += f"\n## 最终结论\n\n{ad.get('conclusion', '无')}\n"

        if ad.get('follow_ups'):
            md += "\n## 追问\n"
            for item in ad['follow_ups']:
                md += f"\n**{item.get('question')}**\n\n{item.get('answer')}\n"

        filepath = os.path.join(report_dir, "ai_analysis.md")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md)
        return filepath
    except Exception:
        return ""


async def analysis_evidence(execution):
    """The same requirement verdicts, baseline comparison and server metrics the report shows."""
    from app.services.evidence import build_evidence, comparison_values, format_for_prompt
    try:
        evidence = await build_evidence(execution, execution.summary)
    except Exception as error:
        logger.warning("Evidence unavailable for analysis of %s: %s", execution.id, error)
        return "", None, []
    req = evidence.requirements
    verdict = {"verdict": req.verdict, "passed": req.passed, "failed": req.failed, "missing": req.missing}
    from app.services.ai.analysis_context import recall
    remembered = await recall(execution)
    return (format_for_prompt(evidence) + remembered.text, verdict,
            [*comparison_values(evidence), *remembered.numbers])


HYPOTHESIS_MAX_AGE_DAYS = 30


async def record_facts(execution: TestExecutionInfo):
    """Give later analyses a measured history, and check the project's hypotheses waiting for a run like this."""
    from app.services.ai.analysis_context import run_context
    from app.services.ai.memory import fact_ledger, hypothesis_ledger
    from app.services.ai.memory.provenance import load_execution_scope
    jtl_path = os.path.join(execution.report_dir or '', 'result.jtl')
    scope = await load_execution_scope(execution.id)
    if not scope or not os.path.isfile(jtl_path):
        return
    config = (execution.context_snapshot or {}).get('test_config') or {}
    fact = await asyncio.to_thread(
        fact_ledger.compute_from_summary, summary=execution.summary, execution_id=execution.id, jtl_path=jtl_path,
        module=execution.test_plan_id, version='unknown', test_type=config.get('test_type', ''))
    fact.context_scope = scope
    for key, value in run_context(execution).items():
        setattr(fact, key, value)
    if await fact_ledger.save(fact):
        if fact.project_id:
            await hypothesis_ledger.evaluate_project_hypotheses(fact.project_id, execution.id)
            await hypothesis_ledger.expire(fact.project_id, HYPOTHESIS_MAX_AGE_DAYS)
        await record_hypotheses(execution, fact)


async def record_hypotheses(execution: TestExecutionInfo, fact):
    """Report explanations join the ledger as untested, so a later run can settle them."""
    from app.models.schemas import Hypothesis, HypothesisCategory, HypothesisStatus
    from app.services.ai.memory import hypothesis_ledger, prediction
    from app.services.ai.memory.hypothesis_ledger import REF_LENGTH, HypothesisUpdateConflict
    labels = set(fact.per_label or {})
    # Reports of a plan run again and again restate the same claim; the newest wording replaces the open one,
    # found by the number the report quotes or, failing that, by making the same prediction.
    open_hypotheses = [hyp for hyp in await hypothesis_ledger.list_for_project(
        fact.project_id, status=HypothesisStatus.UNTESTED, limit=200) if hyp.source_execution_id != execution.id]
    by_ref = {hyp.hypothesis_id[:REF_LENGTH]: hyp for hyp in open_hypotheses}
    open_claims = {}
    for hyp in open_hypotheses:
        if hyp.module == fact.module and hyp.prediction:
            open_claims.setdefault((hyp.category, prediction.signature(hyp.prediction)), []).append(hyp)
    replaced = set()
    for item in (execution.ai_analysis.hypotheses if execution.ai_analysis else []):
        statement = str(item.get('statement') or '').strip()
        if not statement:
            continue
        try:
            category = HypothesisCategory(item.get('category'))
        except ValueError:
            category = HypothesisCategory.BOTTLENECK
        # Without a checkable prediction it waits for a person to decide it on the report page.
        expected = prediction.normalize(item.get('prediction'), labels)
        hypothesis_id = hashlib.sha256(json.dumps([execution.id, statement], ensure_ascii=False).encode()).hexdigest()
        await hypothesis_ledger.add(Hypothesis(
            hypothesis_id=hypothesis_id,
            source_execution_id=execution.id, module=fact.module, version=fact.version, test_type=fact.test_type,
            category=category, statement=statement, confidence=0.5, status=HypothesisStatus.UNTESTED,
            testable_prediction=str(item.get('testable_prediction') or '') or None,
            prediction=expected, project_id=fact.project_id), if_absent=True)
        same = open_claims.pop((category, prediction.signature(expected)), []) if expected else []
        quoted = by_ref.get(str(item.get('replaces') or '').strip()[:REF_LENGTH])
        for old in ([quoted] if quoted else []) + same:
            if old.hypothesis_id in replaced:
                continue
            replaced.add(old.hypothesis_id)
            why = '更新了这条假设' if old is quoted else '提出了同样的预测'
            try:
                await hypothesis_ledger.update_status(
                    old.hypothesis_id, HypothesisStatus.DEPRECATED,
                    notes=f'运行 {execution.id[:8]} 的报告{why}，以新的为准',
                    expected_status=HypothesisStatus.UNTESTED)
            except HypothesisUpdateConflict:
                pass  # A run or a person settled it meanwhile; that answer stands.


async def _write_analysis(execution: TestExecutionInfo, ai_model):
    """An analysis that fails is recorded on the analysis; the report stays usable."""
    execution.ai_analysis = await analysis_for(execution, ai_model)
    save_analysis_markdown(execution.report_dir, execution.id, execution.ai_analysis)
    await execution_lifecycle.emit('analysis_completed', execution)


async def analysis_for(execution: TestExecutionInfo, ai_model=None) -> ReportAnalysis:
    """The report's analysis of a finished run, with its numbers checked and its token use; saves nothing."""
    with usage.metered() as used:
        analysis = await _analyse(execution, ai_model)
    if used['calls']:
        analysis.usage = used
        logger.info("Analysis of %s used %s", execution.id, used)
    return analysis


async def _analyse(execution: TestExecutionInfo, ai_model) -> ReportAnalysis:
    test_config = (execution.context_snapshot or {}).get('test_config') or {}
    try:
        analyzer = TestAnalyzer(model_type=ai_model)  # None: the factory picks the default model
        evidence_text, verdict, comparison_numbers = await analysis_evidence(execution)
        analysis = await analyzer.analyze(
            summary_data=execution.summary.model_dump(), test_config=test_config,
            test_type=test_config.get("test_type", "unknown"), evidence_text=evidence_text)
        analysis.requirements_verdict = verdict
        analysis = check_numbers(analysis, execution.summary, execution.id, comparison_numbers)
        if analysis.evidence_status == 'mismatch':
            # One rewrite with the wrong numbers named; kept only if it gets more of them right.
            wrong = analysis.fact_check['mismatches']
            rewritten = await analyzer.correct_numbers(wrong)
            if rewritten is not None:
                rewritten.requirements_verdict = verdict
                rewritten = check_numbers(rewritten, execution.summary, execution.id, comparison_numbers)
                if len((rewritten.fact_check or {}).get('mismatches', [])) < len(wrong):
                    rewritten.fact_check['corrected_from'] = len(wrong)
                    analysis = rewritten
        return analysis
    except Exception as error:
        logger.warning("AI analysis failed for %s: %s", execution.id, error)
        return ReportAnalysis(
            analysis_status='failed', overall_assessment=f"AI analysis failed: {error}",
            conclusion="AI 分析未完成")


_reanalyses: dict[str, asyncio.Task] = {}
INTERRUPTED = '后端重启时分析被中断'


async def _reanalyze(execution: TestExecutionInfo, ai_model):
    try:
        await _write_analysis(execution, ai_model)
        execution.report_status = 'completed'
        from app.services.ai.memory import fact_ledger
        fact = await fact_ledger.load(execution.id)
        if fact:
            await record_hypotheses(execution, fact)
    except asyncio.CancelledError:
        execution.report_status, execution.report_error = 'failed', INTERRUPTED
        raise
    except Exception as error:
        logger.warning("Re-analysis failed for %s: %s", execution.id, error)
        execution.report_status, execution.report_error = 'failed', str(error)
    finally:
        _reanalyses.pop(execution.id, None)
        # Read the version again: follow-up questions are not allowed meanwhile, but a delete is.
        current = store.get_execution(execution.id)
        if current is not None:
            execution.storage_version = current.storage_version
            from app.services.execution_lifecycle import persist_execution
            try:
                await persist_execution(execution)
            except Exception as error:
                logger.warning("Re-analysis result not saved for %s: %s", execution.id, error)


async def start_reanalysis(execution_id: str, ai_model=None) -> TestExecutionInfo:
    """Write the run's AI analysis again in the background; the report page polls report_status."""
    from app.services.execution_lifecycle import persist_execution
    current = store.get_execution(execution_id)
    if current is None:
        raise LookupError(execution_id)
    if current.status != TestExecutionStatus.COMPLETED or not current.summary:
        raise ValueError('只有成功完成并有结果的运行才能分析')
    if current.report_status == 'running' or execution_id in _reanalyses:
        raise RuntimeError('这次运行的分析还在进行')
    execution = current.model_copy(deep=True)
    execution.report_status, execution.report_error = 'running', None
    await persist_execution(execution)
    _reanalyses[execution_id] = asyncio.create_task(_reanalyze(execution, ai_model))
    return execution


async def stop_reanalyses():
    tasks = list(_reanalyses.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def recover_interrupted():
    """A restart ends any analysis in flight; without this the report would say it is still running."""
    from app.services.execution_lifecycle import TERMINAL, persist_execution
    for execution in list(store.executions.values()):
        if execution.report_status == 'running' and execution.status in TERMINAL:
            execution = execution.model_copy(deep=True)
            execution.report_status, execution.report_error = 'failed', INTERRUPTED
            try:
                await persist_execution(execution)
            except Exception as error:
                logger.warning("Could not mark %s as interrupted: %s", execution.id, error)


async def finish_run(execution: TestExecutionInfo, ai_model=None):
    """AUTO_ANALYSIS_ENABLED saves the model calls; the measured history is kept either way."""
    if execution.status != TestExecutionStatus.COMPLETED or not execution.summary:
        return
    if settings.AUTO_ANALYSIS_ENABLED:
        await _write_analysis(execution, ai_model)
    try:
        await record_facts(execution)
    except Exception as error:
        logger.warning("Fact recording skipped for %s: %s", execution.id, error)


async def backfill_memory():
    """Runs recorded before facts carried a project and load get them, so they join project history."""
    from app.services.ai.analysis_context import run_context
    from app.services.ai.memory import fact_ledger, hypothesis_ledger
    updated = 0
    for execution in list(store.executions.values()):
        if execution.status != TestExecutionStatus.COMPLETED or not execution.context_snapshot:
            continue
        try:
            context = await asyncio.to_thread(run_context, execution)
            if not context['project_id']:
                continue
            if await fact_ledger.annotate(execution.id, only_missing=True, **context):
                updated += 1
            await hypothesis_ledger.assign_project(execution.id, context['project_id'])
        except Exception as error:
            logger.warning('Memory backfill skipped %s: %s', execution.id, error)
    if updated:
        logger.info('Added project and load to %d earlier runs', updated)
