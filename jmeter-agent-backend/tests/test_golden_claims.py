"""The number checker on hand-labelled report sentences: every wrong number caught, no right one flagged."""
import subprocess
import sys
from pathlib import Path

from app.services.ai import golden


def test_the_checker_catches_every_wrong_number_and_flags_no_right_one():
    score = golden.score_checker()
    assert [case['text'] for case in score.missed] == []
    assert [case['text'] for case in score.false_alarms] == []
    assert len(score.caught) >= 10 and len(score.passed) >= 10  # The set keeps both kinds.


def test_every_case_names_a_run_and_says_why():
    data = golden.load()
    for case in data['cases']:
        assert case['run'] in data['runs'] and case['why'] and isinstance(case['wrong'], bool)
    for name, run in data['runs'].items():
        golden.summary(run)  # Each run is a valid summary.


def test_the_eval_script_scores_the_checker_without_a_model():
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, 'scripts/eval_reports.py'], cwd=backend, capture_output=True,
                            text=True, encoding='utf-8', timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'recall 100%, precision 100%' in result.stdout
