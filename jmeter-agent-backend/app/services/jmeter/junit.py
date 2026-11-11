"""A finished run as JUnit XML, the format CI systems read test results in.

One test case per request label; a label with failed requests is a failed case. The suite's time is how
long the run lasted and a case's time is that request's average response time, both in seconds.
"""
from typing import Optional
from xml.etree import ElementTree as ET

from app.models.schemas import DetailedTestSummary


def _failed(total: int, error_rate: float) -> int:
    """Failed requests from a percentage: rounded, since 3 failures of 1000 is 0.3% and not zero."""
    return min(total, round(float(error_rate or 0) / 100 * total))


def junit_xml(summary: DetailedTestSummary, name: str = 'JMeter Test', timestamp: Optional[str] = None) -> bytes:
    suite = ET.Element('testsuite', name=name, errors='0')
    labels = summary.per_label or {}
    cases = labels or {'All Requests': {
        'avg_response_time': summary.avg_response_time, 'error_rate': summary.error_rate,
        'count': summary.total_requests, 'p95_response_time': summary.percentiles.get('p95'),
        'p99_response_time': summary.percentiles.get('p99')}}
    failures = 0
    for label, stats in cases.items():
        case = ET.SubElement(suite, 'testcase', classname=name, name=str(label),
                             time=f"{float(stats.get('avg_response_time') or 0) / 1000:.3f}")
        rate = float(stats.get('error_rate') or 0)
        if rate > 0:
            failures += 1
            count = int(stats.get('count') or stats.get('total_requests') or 0)
            failed = f'{_failed(count, rate)} of {count} requests failed' if count else 'requests failed'
            failure = ET.SubElement(case, 'failure', message=f'Error rate: {rate:g}% ({failed})')
            failure.text = (f"P95: {stats.get('p95_response_time', 'N/A')}ms, "
                            f"P99: {stats.get('p99_response_time', 'N/A')}ms")
    duration = summary.total_requests / summary.throughput if summary.throughput else 0.0
    suite.set('tests', str(len(cases)))
    suite.set('failures', str(failures))
    suite.set('time', f'{duration:.3f}')
    if timestamp:
        suite.set('timestamp', timestamp)
    # Whole-run numbers for jobs that plot them; JUnit readers ignore unknown properties.
    properties = ET.SubElement(suite, 'properties')
    for key, value in (('total_requests', summary.total_requests),
                       ('failed_requests', _failed(summary.total_requests, summary.error_rate)),
                       ('error_rate_percent', summary.error_rate), ('throughput_per_second', summary.throughput),
                       ('avg_response_time_ms', summary.avg_response_time),
                       ('p95_response_time_ms', summary.percentiles.get('p95')),
                       ('p99_response_time_ms', summary.percentiles.get('p99'))):
        if value is not None:
            ET.SubElement(properties, 'property', name=key, value=f'{value:g}' if isinstance(value, float) else str(value))
    suite.remove(properties)
    suite.insert(0, properties)
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(suite, encoding='utf-8')
