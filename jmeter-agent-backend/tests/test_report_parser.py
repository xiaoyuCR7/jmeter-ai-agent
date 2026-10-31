"""JTL parsing: results must not depend on format, and cost must stay bounded."""
import os
import random
import time
import tracemalloc
from io import StringIO

from app.services.jmeter import report_parser
from app.services.jmeter.report_parser import ReportParser

CSV_HEADER = 'timeStamp,elapsed,label,responseCode,responseMessage,success,bytes,sentBytes\n'


def _samples(count, seed=7):
    """(ts, elapsed, label, code, message, success, received, sent) in shuffled arrival order."""
    rng = random.Random(seed)
    rows = []
    for index in range(count):
        success = rng.random() > 0.1
        rows.append((1_700_000_000_000 + index * 37, rng.randint(1, 7000), rng.choice(['login', 'search', 'pay']),
                     '200' if success else rng.choice(['500', '503']), 'OK' if success else 'Server Error',
                     success, rng.randint(300, 9000), rng.randint(300, 900)))
    rng.shuffle(rows)
    return rows


def _csv(rows):
    return CSV_HEADER + '\n'.join(
        f'{ts},{elapsed},{label},{code},{message},{str(success).lower()},{received},{sent}'
        for ts, elapsed, label, code, message, success, received, sent in rows)


def _xml(rows):
    return '<testResults>' + ''.join(
        f'<httpSample ts="{ts}" t="{elapsed}" lb="{label}" rc="{code}" rm="{message}" '
        f's="{str(success).lower()}" by="{received}" sb="{sent}"/>'
        for ts, elapsed, label, code, message, success, received, sent in rows) + '</testResults>'


def _percentile(values, p):
    data = sorted(values)
    k = (len(data) - 1) * p
    low = int(k)
    high = min(low + 1, len(data) - 1)
    return data[low] if low == high else data[low] * (high - k) + data[high] * (k - low)


def test_response_time_statistics_match_a_sorted_list_reference():
    rows = _samples(5000)
    summary = ReportParser()._parse_csv_jtl(StringIO(_csv(rows)))
    elapsed = [row[1] for row in rows]

    assert summary.total_requests == len(rows)
    assert summary.min_response_time == min(elapsed)
    assert summary.max_response_time == max(elapsed)
    assert summary.avg_response_time == round(sum(elapsed) / len(elapsed), 2)
    assert summary.percentiles == {
        name: round(_percentile(elapsed, p), 2)
        for name, p in (('p50', .5), ('p90', .9), ('p95', .95), ('p99', .99))}
    assert summary.error_rate == round(sum(not row[5] for row in rows) / len(rows) * 100, 2)
    for label in ('login', 'search', 'pay'):
        own = [row[1] for row in rows if row[2] == label]
        stats = summary.per_label[label]
        assert stats['count'] == len(own)
        assert stats['avg_response_time'] == round(sum(own) / len(own), 2)
        assert (stats['min_response_time'], stats['max_response_time']) == (min(own), max(own))
        assert stats['p95_response_time'] == round(_percentile(own, .95), 2)
        assert stats['p99_response_time'] == round(_percentile(own, .99), 2)


def test_timeline_segments_aggregate_each_window():
    rows = _samples(5000)
    summary = ReportParser()._parse_csv_jtl(StringIO(_csv(rows)))
    origin = min(row[0] for row in rows)
    width = int(summary.timeline_segments[0].time_range.split('-')[1].rstrip('s')) * 1000
    windows = {}
    for ts, elapsed, *_rest in rows:
        windows.setdefault((ts - origin) // width, []).append(elapsed)

    assert [segment.request_count for segment in summary.timeline_segments] == [
        len(windows[index]) for index in sorted(windows)]
    assert [segment.max_response_time for segment in summary.timeline_segments] == [
        max(windows[index]) for index in sorted(windows)]
    assert [segment.avg_response_time for segment in summary.timeline_segments] == [
        round(sum(windows[index]) / len(windows[index]), 2) for index in sorted(windows)]


def test_csv_and_xml_results_of_the_same_run_are_identical():
    rows = _samples(3000)
    parser = ReportParser()
    assert (parser._parse_xml_jtl(StringIO(_xml(rows))).model_dump()
            == parser._parse_csv_jtl(StringIO(_csv(rows))).model_dump())


def test_xml_samples_with_unparseable_numbers_are_counted_without_crashing():
    xml = '<testResults><httpSample t="abc" ts="1000" lb="api" s="true"/></testResults>'
    summary = ReportParser()._parse_xml_jtl(StringIO(xml))
    assert summary.total_requests == 1
    assert summary.avg_response_time == 0


def test_parsing_memory_does_not_keep_python_objects_per_sample(tmp_path):
    count = 100_000
    path = tmp_path / 'large.jtl'
    path.write_text(_csv(_samples(count)), encoding='utf-8')

    tracemalloc.start()
    try:
        with open(path, encoding='utf-8') as stream:
            summary = ReportParser()._parse_csv_jtl(stream)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert summary.total_requests == count
    assert peak < count * 60


def _write_jtl(tmp_path, modified_seconds_ago):
    path = tmp_path / 'result.jtl'
    path.write_text(_csv(_samples(2)), encoding='utf-8')
    stamp = time.time() - modified_seconds_ago
    os.utime(path, (stamp, stamp))
    return str(path)


def test_settled_jtl_is_parsed_without_a_stabilization_wait(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(report_parser.time, 'sleep', sleeps.append)
    summary = ReportParser().parse_jtl(_write_jtl(tmp_path, modified_seconds_ago=3600))
    assert summary.total_requests == 2
    assert sleeps == []


def test_just_written_jtl_still_waits_for_its_size_to_settle(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(report_parser.time, 'sleep', sleeps.append)
    summary = ReportParser().parse_jtl(_write_jtl(tmp_path, modified_seconds_ago=0))
    assert summary.total_requests == 2
    assert sleeps
