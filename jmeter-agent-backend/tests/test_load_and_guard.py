"""Running a plan at another load, and the guard that stops a run whose requests nearly all fail."""
import asyncio
import xml.etree.ElementTree as ET

import pytest

from app.core.config import settings
from app.services.jmeter import run_guard
from app.services.jmeter.load_variants import scale_threads, total_threads


def plan(*groups) -> bytes:
    body = ''.join(
        f'<{tag} testname="{name}" enabled="{"false" if off else "true"}"><stringProp name="ThreadGroup.num_threads">'
        f'{threads}</stringProp></{tag}><hashTree/>' for tag, name, threads, off in groups)
    return f'<?xml version="1.0"?><jmeterTestPlan><hashTree><TestPlan/><hashTree>{body}</hashTree></hashTree></jmeterTestPlan>'.encode()


def threads_of(content: bytes) -> dict:
    root = ET.fromstring(content)
    return {group.get('testname'): group.find("stringProp[@name='ThreadGroup.num_threads']").text
            for group in root.iter() if group.tag.endswith('ThreadGroup')}


def test_threads_scale_in_proportion_and_add_up():
    content = plan(('ThreadGroup', 'step1', 10, False), ('ThreadGroup', 'step2', 30, False),
                   ('SetupThreadGroup', 'setup', 1, False), ('ThreadGroup', 'off', 5, True))
    scaled = scale_threads(content, 100)
    assert threads_of(scaled) == {'step1': '25', 'step2': '75', 'setup': '1', 'off': '5'}
    assert total_threads(scaled) == 100
    small = threads_of(scale_threads(content, 3))
    assert int(small['step1']) >= 1 and int(small['step1']) + int(small['step2']) == 3


def test_threads_that_are_variables_cannot_be_scaled():
    with pytest.raises(ValueError, match='固定数字'):
        scale_threads(plan(('ThreadGroup', 'g', '${__P(users,10)}', False)), 20)
    with pytest.raises(ValueError):
        scale_threads(plan(('ThreadGroup', 'g', 10, False)), 0)


def window(rows):
    w = run_guard.Window(30)
    for stamp, ok in rows:
        w.add(stamp, ok)
    return w


def test_the_guard_waits_for_its_grace_period_and_enough_samples(monkeypatch):
    monkeypatch.setattr(settings, 'RUN_GUARD_GRACE_SECONDS', 30)
    monkeypatch.setattr(settings, 'RUN_GUARD_MIN_SAMPLES', 50)
    monkeypatch.setattr(settings, 'RUN_GUARD_ERROR_RATE', 90)
    early = window([(1000 + i, False) for i in range(100)])
    assert run_guard.judge(early, 2000) is None  # Still in the grace period.
    failing = window([(0, True)] + [(40_000 + i, False) for i in range(100)])
    assert '失败' in run_guard.judge(failing, 40_100)
    few = window([(0, True)] + [(40_000 + i, False) for i in range(10)])
    assert run_guard.judge(few, 40_010) is None
    mostly_fine = window([(0, True)] + [(40_000 + i, i % 2 == 0) for i in range(100)])
    assert run_guard.judge(mostly_fine, 40_100) is None


def test_the_tail_reads_only_complete_new_rows(tmp_path):
    path = tmp_path / 'result.jtl'
    path.write_text('timeStamp,elapsed,label,success\n1,10,a,true\n2,10,a,fal', encoding='utf-8')
    tail = run_guard.JtlTail(str(path))
    assert [row['timeStamp'] for row in tail.read()] == ['1']
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write('se\n3,10,a,true\n')
    rows = tail.read()
    assert [(row['timeStamp'], row['success']) for row in rows] == [('2', 'false'), ('3', 'true')]


@pytest.mark.asyncio
async def test_the_guard_stops_after_two_bad_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'RUN_GUARD_GRACE_SECONDS', 0)
    monkeypatch.setattr(settings, 'RUN_GUARD_MIN_SAMPLES', 5)
    path = tmp_path / 'result.jtl'
    path.write_text('timeStamp,success\n' + ''.join(f'{i},false\n' for i in range(10)), encoding='utf-8')
    reasons = []

    async def stop(reason):
        reasons.append(reason)
    await asyncio.wait_for(run_guard.watch(str(path), stop, check_seconds=0.01), timeout=2)
    assert len(reasons) == 1


@pytest.mark.asyncio
async def test_a_disabled_guard_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'RUN_GUARD_ENABLED', False)
    await asyncio.wait_for(run_guard.watch(str(tmp_path / 'x.jtl'), None), timeout=1)
