"""The hand-labelled report sentences in tests/golden/report_claims.json, scored by the number checker.

Used by the test suite (the checker must catch every wrong number and pass every right one) and by
scripts/eval_reports.py, which also scores reports a model writes for the same runs.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.models.schemas import DetailedTestSummary
from app.services.ai.memory.fact_ledger import FactLedger
from app.services.ai.memory.validators import FactValidator

GOLDEN = Path(__file__).resolve().parents[3] / 'tests' / 'golden' / 'report_claims.json'


@dataclass
class Score:
    caught: list = field(default_factory=list)  # Wrong and flagged.
    missed: list = field(default_factory=list)  # Wrong but passed: a wrong number would reach the reader.
    false_alarms: list = field(default_factory=list)  # Right but flagged: the model would be told to change it.
    passed: list = field(default_factory=list)  # Right and passed.

    @property
    def recall(self) -> float:
        wrong = len(self.caught) + len(self.missed)
        return len(self.caught) / wrong if wrong else 1.0

    @property
    def precision(self) -> float:
        flagged = len(self.caught) + len(self.false_alarms)
        return len(self.caught) / flagged if flagged else 1.0


def load() -> dict:
    return json.loads(GOLDEN.read_text(encoding='utf-8'))


def summary(run: dict) -> DetailedTestSummary:
    return DetailedTestSummary(**run['summary'])


def fact_sheet(name: str, run: dict):
    return FactLedger.compute_from_summary(summary(run), f'golden-{name}')


def score_checker(data: dict | None = None) -> Score:
    data = data or load()
    validator, score = FactValidator(), Score()
    for case in data['cases']:
        run = data['runs'][case['run']]
        result = validator.validate(case['text'], fact_sheet(case['run'], run), run.get('compared_with', ()))
        flagged = bool(result.mismatches)
        bucket = (score.caught if flagged else score.missed) if case['wrong'] else \
            (score.false_alarms if flagged else score.passed)
        bucket.append(case | {'mismatches': result.mismatches})
    return score
