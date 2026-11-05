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


MARKDOWN = {
    'zh-CN': {
        'title': 'AI 性能测试分析报告', 'id': '执行ID', 'time': '生成时间', 'status': '分析状态', 'none': '无',
        'overall_assessment': '整体评估', 'key_findings': '关键发现', 'limitations': '分析限制',
        'specific_issues': '具体问题定位', 'timeline_analysis': '性能时间线分析', 'resource_bottlenecks': '资源瓶颈推断',
        'performance_bottlenecks': '性能瓶颈点', 'error_analysis': '错误根因分析', 'recommendations': '优化建议',
        'improvement_plan': '分步改进计划', 'risk_areas': '风险区域', 'conclusion': '最终结论', 'follow_ups': '追问',
        'corrected': '初稿有 {n} 处数字不符，已让模型改写。',
        'mismatch': '有数字与实测不符，请以原始指标为准：{wrong}。', 'mismatch_item': '{metric} 写的是 {claimed}，实测 {actual}',
        'joiner': '；',
        'verified': '已核对文中 {n} 处指标数字，均与实测相符；解释性内容仍是推测，不等于已验证根因。',
        'unverified': 'AI文本尚未逐项事实核验，请结合原始指标；推测不等于已验证根因。'},
    'en': {
        'title': 'AI Performance Test Analysis', 'id': 'Execution ID', 'time': 'Generated', 'status': 'Analysis status',
        'none': 'None', 'overall_assessment': 'Overall assessment', 'key_findings': 'Key findings',
        'limitations': 'Limitations', 'specific_issues': 'Specific issues', 'timeline_analysis': 'Timeline',
        'resource_bottlenecks': 'Resource bottlenecks (inferred)', 'performance_bottlenecks': 'Performance bottlenecks',
        'error_analysis': 'Errors', 'recommendations': 'Recommendations', 'improvement_plan': 'Step-by-step plan',
        'risk_areas': 'Risks', 'conclusion': 'Conclusion', 'follow_ups': 'Follow-up questions',
        'corrected': 'The first draft had {n} numbers that did not match; the model rewrote it. ',
        'mismatch': 'Some numbers do not match the measurements; trust the raw metrics: {wrong}.',
        'mismatch_item': '{metric} written as {claimed}, measured {actual}', 'joiner': '; ',
        'verified': ('All {n} metric numbers in the text match the measurements; explanations are still '
                     'hypotheses, not verified causes.'),
        'unverified': ('The text has not been fact-checked number by number; read it with the raw metrics. '
                       'Hypotheses are not verified causes.')},
}
MARKDOWN_SECTIONS = ('key_findings', 'limitations', 'specific_issues', 'timeline_analysis', 'resource_bottlenecks',
                     'performance_bottlenecks', 'error_analysis', 'recommendations', 'improvement_plan', 'risk_areas')
ALWAYS_SHOWN = {'key_findings', 'performance_bottlenecks', 'error_analysis', 'recommendations'}


def _words(analysis: dict) -> dict:
    return MARKDOWN['en' if analysis.get('language') == 'en' else 'zh-CN']


def _fact_check_note(analysis: dict) -> str:
    check, words = analysis.get('fact_check') or {}, _words(analysis)
    fixed = words['corrected'].format(n=check['corrected_from']) if check.get('corrected_from') else ''
    return fixed + _fact_check_status(analysis, check)


def _fact_check_status(analysis: dict, check: dict) -> str:
    words = _words(analysis)
    if analysis.get('evidence_status') == 'mismatch':
        wrong = words['joiner'].join(words['mismatch_item'].format(**m) for m in check.get('mismatches', []))
        return words['mismatch'].format(wrong=wrong)
    if analysis.get('evidence_status') == 'verified':
        return words['verified'].format(n=check.get('checked', 0))
    return words['unverified']


def save_analysis_markdown(report_dir: str, execution_id: str, analysis) -> str:
    """Save AI analysis as a readable Markdown file in the report directory, in the analysis' language.

    Returns the file path, or empty string on failure.
    """
    if not report_dir or not os.path.isdir(report_dir):
        return ""
    try:
        ad = analysis.model_dump() if hasattr(analysis, 'model_dump') else analysis
        w = _words(ad)
        separator = ' ' if ad.get('language') == 'en' else '。'
        md = (f"# {w['title']}\n\n**{w['id']}**: {execution_id}\n"
              f"**{w['time']}**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
              f"**{w['status']}**: {ad.get('analysis_status', 'completed')}{separator}{_fact_check_note(ad)}\n\n---\n\n"
              f"## {w['overall_assessment']}\n\n{ad.get('overall_assessment') or w['none']}\n")
        for key in MARKDOWN_SECTIONS:
            value = ad.get(key)
            if not value and key not in ALWAYS_SHOWN:
                continue
            md += f"\n## {w[key]}\n\n"
            if isinstance(value, str):
                md += value + "\n"
            elif key == 'improvement_plan':
                md += ''.join(f"{i}. {item}\n" for i, item in enumerate(value, 1))
            else:
                md += ''.join(f"- {item}\n" for item in value or [])
        md += f"\n## {w['conclusion']}\n\n{ad.get('conclusion') or w['none']}\n"
        if ad.get('follow_ups'):
            md += f"\n## {w['follow_ups']}\n"
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
    # A re-analysis rewrites this run's report: what its earlier report proposed and no run has settled
    # gives way to the new one, or the ledger keeps two reports' claims about one run.
    earlier_own = {hyp.hypothesis_id: hyp for hyp in await hypothesis_ledger.list_for_project(
        fact.project_id, status=HypothesisStatus.UNTESTED, limit=200) if hyp.source_execution_id == execution.id}
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
        earlier_own.pop(hypothesis_id, None)  # Said again in the same words: it stays.
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
    for old in earlier_own.values():
        try:
            await hypothesis_ledger.update_status(
                old.hypothesis_id, HypothesisStatus.DEPRECATED,
                notes='这次运行的报告重新生成了，以新报告为准', expected_status=HypothesisStatus.UNTESTED)
        except HypothesisUpdateConflict:
            pass


async def _write_analysis(execution: TestExecutionInfo, ai_model, lang=None):
    """An analysis that fails is recorded on the analysis; the report stays usable."""
    execution.ai_analysis = await analysis_for(execution, ai_model, lang)
    save_analysis_markdown(execution.report_dir, execution.id, execution.ai_analysis)
    await execution_lifecycle.emit('analysis_completed', execution)


async def analysis_for(execution: TestExecutionInfo, ai_model=None, lang=None) -> ReportAnalysis:
    """The report's analysis of a finished run, with its numbers checked and its token use; saves nothing."""
    from app.core.lang_utils import report_lang
    with usage.metered() as used:
        analysis = await _analyse(execution, ai_model, report_lang(lang))
    if used['calls']:
        analysis.usage = used
        logger.info("Analysis of %s used %s", execution.id, used)
    return analysis


async def _analyse(execution: TestExecutionInfo, ai_model, lang: str) -> ReportAnalysis:
    # Runs that finish together queue here instead of all calling the model at once.
    from app.services.ai.agent.base import agent_slot
    async with agent_slot(wait_seconds=None, background=True):
        return await _analyse_now(execution, ai_model, lang)


async def _analyse_now(execution: TestExecutionInfo, ai_model, lang: str) -> ReportAnalysis:
    test_config = (execution.context_snapshot or {}).get('test_config') or {}
    try:
        analyzer = TestAnalyzer(model_type=ai_model)  # None: the factory picks the default model
        evidence_text, verdict, comparison_numbers = await analysis_evidence(execution)
        analysis = await analyzer.analyze(
            summary_data=execution.summary.model_dump(), test_config=test_config,
            test_type=test_config.get("test_type", "unknown"), evidence_text=evidence_text, lang=lang)
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
            conclusion="AI analysis did not finish" if lang == 'en' else "AI 分析未完成", language=lang)


_reanalyses: dict[str, asyncio.Task] = {}
INTERRUPTED = '后端重启时分析被中断'


async def _reanalyze(execution: TestExecutionInfo, ai_model, lang=None):
    try:
        await _write_analysis(execution, ai_model, lang)
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


async def start_reanalysis(execution_id: str, ai_model=None, lang=None) -> TestExecutionInfo:
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
    _reanalyses[execution_id] = asyncio.create_task(_reanalyze(execution, ai_model, lang))
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


def is_capacity_step(execution) -> bool:
    purpose = (getattr(execution, 'context_snapshot', None) or {}).get('purpose') or {}
    return purpose.get('kind') == 'capacity'


async def finish_run(execution: TestExecutionInfo, ai_model=None):
    """AUTO_ANALYSIS_ENABLED saves the model calls; the measured history is kept either way.

    A capacity step gets no report of its own: the search writes one when it ends, on the step that decided it.
    """
    if execution.status != TestExecutionStatus.COMPLETED or not execution.summary:
        return
    if settings.AUTO_ANALYSIS_ENABLED and not is_capacity_step(execution):
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
