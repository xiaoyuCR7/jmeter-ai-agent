"""Measure the AI analysis on finished runs with a real model, so prompt changes are judged by numbers.

For each run it writes the report analysis again (nothing is saved to the run) and asks a few fixed
questions, then prints and saves:
- numbers checked, mismatched, and whether the one rewrite fixed them;
- how many hypotheses carry a prediction a later run can check;
- answer length, and tokens used.

Run from jmeter-agent-backend while the app's database exists (the server may be running):
    py -3.13 scripts/eval_analysis.py --runs 3
    py -3.13 scripts/eval_analysis.py --ids <execution id> <execution id> --model deepseek
Results go to reports/eval/<time>.json. Each run costs several model calls.
"""

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

QUESTIONS = ['错误主要集中在哪个接口？原因可能是什么？', '下一步应该先查什么？']


async def pick_runs(ids, count):
    from app.services.storage import store
    await store.sync_from_dbstore()
    if ids:
        return [store.executions[i] for i in ids if i in store.executions]
    finished = [e for e in store.executions.values()
                if e.status.value == 'completed' and e.summary is not None and e.report_dir
                and os.path.isfile(os.path.join(e.report_dir, 'result.jtl'))]
    finished.sort(key=lambda e: e.start_time, reverse=True)
    return finished[:count]


async def report_metrics(execution, model):
    from app.services import post_run
    from app.services.ai.memory import prediction
    analysis = await post_run.analysis_for(execution.model_copy(deep=True), model)
    check = analysis.fact_check or {}
    from app.services.ai.memory.fact_ledger import FactLedger
    labels = set(FactLedger.compute_from_summary(execution.summary, execution.id).per_label or {})
    hypotheses = analysis.hypotheses or []
    checkable = sum(prediction.checkable(prediction.normalize(h.get('prediction'), labels)) for h in hypotheses)
    return {
        'status': analysis.analysis_status, 'evidence': analysis.evidence_status,
        'numbers_checked': check.get('checked', 0), 'mismatches': len(check.get('mismatches', [])),
        'corrected_from': check.get('corrected_from', 0),
        'hypotheses': len(hypotheses), 'checkable_predictions': checkable,
        'usage': analysis.usage, 'analysis': analysis.model_dump(exclude={'follow_ups'}),
    }, analysis


async def question_metrics(execution, analysis, model):
    from app.services.ai import usage
    from app.services.ai.adapters.factory import AIAdapterFactory
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    evidence = await AnalysisAgent.resolve_execution(execution.id)
    context = json.dumps({'report': analysis.model_dump(exclude={'follow_ups', 'fact_check'}), 'earlier_questions': []},
                         ensure_ascii=False, default=str)
    answers = []
    for question in QUESTIONS:
        with usage.metered() as used:
            result = await AnalysisAgent(AIAdapterFactory.create(model)).run_analysis(question, evidence, context=context)
        validation = (result.analysis or {}).get('validation') or {}
        answers.append({'question': question, 'success': result.success, 'error': result.error,
                        'chars': len(result.output or ''), 'mismatches': len(validation.get('mismatches') or []),
                        'corrected_from': (result.analysis or {}).get('corrected_from', 0),
                        'usage': used, 'answer': result.output})
    return answers


def show(rows):
    print(f"\n{'run':10} {'status':10} {'numbers':>7} {'wrong':>5} {'fixed':>5} {'hyp':>4} {'checkable':>9} "
          f"{'tokens in/out':>15} {'answers (chars, wrong)':>24}")
    for row in rows:
        r = row['report']
        used = r['usage'] or {}
        answers = ', '.join(f"{a['chars']}/{a['mismatches']}" if a['success'] else 'failed' for a in row['questions'])
        print(f"{row['execution_id'][:8]:10} {r['status']:10} {r['numbers_checked']:>7} {r['mismatches']:>5} "
              f"{r['corrected_from']:>5} {r['hypotheses']:>4} {r['checkable_predictions']:>9} "
              f"{used.get('input_tokens', 0):>7}/{used.get('output_tokens', 0):<7} {answers:>24}")


async def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--runs', type=int, default=3, help='how many of the newest finished runs to use')
    parser.add_argument('--ids', nargs='*', help='execution ids to use instead of the newest runs')
    parser.add_argument('--model', help='model id from Settings; the default model if omitted')
    parser.add_argument('--no-questions', action='store_true', help='measure the report only')
    args = parser.parse_args()

    from app.core.config import settings
    runs = await pick_runs(args.ids, args.runs)
    if not runs:
        print('No finished run with a result.jtl was found.')
        return
    rows = []
    for execution in runs:
        print(f'Analysing {execution.id} ...', flush=True)
        report, analysis = await report_metrics(execution, args.model)
        questions = [] if args.no_questions or report['status'] != 'completed' else \
            await question_metrics(execution, analysis, args.model)
        rows.append({'execution_id': execution.id, 'report': report, 'questions': questions})
    show(rows)
    out_dir = os.path.join(settings.REPORTS_DIR, 'eval')
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, time.strftime('%Y%m%d-%H%M%S') + '.json')
    with open(path, 'w', encoding='utf-8') as file:
        json.dump({'model': args.model, 'runs': rows}, file, ensure_ascii=False, indent=1, default=str)
    print(f'\nSaved {path}')


if __name__ == '__main__':
    asyncio.run(main())
