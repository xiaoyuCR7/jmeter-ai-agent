"""Tests for FactValidator and MetricClaimExtractor."""

import os
import pytest

from app.services.ai.memory.validators import (
    MetricClaimExtractor, FactValidator, HypothesisExtractor,
)
from app.services.ai.memory.fact_ledger import FactLedger


@pytest.fixture
def sample_jtl():
    return os.path.join(os.path.dirname(__file__), "fixtures", "sample.jtl")


@pytest.fixture
def fact(sample_jtl):
    return FactLedger.compute_from_jtl(
        jtl_path=sample_jtl,
        execution_id="exec-val",
        module="api",
        version="v1",
    )


def test_extract_metric_claims():
    text = """
    本次测试 p99 为 250ms，平均响应时间为 195ms。
    吞吐量 TPS 为 1.0 req/s，错误率为 20%。
    总请求数 1000。
    """
    extractor = MetricClaimExtractor()
    claims = extractor.extract(text)

    by_name = {c.metric_name: c for c in claims}
    assert "p99" in by_name
    assert "avg_response_time" in by_name
    assert "throughput" in by_name
    assert "error_rate" in by_name
    assert by_name["p99"].claimed_value == 250.0
    assert by_name["avg_response_time"].claimed_value == 195.0


def test_validator_accepts_correct_numbers(fact):
    validator = FactValidator()
    text = f"""
    本次测试平均响应时间为 {fact.avg_response_time}ms，
    p99 为 {fact.percentiles['p99']}ms，
    吞吐量为 {fact.throughput} req/s，
    错误率为 {fact.error_rate}%。
    """
    result = validator.validate(text, fact)
    assert result.is_valid is True
    assert len(result.mismatches) == 0


def test_validator_rejects_wrong_numbers(fact):
    validator = FactValidator()
    text = f"""
    本次测试 p99 为 999ms（实际应为 {fact.percentiles['p99']}ms），
    吞吐量为 100 req/s（实际应为 {fact.throughput}）。
    """
    result = validator.validate(text, fact)
    assert result.is_valid is False
    assert len(result.mismatches) >= 2

    mismatch_names = {m["metric"] for m in result.mismatches}
    assert "p99" in mismatch_names
    assert "throughput" in mismatch_names


def test_missing_fact_field_cannot_validate_a_numeric_claim(fact):
    fact.percentiles = {}
    result = FactValidator().validate('p99 为 123ms', fact)
    assert not result.is_valid
    assert 'p99' in result.missing_fields


def test_validator_handles_seconds_conversion(fact):
    """Claims in seconds should be converted to ms for response time metrics."""
    validator = FactValidator()
    # fact.avg_response_time is ~205ms => 0.205s
    text = f"平均响应时间为 {fact.avg_response_time / 1000.0}s"
    result = validator.validate(text, fact)
    assert result.is_valid is True


def test_hypothesis_extractor():
    text = """
    整体评估：系统在高并发下表现不佳。
    假设：瓶颈在数据库连接池。
    初步判断：p99 恶化是因为锁竞争。
    建议：增加连接池大小。
    """
    extractor = HypothesisExtractor()
    hyps = extractor.extract(text, execution_id="exec-001", module="api", version="v1")

    statements = {h.statement for h in hyps}
    assert any("连接池" in s for s in statements)
    assert any("锁竞争" in s for s in statements)


def scoped_fact():
    from app.models.schemas import FactSheet
    return FactSheet(execution_id='x', total_requests=1000, avg_response_time=300, max_response_time=2000,
                     error_rate=1.0, throughput=100, percentiles={'p50': 200, 'p95': 500, 'p99': 900},
                     per_label={'GET /items': {'count': 600, 'p95_response_time': 1200, 'avg_response_time': 10},
                                'POST /login': {'count': 400, 'p95_response_time': 300, 'avg_response_time': 250}})


def test_a_number_given_for_one_request_is_checked_against_that_request():
    fact = scoped_fact()
    assert FactValidator().validate('GET /items 的 P95 为 1200ms', fact).mismatches == []
    wrong = FactValidator().validate('POST /login 的 P95 为 1200ms', fact).mismatches
    assert wrong and wrong[0]['scope'] == 'POST /login' and wrong[0]['actual'] == 300


def test_overall_numbers_are_not_matched_against_a_single_request():
    fact = scoped_fact()
    assert FactValidator().validate('整体 P95 为 500ms', fact).mismatches == []
    assert FactValidator().validate('整体 P95 为 1200ms', fact).mismatches


def test_numbers_of_other_runs_count_only_where_the_sentence_compares():
    fact = scoped_fact()
    assert FactValidator().validate('P95 从 650ms 升到 500ms', fact, [650.0]).mismatches == []
    assert FactValidator().validate('P95 为 650ms', fact, [650.0]).mismatches


def test_small_response_times_get_a_small_tolerance():
    fact = scoped_fact()
    assert FactValidator().validate('GET /items 平均响应时间 12ms', fact).mismatches == []
    assert FactValidator().validate('GET /items 平均响应时间 55ms', fact).mismatches


def test_english_claims_are_checked():
    fact = scoped_fact()
    good = 'Overall average response time was 300 ms, throughput 100 req/s and the error rate 1%.'
    result = FactValidator().validate(good, fact)
    assert {c.metric_name for c in result.claims} == {'avg_response_time', 'throughput', 'error_rate'}
    assert result.mismatches == []
    assert FactValidator().validate('The error rate was 12%.', fact).mismatches
    assert FactValidator().validate('Throughput reached 400 requests/sec.', fact).mismatches
    assert FactValidator().validate('POST /login has a P95 of 1.2 seconds.', fact).mismatches


def test_claims_without_a_scope_are_counted():
    assert FactValidator().validate('P95 为 1200ms', scoped_fact()).unbound_claims == 1
