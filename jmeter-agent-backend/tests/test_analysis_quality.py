"""What the analysis is told and how far its conclusions can be trusted."""
import json
from io import StringIO
from types import SimpleNamespace

import pytest

from app.models.schemas import DetailedTestSummary, FactSheet, ReportAnalysis, TimelineSegment
from app.services import baseline_compare, requirements as rq, run_diagnostics
from app.services.ai.analyzer import check_numbers
from app.services.ai.memory.validators import FactValidator
from app.services.ai.prompts.test_analyzer import build_analysis_prompt
from app.services.evidence import ExecutionEvidence, comparison_values, format_for_prompt
from app.services.jmeter.regression_engine import RegressionEngine
from app.services.jmeter.report_parser import ReportParser
from app.services.ml_analysis import MLAnomalyDetector
from app.services.server_metrics import MIN_CORRELATION_POINTS, analyze as analyze_server

HEADER = ('timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,success,failureMessage,'
          'bytes,sentBytes,grpThreads,allThreads,URL,Latency,IdleTime,Connect\n')


def row(ts, elapsed, label='A', code='200', message='OK', ok='true', failure='', threads=10, connect=0, latency=None):
    return (f'{ts},{elapsed},{label},{code},{message},t,text,{ok},{failure},100,10,{threads},{threads},http://x,'
            f'{elapsed if latency is None else latency},0,{connect}\n')


def parse(rows):
    return ReportParser()._parse_csv_jtl(StringIO(HEADER + ''.join(rows)))


def requirements(*items, **load):
    return rq.PerformanceRequirements(items=list(items), load_profile=rq.LoadProfile(**load))


P95 = rq.RequirementItem(id='R1', metric='p95', operator='lte', value=500)
TPS = rq.RequirementItem(id='R2', metric='throughput', operator='gte', value=100)


def test_fast_failures_do_not_satisfy_latency_or_throughput_requirements():
    summary = parse([row(1000 + i * 2, 5, code='503', message='Unavailable', ok='false') for i in range(2000)])
    assert rq.evaluate(requirements(P95), summary).verdict == 'incomplete'
    evaluation = rq.evaluate(requirements(P95, TPS), summary)
    assert [item.status for item in evaluation.items] == ['missing', 'fail'] and evaluation.verdict == 'fail'
    assert summary.throughput > 100 and summary.success['throughput'] == 0


def test_latency_requirements_ignore_the_failed_samples():
    rows = [row(1000 + i, 900) for i in range(100)] + [row(2000 + i, 1, ok='false') for i in range(900)]
    summary = parse(rows)
    assert summary.percentiles['p95'] == 900 and summary.percentiles['p50'] == 1
    evaluation = rq.evaluate(requirements(rq.RequirementItem(id='R1', metric='avg_response_time', operator='lte', value=500)), summary)
    assert evaluation.verdict == 'fail' and evaluation.items[0].actual == 900


def test_reports_parsed_before_success_stats_keep_their_verdict():
    summary = DetailedTestSummary(total_requests=10, avg_response_time=100, min_response_time=1, max_response_time=200,
                                  p95_response_time=300, error_rate=0, throughput=200, received_bytes_per_sec=0,
                                  sent_bytes_per_sec=0, per_label={'A': {'p95_response_time': 300}})
    assert rq.evaluate(requirements(P95, TPS), summary).verdict == 'pass'


def test_a_pass_below_the_required_load_is_incomplete():
    summary = parse([row(1000 + i * 100, 50, threads=5) for i in range(100)])
    evaluation = rq.evaluate(requirements(rq.RequirementItem(id='R1', metric='p95', operator='lte', value=500),
                                          concurrency=100, duration_seconds=5), summary)
    assert evaluation.verdict == 'incomplete' and evaluation.passed == 1
    assert [(gap.field, gap.expected, gap.actual) for gap in evaluation.load_gaps] == [('concurrency', 100, 5)]
    assert '负载未达到需求' in rq.format_for_prompt(evaluation)


def test_assertion_failures_show_their_reason_and_transaction():
    summary = parse([row(1000 + i, 50, label='Login', ok='false', failure='Test failed: expected orderId') for i in range(4)]
                    + [row(2000, 50, label='Home', ok='false', failure='Test failed: expected orderId')])
    error = summary.error_details[0]
    assert error.error_type == '200: Test failed: expected orderId'
    assert error.sample_messages[0] == 'Test failed: expected orderId'
    assert error.labels == {'Login': 4, 'Home': 1}


def test_xml_results_read_assertions_and_timing_and_count_parents_once():
    content = ('<testResults><httpSample t="50" lt="40" ct="9" ts="1000" s="false" lb="Buy" rc="200" rm="OK" na="3">'
               '<assertionResult><failure>true</failure><failureMessage>no order id</failureMessage></assertionResult>'
               '<httpSample t="20" ts="1000" s="true" lb="Buy-0" rc="200" rm="OK"/>'
               '</httpSample></testResults>')
    summary = ReportParser()._parse_xml_jtl(StringIO(content))
    assert summary.total_requests == 1 and summary.error_details[0].error_type == '200: no order id'
    assert summary.diagnostics['timing']['avg_connect_time'] == 9
    assert summary.diagnostics['load']['peak_threads'] == 3


TRANSACTION = 'Number of samples in transaction : 2, number of failing samples : 0'


def test_transaction_parents_are_reported_but_not_counted_twice():
    summary = parse([row(1000, 40, label='GET a'), row(1040, 60, label='GET b'),
                     row(1000, 100, label='Checkout', message=TRANSACTION)])
    assert summary.total_requests == 2
    assert summary.per_label['Checkout']['is_transaction'] and summary.per_label['Checkout']['count'] == 1


def test_a_plan_that_only_records_transactions_still_has_a_report():
    summary = parse([row(1000 + i, 100, label='Checkout', message=TRANSACTION) for i in range(3)])
    assert summary.total_requests == 3 and summary.per_label['Checkout']['count'] == 3


def run_with_ramp():
    rows = [row(1000 + i * 100, 2000, threads=i + 1, connect=1500) for i in range(9)]  # Ramp-up, one connect each.
    rows += [row(2000 + i * 100, 100) for i in range(500)]
    rows += [row(30000, 2600, label='B'), row(30000, 2500, label='B')]
    rows += [row(52000 + i, 90, threads=1) for i in range(3)]
    return parse(rows)


def test_steady_window_leaves_out_ramp_up_and_tail():
    summary = run_with_ramp()
    load = summary.diagnostics['load']
    assert load['peak_threads'] == 10 and load['steady_window']['start_s'] > 0.8
    assert summary.diagnostics['steady']['max_response_time'] == 2600
    assert summary.diagnostics['steady']['labels']['A']['p99_response_time'] == 100
    assert summary.per_label['A']['p99_response_time'] == 2000


def test_slowest_samples_keep_their_moment_and_connect_time():
    slowest = run_with_ramp().diagnostics['slowest']
    assert slowest[0]['elapsed'] == 2600 and slowest[0]['offset_s'] == 29.0 and slowest[0]['connect'] == 0
    assert any(sample['connect'] == 1500 for sample in slowest)


def test_a_step_test_has_no_steady_window():
    rows = [row(1000 + i * 100, 50, threads=1 + i // 100) for i in range(1000)]
    summary = parse(rows)
    assert summary.diagnostics['load']['steady_window'] is None and summary.diagnostics['steady'] is None


def test_timeline_resolution_follows_the_run_length():
    def widths(seconds):
        segments = parse([row(1000 + i * 1000, 50) for i in range(seconds)]).timeline_segments
        return segments[0].time_range
    assert widths(60) == '0-5s' and widths(600) == '0-10s' and widths(3600) == '0-60s'


def test_the_analysis_is_told_about_each_transaction_and_its_errors():
    summary = run_with_ramp()
    text = run_diagnostics.format_for_prompt(run_diagnostics.build(summary))
    run = json.loads(text.split('\n')[1])
    assert [item['label'] for item in run['labels']] == ['B', 'A']
    assert run['labels'][1]['steady_p99'] == 100 and run['slowest_samples'][0]['offset_s'] == 29.0


def test_every_analysis_path_reads_the_same_block():
    summary = run_with_ramp()
    evidence = ExecutionEvidence(execution_id='x', project_id='p', requirements=rq.evaluate(None, summary),
                                 run=run_diagnostics.build(summary), configured_load={'concurrent_users': 10})
    text = format_for_prompt(evidence)
    assert '运行诊断' in text and '配置负载' in text and 'slowest_samples' in text


def test_report_prompt_asks_for_each_finding_once():
    summary = run_with_ramp()
    prompt = build_analysis_prompt(summary.model_dump(), {}, 'baseline')
    assert 'improvement_plan' not in prompt and 'specific_issues' not in prompt and 'active_threads' in prompt


def fact():
    return FactSheet(execution_id='x', total_requests=1000, avg_response_time=300, max_response_time=2000, error_rate=1.0,
                     throughput=100, percentiles={'p50': 200, 'p90': 400, 'p95': 500, 'p99': 900},
                     per_label={'Login': {'count': 500, 'p99_response_time': 3000, 'avg_response_time': 700}})


@pytest.mark.parametrize('text', [
    'P95、P99 分别为 500ms 和 900ms', '登录接口 P99 为 3000ms，整体 P99 为 900ms', '错误率较上次上升 50%',
    'P99 比基线恶化了 30%', '吞吐量下降 20%', '服务端 CPU 峰值 92%，TPS 为 100', 'P99 约为 P50 的 3 倍', 'P95 ≤ 800 ms',
    'P99 是平均值的 3 倍', '平均响应时间 300 ms',
])
def test_honest_sentences_are_not_flagged(text):
    assert FactValidator().validate(text, fact()).mismatches == []


@pytest.mark.parametrize('text', ['P99 为 90000ms', '吞吐量 400 TPS', '错误率为 30%', '总请求 5000', '平均响应时间约 9 秒'])
def test_invented_numbers_are_flagged(text):
    assert FactValidator().validate(text, fact()).mismatches


def test_numbers_quoted_from_another_run_are_allowed_only_when_they_exist():
    assert FactValidator().validate('基线 P99 为 600ms', fact(), [600.0]).mismatches == []
    assert FactValidator().validate('基线 P99 为 600ms', fact(), [100.0]).mismatches


def test_validation_no_longer_claims_to_have_corrected_anything():
    assert '已修正' not in FactValidator().validate('P99 为 90000ms', fact()).corrected_summary


def analysis(**fields):
    return ReportAnalysis(**{'overall_assessment': 'x', 'conclusion': 'y', **fields})


def test_report_analysis_records_what_its_numbers_were_checked_against():
    summary = run_with_ramp()
    p95 = summary.percentiles['p95']
    good = check_numbers(analysis(key_findings=[f'P95 为 {p95}ms']), summary, 'x')
    assert good.evidence_status == 'verified' and good.fact_check['checked'] == 1
    bad = check_numbers(analysis(key_findings=['P95 为 98765ms']), summary, 'x')
    assert bad.evidence_status == 'mismatch' and bad.fact_check['mismatches'][0]['claimed'] == 98765
    assert check_numbers(analysis(), summary, 'x').evidence_status == 'unverified'
    failed = analysis(analysis_status='failed', key_findings=['P95 为 98765ms'])
    assert check_numbers(failed, summary, 'x').evidence_status == 'unverified'


def test_comparison_values_cover_runs_and_requirement_limits():
    summary = run_with_ramp()
    evidence = ExecutionEvidence(execution_id='x', project_id='p', requirements=rq.evaluate(requirements(P95), summary),
                                 baseline={'baseline': {'deltas': [{'baseline': 600, 'target': 900}], 'transactions': []}})
    assert set(comparison_values(evidence)) == {500.0, 600.0, 900.0}


def summary_of(**fields):
    values = dict(total_requests=1000, avg_response_time=100, min_response_time=1, max_response_time=2000,
                  p95_response_time=300, p99_response_time=400, error_rate=0.1, throughput=100,
                  received_bytes_per_sec=0, sent_bytes_per_sec=0)
    return DetailedTestSummary(**{**values, **fields})


def test_min_and_max_are_context_not_regressions():
    report = RegressionEngine(20).compare(summary_of(), summary_of(min_response_time=2, max_response_time=9000))
    assert report.overall_verdict == 'stable' and report.regressions == []
    assert {'min_response_time', 'max_response_time'} <= {d.metric for d in report.deltas}


def test_error_rate_needs_a_real_rise_to_be_a_regression():
    assert RegressionEngine(20).compare(summary_of(), summary_of(error_rate=0.2)).overall_verdict == 'stable'
    assert RegressionEngine(20).compare(summary_of(error_rate=0), summary_of(error_rate=0.5)).overall_verdict == 'stable'
    assert RegressionEngine(20).compare(summary_of(), summary_of(error_rate=2)).overall_verdict == 'regression'


def test_runs_at_different_load_are_not_judged():
    one = summary_of(diagnostics={'load': {'peak_threads': 100}})
    other = summary_of(p95_response_time=900, diagnostics={'load': {'peak_threads': 10}})
    result = baseline_compare._comparison('base', one, other)
    assert result['verdict'] == 'unknown' and result['notes'] == [{'kind': 'load', 'baseline': 100, 'target': 10}]
    assert '负载不同' in baseline_compare.format_for_prompt({'previous': result, 'baseline': None})


def test_small_samples_are_called_out_and_small_transactions_skipped():
    small = summary_of(total_requests=20, per_label={'A': {'count': 10, 'p95_response_time': 100}})
    worse = summary_of(total_requests=20, per_label={'A': {'count': 10, 'p95_response_time': 900}})
    result = baseline_compare._comparison('base', small, worse)
    assert result['notes'][0]['kind'] == 'samples' and result['transactions'] == []


def segments(values, requests=100):
    return [TimelineSegment(time_range=f'{i * 30}-{i * 30 + 30}s', avg_response_time=value, max_response_time=value,
                            throughput=10, error_rate=0, request_count=requests) for i, value in enumerate(values)]


@pytest.mark.parametrize('values,expected', [
    ((100, 150, 200, 250, 300, 350), 'degrading'), ((350, 300, 250, 200, 150, 100), 'improving'),
    ((100, 300, 100, 300, 100, 300), 'fluctuating'), ((100, 101, 99, 100, 102, 100), 'stable'),
])
def test_trend_direction(values, expected):
    assert ReportParser()._analyze_trend(segments(values)).trend_direction == expected


def test_a_few_samples_do_not_set_the_trend():
    timeline = segments([500], requests=5) + segments([100] * 9)
    assert ReportParser()._analyze_trend(timeline).trend_direction == 'stable'


def timeline(rt=None, tps=None):
    size = len(rt or tps)
    return [{'time_range': f'{i * 5}-{i * 5 + 5}s', 'avg_response_time': (rt or [100] * size)[i], 'error_rate': 0,
             'throughput': (tps or [10] * size)[i]} for i in range(size)]


@pytest.mark.parametrize('values', [[100, 100, 100, 100, 5000], [100] * 8 + [5000, 5000]])
def test_spikes_are_found_in_short_series_and_when_they_repeat(values):
    anomalies = MLAnomalyDetector().analyze_timeline(timeline(rt=values)).anomalies
    assert [a.value for a in anomalies] == [v for v in values if v == 5000]
    assert anomalies[0].description.startswith('response_time spike at ')


def test_a_fast_segment_is_not_an_anomaly():
    assert MLAnomalyDetector().analyze_timeline(timeline(rt=[100] * 7 + [5])).anomalies == []


def test_ramp_up_and_tail_are_not_throughput_drops():
    report = MLAnomalyDetector().analyze_timeline(timeline(tps=[2, 10, 10, 10, 10, 10, 3]))
    assert report.anomalies == [] and report.health_score == 100


def test_a_drop_in_the_middle_of_the_run_is_found():
    report = MLAnomalyDetector().analyze_timeline(timeline(tps=[10, 10, 2, 10, 10]))
    assert [a.metric for a in report.anomalies] == ['throughput']


def server_raw(points):
    return {'test_start': 0, 'test_end': 5 * points, 'metrics': [{'name': 'CPU', 'unit': '%', 'series': [
        {'labels': 'all', 'points': [[i * 5 + 1, float(i)] for i in range(points)]}]}]}


@pytest.mark.parametrize('points,strong', [(MIN_CORRELATION_POINTS - 1, False), (MIN_CORRELATION_POINTS, True)])
def test_correlation_needs_enough_points(points, strong):
    summary = summary_of(timeline_segments=[
        TimelineSegment(time_range=f'{i * 5}-{i * 5 + 5}s', avg_response_time=100 + i, max_response_time=1, throughput=1,
                        error_rate=0, request_count=1) for i in range(points)])
    result = analyze_server(server_raw(points), summary)
    assert result['metrics'][0]['corr_strong'] is strong and bool(result['findings']) is strong


@pytest.mark.asyncio
async def test_knowledge_is_included_only_when_people_archived_some():
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    agent = AnalysisAgent.__new__(AnalysisAgent)
    fact_sheet = fact().model_copy(update={'raw_summary': summary_of()})
    calls = []

    def retrieve(report, limit, module):
        calls.append(module)
        return {'similar_defects': [], 'known_issues': [1], 'module_standards': [], 'formatted_context': '已知问题 A'}
    agent.retriever = SimpleNamespace(retrieve=retrieve)
    text = await agent._knowledge_context(fact_sheet, 'Checkout')
    assert '已知问题 A' in text and '不是本次测试的事实' in text and calls == ['Checkout']

    agent.retriever = SimpleNamespace(retrieve=lambda *args: (_ for _ in ()).throw(OSError('store down')))
    assert await agent._knowledge_context(fact_sheet, 'Checkout') == ''


@pytest.mark.asyncio
async def test_a_finished_run_joins_the_measured_history(monkeypatch, tmp_path):
    from app.api import routes
    from app.services.ai import memory
    from app.services.ai.memory import provenance
    jtl = tmp_path / 'result.jtl'
    jtl.write_text(HEADER + row(1000, 50), encoding='utf-8')
    saved, evaluated = [], []

    async def save(item):
        saved.append(item)
        return True

    async def evaluate(module, execution_id):
        evaluated.append((module, execution_id))

    async def scope(execution_id):
        return 'scope-1'
    monkeypatch.setattr(memory.fact_ledger, 'save', save)
    monkeypatch.setattr(memory.hypothesis_ledger, 'evaluate_module_hypotheses', evaluate)
    monkeypatch.setattr(provenance, 'load_execution_scope', scope)
    execution = SimpleNamespace(id='run-1', report_dir=str(tmp_path), test_plan_id='plan-1', summary=summary_of(),
                                context_snapshot={'test_config': {'test_type': 'baseline'}})
    await routes._record_facts(execution)
    assert saved[0].module == 'plan-1' and saved[0].context_scope == 'scope-1' and saved[0].test_type == 'baseline'
    assert evaluated == [('plan-1', 'run-1')]


def test_reports_parsed_before_diagnostics_still_build_their_evidence():
    summary = summary_of(per_label={'A': {'count': 10, 'error_rate': 0, 'p95_response_time': 300}})
    run = run_diagnostics.build(summary)
    assert run['labels'][0]['label'] == 'A' and run['load'] is None and run['slowest_samples'] is None
    assert '运行诊断' in run_diagnostics.format_for_prompt(run)


@pytest.mark.asyncio
async def test_an_empty_model_reply_is_retried_with_room_to_answer(monkeypatch):
    from unittest.mock import AsyncMock
    from app.services.ai import analyzer as module
    good = json.dumps({'overall_assessment': 'ok', 'conclusion': 'done'})
    chat = AsyncMock(side_effect=['', good])
    adapter = SimpleNamespace(chat=chat, prepare_chat_messages=lambda messages, response_tokens: messages)
    monkeypatch.setattr(module.AIAdapterFactory, 'create', lambda model_type=None: adapter)
    result = await module.TestAnalyzer().analyze({}, {}, 'baseline')
    assert result.analysis_status == 'completed' and chat.await_count == 2
    assert chat.await_args.kwargs['max_tokens'] == module.AI_ANALYSIS_MAX_TOKENS
