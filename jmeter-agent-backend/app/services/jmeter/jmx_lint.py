"""Checks a plan must pass before anyone runs it.

Each rule here comes from a plan that looked fine and was not: it failed to load in
JMeter, failed every request, or passed requests that were wrong. ``load_check``
then asks JMeter itself, because only JMeter knows what it can load.
"""
import logging
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from typing import Optional

logger = logging.getLogger(__name__)

LOAD_CHECK_TIMEOUT_SECONDS = 90
_THREAD_GROUPS = ('ThreadGroup', 'SetupThreadGroup', 'PostThreadGroup')
_VARIABLE = re.compile(r'\$\{([A-Za-z_][\w.-]*)\}')
_PLACEHOLDER = re.compile(r'(?<!\$)\{[^{}/]+\}')
_SCRIPTED = ('JSR223', 'BeanShell', 'BSF')
_LOGIN = re.compile(r'login|signin|sign-in|token|auth', re.IGNORECASE)
_DEFINING_PROPS = {'JSONPostProcessor': 'JSONPostProcessor.referenceNames', 'RegexExtractor': 'RegexExtractor.refname',
                   'BoundaryExtractor': 'BoundaryExtractor.refname', 'XPath2Extractor': 'XPath2Extractor.refname',
                   'XPathExtractor': 'XPathExtractor.refname', 'CSVDataSet': 'variableNames',
                   'RandomVariableConfig': 'variableName', 'CounterConfig': 'CounterConfig.name'}


def _issue(severity: str, code: str, message: str) -> dict:
    return {'severity': severity, 'code': code, 'message': message}


def _prop(element, name: str) -> str:
    for child in element:
        if child.get('name') == name:
            return (child.text or '').strip()
    return ''


def _pairs(tree):
    """(element, its hashTree) for each entry of a hashTree."""
    children = list(tree)
    for index in range(0, len(children) - 1, 2):
        yield children[index], children[index + 1]


def _names_in(element) -> set[str]:
    """Variable names an element defines."""
    tag = element.tag
    if tag in _DEFINING_PROPS:
        return {name.strip() for name in re.split(r'[,;]', _prop(element, _DEFINING_PROPS[tag])) if name.strip()}
    if tag == 'Arguments' or element.get('testclass') == 'Arguments':
        return {item.get('name') for item in element.iter('elementProp') if item.get('name')}
    if tag == 'TestPlan':
        return {item.get('name') for item in element.iter('elementProp')
                if item.get('name') and item.get('elementType') == 'Argument'}
    return set()


def _used_in(element) -> set[str]:
    return {name for node in element.iter() for name in _VARIABLE.findall(node.text or '')}


def _check_pairing(tree, issues: list):
    children = list(tree)
    for first, second in zip(children[::2], children[1::2]):
        if first.tag == 'hashTree' or second.tag != 'hashTree':
            issues.append(_issue('error', 'broken_tree', f'{first.tag}「{first.get("testname", "")}」后面缺少 hashTree，'
                                                         'JMeter 无法加载这个计划。'))
            return
        _check_pairing(second, issues)
    if len(children) % 2:
        issues.append(_issue('error', 'broken_tree', f'{children[-1].tag} 后面缺少 hashTree，JMeter 无法加载这个计划。'))


def _check_assertion(element, issues: list):
    label = element.get('testname', '')
    if element.find("collectionProp[@name='Assertion.test_strings']") is not None:
        issues.append(_issue('error', 'dead_assertion', f'响应断言「{label}」用了 Assertion.test_strings。JMeter 只认 '
                                                        'Asserion.test_strings，这条断言不会检查任何内容。'))
    elif element.find("collectionProp[@name='Asserion.test_strings']/stringProp") is None:
        issues.append(_issue('error', 'dead_assertion', f'响应断言「{label}」没有期望值，不会检查任何内容。'))


def _walk(tree, known: set, state: dict, issues: list, once: bool = False):
    for element, children in _pairs(tree):
        if element.get('enabled') == 'false':
            continue
        known |= _names_in(element)
        tag = element.tag
        if any(tag.startswith(prefix) for prefix in _SCRIPTED):
            state['scripted'] = True
        if tag == 'ResponseAssertion':
            _check_assertion(element, issues)
        if tag == 'HTTPSamplerProxy':
            label = element.get('testname', '')
            path = _prop(element, 'HTTPSampler.path')
            if '$${' in path:
                issues.append(_issue('error', 'stray_dollar', f'请求「{label}」的路径 {path} 多了一个 $，实际 URL 是错的。'))
            for placeholder in _PLACEHOLDER.findall(path):
                issues.append(_issue('error', 'unresolved_path', f'请求「{label}」的路径 {path} 还有占位符 {placeholder}，'
                                                                 '需要绑定变量或给出实际值。'))
            for item in element.iter('elementProp'):
                if item.get('elementType') == 'Argument' and item.get('name') != 'HTTPsampler.Arguments':
                    issues.append(_issue('error', 'wrong_argument', f'请求「{label}」的参数要用 HTTPArgument，'
                                                                    '用 Argument 时每个请求都会失败。'))
                    break
            subtree = list(children)
            if not any(child.tag in ('ResponseAssertion', 'JSONPathAssertion') for child in subtree):
                state['unasserted'].append(label)
            if not once and _LOGIN.search(f'{label} {path}') and any(child.tag in _DEFINING_PROPS for child in subtree):
                state['repeated_login'].append(label)
            state['samplers'] += 1
            for name in sorted(_used_in(element) | {n for c in subtree if c.tag == 'HeaderManager' for n in _used_in(c)}):
                state['used'].append((label, name, set(known)))
        if tag == 'ConfigTestElement':
            for item in element.iter('elementProp'):
                if item.get('elementType') == 'Argument' and element.get('guiclass') == 'HttpDefaultsGui':
                    issues.append(_issue('error', 'wrong_argument', 'HTTP 默认值里的参数要用 HTTPArgument，'
                                                                    '用 Argument 时每个请求都会失败。'))
                    break
            if _prop(element, 'HTTPSampler.response_timeout'):
                state['timeout'] = True
        _walk(children, known, state, issues, once or tag == 'OnceOnlyController')
        # Extractors sit under their sampler, and what they define is for the requests after it.
        known |= {name for child in children for name in _names_in(child)}


def lint(content) -> list[dict]:
    """Problems with a plan. Errors mean it must not run; warnings mean it may mislead."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        return [_issue('error', 'invalid_xml', f'计划不是有效的 XML：{error}')]
    outer = root.find('hashTree')
    if root.tag != 'jmeterTestPlan' or outer is None:
        return [_issue('error', 'invalid_plan', '计划缺少 jmeterTestPlan 根结构。')]
    issues: list[dict] = []
    _check_pairing(outer, issues)
    if issues:
        return issues

    state = {'samplers': 0, 'scripted': False, 'timeout': False, 'used': [], 'unasserted': [], 'repeated_login': []}
    groups = 0
    for plan, plan_tree in _pairs(outer):
        shared = _names_in(plan)
        entries = list(_pairs(plan_tree))
        for element, _ in entries:
            shared |= _names_in(element)
        config_only = ET.Element('hashTree')
        for element, children in entries:
            if element.tag in _THREAD_GROUPS:
                if element.get('enabled') != 'false':
                    groups += 1
                    _walk(children, set(shared), state, issues)
            else:
                config_only.extend([element, children])
        _walk(config_only, set(shared), state, issues)

    if not groups:
        issues.append(_issue('error', 'no_thread_group', '计划没有启用的线程组。'))
    if groups and not state['samplers'] and 'HTTPSamplerProxy' in str(content):
        issues.append(_issue('error', 'no_sampler', '计划没有启用的请求。'))
    if not state['scripted']:
        for label, name in sorted({(label, name) for label, name, known in state['used'] if name not in known}):
            issues.append(_issue('error', 'undefined_variable', f'请求「{label}」用到 ${{{name}}}，但在它之前没有地方定义这个变量。'))
    for label in sorted(set(state['unasserted'])):
        issues.append(_issue('warning', 'no_assertion', f'请求「{label}」没有断言，失败也不会被发现。'))
    for label in sorted(set(state['repeated_login'])):
        issues.append(_issue('warning', 'repeated_login', f'请求「{label}」像登录，却每轮都执行，会把登录压力混进结果。'
                                                          '每个用户登录一次时用 phase=once_per_user。'))
    if state['samplers'] and not state['timeout']:
        issues.append(_issue('warning', 'no_timeout', '计划没有设置响应超时，服务挂住时线程会一直等待。'))
    return issues


def planned_load(content) -> list[dict]:
    """The thread groups a plan really has: the plan decides the load, whatever its config said."""
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, TypeError):
        return []
    groups = []
    for tag in _THREAD_GROUPS:
        for group in root.iter(tag):
            if group.get('enabled') == 'false':
                continue
            number = lambda name: int(_prop(group, name)) if _prop(group, name).isdigit() else 0
            groups.append({'name': group.get('testname', ''), 'kind': tag, 'threads': number('ThreadGroup.num_threads'),
                           'ramp_seconds': number('ThreadGroup.ramp_time'), 'delay_seconds': number('ThreadGroup.delay'),
                           'duration_seconds': number('ThreadGroup.duration')})
    return groups


def errors(issues: list[dict]) -> list[dict]:
    return [issue for issue in issues if issue['severity'] == 'error']


def describe(issues: list[dict]) -> str:
    return '；'.join(issue['message'] for issue in issues)


def _without_threads(content: str) -> str:
    root = ET.fromstring(content)
    for prop in root.iter('stringProp'):
        if prop.get('name') == 'ThreadGroup.num_threads':
            prop.text = '0'
    return ET.tostring(root, encoding='unicode')


def load_check(content: str) -> dict:
    """Have JMeter load the plan with no threads, so nothing is sent anywhere.

    ``checked`` is False where JMeter is not installed locally, as in container mode:
    that is not a failure of the plan.
    """
    from app.services.jmeter.executor import get_executor
    try:
        command = get_executor().jmeter_path
    except (OSError, ValueError) as error:
        return {'checked': False, 'ok': None, 'error': str(error)[:200]}
    with tempfile.TemporaryDirectory(prefix='jmx-check-') as folder:
        plan, log = os.path.join(folder, 'plan.jmx'), os.path.join(folder, 'jmeter.log')
        with open(plan, 'w', encoding='utf-8') as stream:
            stream.write(_without_threads(content))
        try:
            done = subprocess.run([command, '-n', '-t', plan, '-l', os.path.join(folder, 'out.jtl'), '-j', log],
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace',
                                  timeout=LOAD_CHECK_TIMEOUT_SECONDS,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.SubprocessError) as error:
            return {'checked': False, 'ok': None, 'error': type(error).__name__}
        text = done.stdout or ''
        if os.path.exists(log):
            with open(log, encoding='utf-8', errors='replace') as stream:
                text += stream.read()
    problem = _load_problem(text)
    return {'checked': True, 'ok': problem is None, 'error': problem}


def _load_problem(text: str) -> Optional[str]:
    for marker in ('Problem loading XML', 'Error in NonGUIDriver', 'An error occurred'):
        if marker in text:
            at = text.index(marker)
            return text[at:at + 300]
    return None
