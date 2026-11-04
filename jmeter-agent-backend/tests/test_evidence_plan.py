"""What the analysis is told about the plan that ran, besides its results."""
from app.services import evidence
from app.services import requirements as req
from tests.test_requirements import summary

PLAN = '''<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan><hashTree>
  <TestPlan testclass="TestPlan" testname="P" enabled="true"/>
  <hashTree>
    <HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="POST /api/login" enabled="true"/>
    <hashTree/>
    <HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="GET /api/order/list" enabled="true"/>
    <hashTree/>
    <HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="POST /api/pay" enabled="true"/>
    <hashTree/>
    <HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="DELETE /api/old" enabled="false"/>
    <hashTree/>
  </hashTree>
</hashTree></jmeterTestPlan>'''


def test_requests_of_the_plan_that_never_ran_are_named():
    """Every user stopped after its login failed; the report called login "the only endpoint tested"."""
    assert evidence._unrun_requests(PLAN, summary()) == ['POST /api/pay']
    text = evidence.format_for_prompt(evidence.ExecutionEvidence(
        execution_id='e', project_id='p', requirements=req.RequirementEvaluation(verdict='none'),
        unrun_requests=['POST /api/pay'], throughput_cap=50.0))
    assert 'POST /api/pay' in text and '唯一被测' in text
    assert '约 50 请求/秒' in text and '计划在限速' in text


def test_the_plans_own_rate_limit_may_be_quoted():
    found = evidence.ExecutionEvidence(execution_id='e', project_id='p',
                                       requirements=req.RequirementEvaluation(verdict='none'), throughput_cap=50.0)
    assert 50.0 in evidence.comparison_values(found)


def test_the_evidence_prompt_is_the_same_for_every_question_about_a_run(tmp_path):
    """A timestamp early in the prompt kept every follow-up from reusing the cached evidence."""
    import time
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    from app.services.ai.memory.fact_ledger import FactLedger
    jtl = tmp_path / 'result.jtl'
    jtl.write_text('timeStamp,elapsed\n1,2\n', encoding='utf-8')
    tasks = []
    for _ in range(2):
        fact = FactLedger.compute_from_summary(summary(), 'run-1', jtl_path=str(jtl))
        tasks.append(AnalysisAgent._build_base_task(AnalysisAgent.__new__(AnalysisAgent), 'run-1', fact, ''))
        time.sleep(0.01)
    assert tasks[0] == tasks[1]
    assert str(tmp_path) not in tasks[0] and 'computed_at' not in tasks[0]
