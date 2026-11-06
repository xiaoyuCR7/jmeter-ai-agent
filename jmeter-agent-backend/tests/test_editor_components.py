"""What the plan tree editor offers, checked against JMeter itself.

The registry once offered a JSON assertion JMeter could not load (JSONAssertion), and If, Loop, While,
Throughput and ForEach controllers whose settings JMeter never read (condition instead of
IfController.condition): the plan loaded and those settings silently did nothing.

The name check reads the installed JMeter's jars and runs wherever JMeter is installed; the runs
start JMeter and need RUN_JMETER_TESTS=1.
"""
import csv
import os
import re
import subprocess
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from app.services.jmeter import jmx_lint
from app.services.jmeter.component_registry import COMPONENT_REGISTRY
from app.services.jmeter.jmx_builder import _get_guiclass, build_jmx

ROOT = Path(__file__).resolve().parents[2]
SAVESERVICE = ROOT / 'bin' / 'saveservice.properties'
JMETER = ROOT / 'bin' / ('jmeter.bat' if os.name == 'nt' else 'jmeter')
installed = pytest.mark.skipif(not SAVESERVICE.exists(), reason='JMeter is not installed in the project root')
runs = pytest.mark.skipif(not os.environ.get('RUN_JMETER_TESTS') or not JMETER.exists(),
                          reason='set RUN_JMETER_TESTS=1 to start JMeter')
# Names the check cannot see in a jar: built by the thread-group builder, or plain field names.
NOT_IN_JARS = {'loops', 'clearEachIteration', 'useExpires', 'JSON_PATH', 'EXPECTED_VALUE', 'JSONVALIDATION',
               'EXPECT_NULL', 'INVERT', 'ISREGEX'}


def _jar_strings():
    strings = set()
    for jar in (ROOT / 'lib' / 'ext').glob('ApacheJMeter_*.jar'):
        with zipfile.ZipFile(jar) as archive:
            for name in archive.namelist():
                if name.endswith('.class'):
                    strings.update(s.decode('latin1') for s in re.findall(rb'[A-Za-z_][A-Za-z0-9_.]{2,80}', archive.read(name)))
    return strings


@installed
def test_every_element_and_property_the_editor_writes_is_one_jmeter_reads():
    aliases = {alias for line in SAVESERVICE.read_text(encoding='utf-8').splitlines()
               if '=' in line and not line.startswith('#') for alias in line.split('=')[0].split(',')}
    strings = _jar_strings()
    wrong = []
    for testclass, meta in COMPONENT_REGISTRY.items():
        gui = _get_guiclass(testclass, {'properties': {}})
        if testclass not in aliases or gui not in aliases:
            wrong.append(f'{testclass} / {gui}')
        bean = gui == 'TestBeanGUI'
        for name in {**meta.get('properties', {}), **meta.get('defaults', {})}:
            if name.startswith('_'):  # The editor's own bookkeeping, turned into JMeter elements by the builder.
                continue
            known = name in NOT_IN_JARS or ('.' in name and name in strings) or (
                bean and any(f'{verb}{name[0].upper()}{name[1:]}' in strings for verb in ('get', 'is')))
            if not known:
                wrong.append(f'{testclass}.{name}')
    assert wrong == []


def _every_component_plan() -> str:
    """One of everything the editor offers, with its defaults, placed where the editor allows it."""
    def node(testclass, children=()):
        meta = COMPONENT_REGISTRY[testclass]
        return {'testclass': testclass, 'testname': testclass, 'enabled': True,
                'properties': dict(meta.get('defaults', {})), 'children': list(children)}

    def under(parent, kinds):
        return [name for name in COMPONENT_REGISTRY[parent]['allowed_children']
                if COMPONENT_REGISTRY[name]['category'] in kinds]

    kinds = {'assertion', 'extractor', 'timer', 'pre_processor', 'post_processor', 'config'}
    samplers = [node(name) for name in under('ThreadGroup', {'sampler'})]
    placed = set()
    for sampler in samplers:  # Each leaf under the first sampler that takes it.
        leaves = [name for name in under(sampler['testclass'], kinds) if name not in placed]
        sampler['children'] = [node(name) for name in leaves]
        placed.update(leaves)
    controllers = [node(name, [node('DebugSampler')]) for name in under('ThreadGroup', {'controller'})]
    group = node('ThreadGroup', [*samplers, *controllers,
                                 *[node(name) for name in under('ThreadGroup', {'config', 'timer', 'listener'})]])
    plan = node('TestPlan', [group, *[node(name) for name in under('TestPlan', {'config', 'listener'})],
                             node('SetupThreadGroup'), node('PostThreadGroup')])
    return build_jmx(plan)


@runs
def test_a_plan_with_every_component_the_editor_offers_loads_in_jmeter(monkeypatch):
    import importlib
    from app.services.jmeter import executor
    monkeypatch.setattr(executor, 'get_executor', lambda: type('E', (), {'jmeter_path': str(JMETER)})())
    content = _every_component_plan()
    for testclass in COMPONENT_REGISTRY:
        assert f'testclass="{testclass}"' in content, testclass
    loaded = importlib.reload(jmx_lint).load_check(content)  # conftest replaces it for every other test
    assert loaded['checked'] and loaded['ok'], loaded


def _run(tree, folder) -> Counter:
    plan, results = folder / 'plan.jmx', folder / 'out.jtl'
    plan.write_text(build_jmx(tree), encoding='utf-8')
    subprocess.run([str(JMETER), '-n', '-t', str(plan), '-l', str(results), '-j', str(folder / 'jmeter.log')],
                   stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    assert results.exists(), (folder / 'jmeter.log').read_text(encoding='utf-8', errors='replace')[-800:]
    with results.open(encoding='utf-8') as stream:
        return Counter(row['label'] for row in csv.DictReader(stream))


@runs
def test_controller_settings_change_what_jmeter_runs(tmp_path):
    def debug(name):
        return {'testclass': 'DebugSampler', 'testname': name, 'enabled': True,
                'properties': dict(COMPONENT_REGISTRY['DebugSampler']['defaults']), 'children': []}

    def control(testclass, properties, *children):
        return {'testclass': testclass, 'testname': testclass, 'enabled': True,
                'properties': {**COMPONENT_REGISTRY[testclass]['defaults'], **properties}, 'children': list(children)}

    group = control('ThreadGroup', {'ThreadGroup.num_threads': 2, 'loops': 4}, *[
        control('IfController', {'IfController.condition': '${__jexl3(false)}'}, debug('skipped by if')),
        control('LoopController', {'LoopController.loops': 3}, debug('looped')),
        control('OnceOnlyController', {}, debug('once')),
        control('ThroughputController', {'ThroughputController.style': 0, 'ThroughputController.maxThroughput': 1,
                                         'ThroughputController.perThread': True}, debug('throttled')),
        control('SwitchController', {'SwitchController.value': '1'}, debug('switch 0'), debug('switch 1')),
        control('TransactionController', {'TransactionController.parent': True}, debug('inside')),
        control('CounterConfig', {'CounterConfig.name': 'n', 'CounterConfig.format': '000'}),
        debug('counter ${n}'),
    ])
    seen = _run(control('TestPlan', {}, group), tmp_path)
    assert 'skipped by if' not in seen
    assert seen['looped'] == 2 * 4 * 3
    assert seen['once'] == 2
    assert seen['throttled'] == 2  # Once per thread.
    assert seen['switch 1'] == 8 and 'switch 0' not in seen
    assert seen['TransactionController'] == 8
    assert {label for label in seen if label.startswith('counter ')} == {f'counter {n:03d}' for n in range(1, 9)}


def test_a_plan_saved_with_the_old_names_gets_jmeters_names():
    from app.services.jmeter.jmx_parser import parse_jmx
    old = {'testclass': 'TestPlan', 'testname': 'p', 'properties': {}, 'children': [
        {'testclass': 'ThreadGroup', 'testname': 'g', 'properties': {}, 'children': [
            {'testclass': 'IfController', 'testname': 'if', 'properties': {'condition': '${ok}'}, 'children': [
                {'testclass': 'DebugSampler', 'testname': 'd', 'properties': {}, 'children': [
                    {'testclass': 'JSONAssertion', 'testname': 'j', 'properties': {'JSON_PATH': '$.code'}, 'children': []}]}]},
            {'testclass': 'SimpleController', 'testname': 's', 'properties': {}, 'children': []}]}]}
    content = build_jmx(old)
    assert '<JSONPathAssertion guiclass="JSONPathAssertionGui" testclass="JSONPathAssertion"' in content
    assert '<GenericController guiclass="LogicControllerGui" testclass="GenericController"' in content
    assert '<stringProp name="IfController.condition">${ok}</stringProp>' in content
    # Saved again from what the parser reads back, it stays that way.
    assert build_jmx(parse_jmx(content)) .count('IfController.condition') == 1
