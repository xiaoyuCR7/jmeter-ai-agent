"""Requirements written the way documents state them: every request, sustained load, and what the plan allows."""
from unittest.mock import AsyncMock

import pytest

from app.services import requirements as req
from tests.test_requirements import item, summary

CAPPED_PLAN = '''<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan><hashTree>
  <TestPlan testclass="TestPlan" testname="P" enabled="true"/>
  <hashTree>
    <ConstantThroughputTimer testclass="ConstantThroughputTimer" testname="Target RPS" enabled="true">
      <intProp name="calcMode">{mode}</intProp>
      <doubleProp><name>throughput</name><value>{per_minute}</value><savedValue>0.0</savedValue></doubleProp>
    </ConstantThroughputTimer>
    <hashTree/>
    <ThreadGroup testclass="ThreadGroup" testname="Users" enabled="true">
      <stringProp name="ThreadGroup.num_threads">30</stringProp>
    </ThreadGroup>
    <hashTree/>
  </hashTree>
</hashTree></jmeterTestPlan>'''


def steady(steady_tps, **overrides):
    return summary(diagnostics={'load': {'peak_threads': 30, 'duration_seconds': 60,
                                         'steady_window': {'start_s': 9.4, 'end_s': 59.1, 'seconds': 49.7}},
                                'steady': {'throughput': steady_tps}}, **overrides)


def test_every_request_must_meet_an_each_requirement_on_its_own():
    """"所有接口 P95 < 500ms": the overall P95 (650 here, or lower with more fast requests) is not the test."""
    result = req.evaluate(req.PerformanceRequirements(items=[item(scope='each', metric='p95', operator='lt', value=500)]),
                          summary(p95_response_time=300))
    verdict = result.items[0]
    assert verdict.status == 'fail' and verdict.actual == 900
    assert verdict.matched_labels == ['POST /api/order/create']  # The ones that miss it.
    assert verdict.description.startswith('[每个接口]')
    assert '不达标的接口：POST /api/order/create' in req.format_for_prompt(result)
    assert req.evaluate(req.PerformanceRequirements(items=[item(scope='each', metric='p95', operator='lt', value=1000)]),
                        summary()).items[0].status == 'pass'


def test_throughput_is_judged_at_the_planned_load_not_over_the_ramp_up():
    result = req.evaluate(req.PerformanceRequirements(items=[item(metric='throughput', operator='gte', value=50)]),
                          steady(52.4, throughput=47.3, success={'throughput': 47.3}))
    assert (result.items[0].actual, result.items[0].status) == (52.4, 'pass')
    # Without a steady window, the whole run is all there is.
    assert req.evaluate(req.PerformanceRequirements(items=[item(metric='throughput', operator='gte', value=50)]),
                        summary(throughput=47.3)).items[0].actual == 47.3


@pytest.mark.parametrize('mode, per_minute, cap', [(3, 3000, 50.0), (0, 100, 50.0), (1, 6000, 100.0)])
def test_the_plans_throughput_timer_gives_its_total_limit(tmp_path, mode, per_minute, cap):
    path = tmp_path / 'plan.jmx'
    path.write_text(CAPPED_PLAN.format(mode=mode, per_minute=per_minute), encoding='utf-8')
    assert req.throughput_cap(str(path)) == cap
    assert req.throughput_cap(str(tmp_path / 'missing.jmx')) is None


def test_a_throughput_shortfall_the_plan_forces_is_not_judged_as_the_systems(tmp_path):
    """The plan paced 30 users to 50 rps in total; "≥ 50 TPS" could never pass, whatever the server did."""
    path = tmp_path / 'plan.jmx'
    path.write_text(CAPPED_PLAN.format(mode=3, per_minute=3000), encoding='utf-8')
    requirements = req.PerformanceRequirements(items=[item(id='R4', metric='throughput', operator='gte', value=50)])
    result = req.evaluate_run(requirements, steady(49.06), str(path))
    verdict = result.items[0]
    assert verdict.status == 'missing' and verdict.note == 'plan_caps_throughput:50'
    assert result.verdict == 'incomplete'
    assert '计划限速' in req.format_for_prompt(result)
    # With room above the requirement, a shortfall is the system's.
    path.write_text(CAPPED_PLAN.format(mode=3, per_minute=3600), encoding='utf-8')
    assert req.evaluate_run(requirements, steady(49.06), str(path)).items[0].status == 'fail'


@pytest.mark.asyncio
async def test_extraction_names_the_requests_the_plan_has():
    adapter = AsyncMock()
    adapter.chat.return_value = ('{"items": [{"id": "R1", "scope": "each", "metric": "p95", "operator": "lt", "value": 500,'
                                 ' "source": "所有接口 P95 < 500ms"}, {"id": "R2", "scope": "transaction",'
                                 ' "transaction": "POST /api/orders", "metric": "p95", "operator": "lt", "value": 800,'
                                 ' "source": "下单接口 P95 < 800ms"}]}')
    result = await req.extract_requirements('所有接口 P95 < 500ms；下单接口 P95 < 800ms', adapter,
                                            endpoints=[{'route': 'POST /api/orders', 'name': 'Place an order'}])
    sent = adapter.chat.await_args.args[0][1].content
    assert '- POST /api/orders（Place an order）' in sent
    assert [(i.scope, i.transaction) for i in result.items] == [('each', None), ('transaction', 'POST /api/orders')]
