"""Score the number checker, or a model's reports, on the hand-labelled runs in tests/golden/report_claims.json.

    python scripts/eval_reports.py                      # the checker alone: free, no model calls
    python scripts/eval_reports.py --list-models        # the configured models
    python scripts/eval_reports.py --model <id> [--lang en] [--repeat 3]

With --model, the model writes a report for each golden run, as after a real run: one structured call,
the numbers checked, and one rewrite when some are wrong. Each report is scored on the numbers that were
wrong before and after the rewrite, the numbers checked, empty sections and the language. Results go to
eval_results/ and are compared with that model's previous result, so a prompt or model change shows up
as numbers. This calls the model: it costs tokens.

Run it from jmeter-agent-backend. Exit codes: 0 good, 1 the checker missed or wrongly flagged a sentence.
"""
import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.services.ai import golden  # noqa: E402

RESULTS = Path(settings.BASE_DIR) / 'eval_results'
REQUIRED = ('key_findings', 'performance_bottlenecks', 'recommendations')


def checker() -> int:
    score = golden.score_checker()
    total = len(score.caught) + len(score.missed) + len(score.false_alarms) + len(score.passed)
    print(f'Number checker on {total} labelled sentences: recall {score.recall:.0%}, precision {score.precision:.0%}')
    for case in score.missed:
        print(f'  MISSED       {case["text"]}  ({case["why"]})')
    for case in score.false_alarms:
        print(f'  FALSE ALARM  {case["text"]}  -> {case["mismatches"]}')
    return 0 if not score.missed and not score.false_alarms else 1


async def report(model: str, name: str, run: dict, lang: str) -> dict:
    from app.services.ai.analyzer import TestAnalyzer, check_numbers
    summary = golden.summary(run)
    started = time.monotonic()
    analyzer = TestAnalyzer(model_type=model)
    analysis = await analyzer.analyze(summary_data=summary.model_dump(), test_config={'test_type': 'baseline'},
                                      test_type='baseline', lang=lang)
    analysis = check_numbers(analysis, summary, f'golden-{name}', run.get('compared_with', ()))
    first = list((analysis.fact_check or {}).get('mismatches', []))
    if first:
        rewritten = await analyzer.correct_numbers(first)
        if rewritten is not None:
            rewritten = check_numbers(rewritten, summary, f'golden-{name}', run.get('compared_with', ()))
            if len((rewritten.fact_check or {}).get('mismatches', [])) < len(first):
                analysis = rewritten
    return {
        'run': name, 'status': analysis.analysis_status, 'seconds': round(time.monotonic() - started, 1),
        'checked': (analysis.fact_check or {}).get('checked', 0),
        'wrong_first': len(first), 'wrong_final': len((analysis.fact_check or {}).get('mismatches', [])),
        'empty_sections': [section for section in REQUIRED if not getattr(analysis, section, None)],
        'language_ok': (analysis.language or 'zh-CN') == lang, 'mismatches': (analysis.fact_check or {}).get('mismatches', []),
    }


def totals(rows: list[dict]) -> dict:
    done = [row for row in rows if row['status'] == 'completed']
    return {'reports': len(rows), 'completed': len(done), 'checked': sum(row['checked'] for row in done),
            'wrong_first': sum(row['wrong_first'] for row in done), 'wrong_final': sum(row['wrong_final'] for row in done),
            'empty_sections': sum(len(row['empty_sections']) for row in done),
            'language_ok': sum(row['language_ok'] for row in done),
            'seconds': round(sum(row['seconds'] for row in rows), 1)}


def previous(model: str, lang: str):
    files = sorted(RESULTS.glob(f'*-{safe(model)}-{lang}.json')) if RESULTS.exists() else []
    return json.loads(files[-1].read_text(encoding='utf-8')) if files else None


def safe(text: str) -> str:
    return ''.join(ch if ch.isalnum() or ch in '-_.' else '_' for ch in text)


async def evaluate(model: str, lang: str, repeat: int) -> int:
    data = golden.load()
    rows = []
    for _ in range(repeat):
        for name, run in data['runs'].items():
            try:
                rows.append(await report(model, name, run, lang))
            except Exception as error:  # One failed call is a result too, not the end of the evaluation.
                rows.append({'run': name, 'status': f'error: {error}', 'seconds': 0, 'checked': 0, 'wrong_first': 0,
                             'wrong_final': 0, 'empty_sections': [], 'language_ok': False, 'mismatches': []})
            row = rows[-1]
            print(f"  {row['run']:<10} {row['status']:<10} checked {row['checked']:>3}  wrong {row['wrong_first']} -> "
                  f"{row['wrong_final']}  empty {len(row['empty_sections'])}  {row['seconds']}s")
    result = {'model': model, 'lang': lang, 'at': datetime.now().isoformat(timespec='seconds'),
              'totals': totals(rows), 'reports': rows}
    before = previous(model, lang)
    print('Totals:', result['totals'])
    if before:
        changed = {key: f"{before['totals'].get(key)} -> {value}" for key, value in result['totals'].items()
                   if before['totals'].get(key) != value}
        print(f"Against {before['at']}:", changed or 'no change')
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{datetime.now():%Y%m%d-%H%M%S}-{safe(model)}-{lang}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Saved', path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--model', help='model id from --list-models; omit to score the number checker only')
    parser.add_argument('--lang', default='zh-CN', choices=['zh-CN', 'en'])
    parser.add_argument('--repeat', type=int, default=1, help='reports per run, to see how much answers vary')
    parser.add_argument('--list-models', action='store_true')
    args = parser.parse_args()
    if args.list_models:
        from app.services.ai.adapters.factory import AIAdapterFactory
        for model in AIAdapterFactory.get_available_models():
            print(f"{model['id']:<24} {model['name']}  ({model['model']})")
        return 0
    status = checker()
    if args.model:
        status = max(status, asyncio.run(evaluate(args.model, args.lang, max(1, args.repeat))))
    return status


if __name__ == '__main__':
    sys.exit(main())
