"""The project's active environment, applied to the copy of a plan a run executes.

The plan file itself never changes. In the run's copy:

- requests aimed at the host of any environment of the project (or, when the plan talks to a
  single host, at that host) go to the active environment's base URL instead;
- the environment's headers are sent with every request, replacing a plan header of the same name;
- the environment's variables are defined last, so they win over the plan's own.

What the environment added carries a ``[环境`` name, so applying again (a rerun of a snapshot)
replaces it. Secret values sit in their own ``[环境·敏感]`` elements and are masked in the
copy once the run is over.
"""
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import urlsplit

from app.models.schemas import SECRET_MASK

MARK = '[环境'
SECRET_MARK = '[环境·敏感]'
_DEFAULT_PORTS = {'http': '80', 'https': '443'}


class ProductionRunNeedsConfirmation(ValueError):
    """A run against an environment marked production waits for an explicit yes."""

    def __init__(self, environment: str, base_url: str, threads: int):
        self.environment, self.base_url, self.threads = environment, base_url, threads
        super().__init__(f'环境「{environment}」标记为生产环境（{base_url or "未设地址"}），'
                         f'这次运行共 {threads} 个线程，需要确认后才会开始。')


def _origin(base_url: str) -> Optional[dict]:
    if not base_url:
        return None
    parts = urlsplit(base_url if '://' in base_url else 'https://' + base_url)
    if not parts.hostname:
        return None
    scheme = parts.scheme or 'https'
    port = str(parts.port or '')
    return {'protocol': scheme, 'domain': parts.hostname.lower(),
            'port': '' if port == _DEFAULT_PORTS.get(scheme) else port, 'path': parts.path.rstrip('/')}


def _prop(element, name):
    return next((child for child in element if child.get('name') == name), None)


def _text(element, name) -> str:
    prop = _prop(element, name)
    return (prop.text or '').strip() if prop is not None else ''


def _set(element, name, value):
    prop = _prop(element, name)
    if prop is None:
        prop = ET.SubElement(element, 'stringProp', name=name)
    prop.text = value


def _string(parent, name, value):
    ET.SubElement(parent, 'stringProp', name=name).text = value


def _headers_element(name, headers: dict):
    element = ET.Element('HeaderManager', guiclass='HeaderPanel', testclass='HeaderManager',
                         testname=name, enabled='true')
    collection = ET.SubElement(element, 'collectionProp', name='HeaderManager.headers')
    for key, value in headers.items():
        entry = ET.SubElement(collection, 'elementProp', name='', elementType='Header')
        _string(entry, 'Header.name', key)
        _string(entry, 'Header.value', value)
    return element


def _variables_element(name, variables: dict):
    element = ET.Element('Arguments', guiclass='ArgumentsPanel', testclass='Arguments',
                         testname=name, enabled='true')
    collection = ET.SubElement(element, 'collectionProp', name='Arguments.arguments')
    for key, value in variables.items():
        entry = ET.SubElement(collection, 'elementProp', name=key, elementType='Argument')
        _string(entry, 'Argument.name', key)
        _string(entry, 'Argument.value', value)
        _string(entry, 'Argument.metadata', '=')
    return element


def _top_tree(root):
    """The hashTree holding the test plan's children."""
    outer = root.find('hashTree')
    if outer is None:
        raise ValueError('JMX has no test plan')
    children = list(outer)
    for index, child in enumerate(children):
        if child.tag == 'TestPlan' and index + 1 < len(children) and children[index + 1].tag == 'hashTree':
            return children[index + 1]
    raise ValueError('JMX has no test plan')


def _remove_marked(tree):
    """Elements an earlier application added, each with the hashTree that follows it."""
    children = list(tree)
    for index, child in enumerate(children):
        if (child.get('testname') or '').startswith(MARK):
            tree.remove(child)
            if index + 1 < len(children) and children[index + 1].tag == 'hashTree':
                tree.remove(children[index + 1])


def _retarget_hosts(root, target: dict, known: list[dict]) -> tuple[list[str], Optional[str]]:
    http = [element for element in root.iter() if _prop(element, 'HTTPSampler.domain') is not None]
    hosts = {}
    for element in http:
        domain = _text(element, 'HTTPSampler.domain').lower()
        if domain and '${' not in domain:
            hosts.setdefault(domain, []).append(element)
    matched = {domain: next(origin for origin in known if origin['domain'] == domain)
               for domain in hosts if any(origin['domain'] == domain for origin in known)}
    if not matched and len(hosts) == 1:
        matched = {next(iter(hosts)): None}  # One host only: that is the system under test.
    if not matched:
        return [], ('计划访问多个主机，没有一个是项目环境里的地址，请求地址保持不变' if hosts else
                    '计划里没有写死主机，请求地址保持不变')
    for domain, origin in matched.items():
        old_path = origin['path'] if origin else ''
        for element in hosts[domain]:
            _set(element, 'HTTPSampler.protocol', target['protocol'])
            _set(element, 'HTTPSampler.domain', target['domain'])
            _set(element, 'HTTPSampler.port', target['port'])
        if old_path and old_path != target['path']:
            # Requests that inherit this host from the defaults keep their own path; swap its prefix.
            for element in http:
                if _text(element, 'HTTPSampler.domain').lower() in ('', target['domain']):
                    path = _text(element, 'HTTPSampler.path')
                    if path == old_path or path.startswith(old_path + '/'):
                        _set(element, 'HTTPSampler.path', target['path'] + path[len(old_path):])
    return sorted(matched), None


def _override_plan_headers(root, names: set[str]) -> int:
    removed = 0
    for manager in root.iter('HeaderManager'):
        if (manager.get('testname') or '').startswith(MARK):
            continue
        for collection in manager.iter('collectionProp'):
            for entry in list(collection):
                if _text(entry, 'Header.name').lower() in names:
                    collection.remove(entry)
                    removed += 1
    return removed


def apply(content: bytes, environment: dict, other_base_urls: list[str]) -> tuple[bytes, dict]:
    """The run's copy of the plan under ``environment`` (with real secret values), and what changed."""
    secret = set(environment.get('secret_keys') or [])
    headers, variables = environment.get('headers') or {}, environment.get('variables') or {}
    target = _origin(environment.get('base_url') or '')
    applied = {'base_url': environment.get('base_url') or '', 'hosts_replaced': [], 'note': None,
               'headers': sorted(headers), 'variables': sorted(variables), 'plan_headers_overridden': 0}
    if target is None and not headers and not variables and MARK.encode() not in content:
        return content, applied  # Nothing to apply: the plan runs byte for byte.
    try:
        root = ET.fromstring(content)
        tree = _top_tree(root)
    except (ET.ParseError, ValueError):
        # JMeter will say what is wrong with it; the environment cannot be placed in it.
        applied['note'] = '计划结构无法解析，环境的地址、请求头和变量都没有应用'
        return content, applied
    _remove_marked(tree)
    if target is not None:
        known = [origin for origin in map(_origin, [environment.get('base_url') or '', *other_base_urls]) if origin]
        applied['hosts_replaced'], applied['note'] = _retarget_hosts(root, target, known)
    if headers:
        applied['plan_headers_overridden'] = _override_plan_headers(root, {name.lower() for name in headers})
    elements = []
    for name, items in (('[环境] 请求头', {k: v for k, v in headers.items() if k not in secret}),
                        (f'{SECRET_MARK} 请求头', {k: v for k, v in headers.items() if k in secret})):
        if items:
            elements.append((0, _headers_element(name, items)))
    for name, items in (('[环境] 变量', {k: v for k, v in variables.items() if k not in secret}),
                        (f'{SECRET_MARK} 变量', {k: v for k, v in variables.items() if k in secret})):
        if items:
            elements.append((None, _variables_element(name, items)))  # Last: defined after the plan's own.
    for position, element in elements:
        index = 0 if position == 0 else len(tree)
        tree.insert(index, ET.Element('hashTree'))
        tree.insert(index, element)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True), applied


def scrub(path) -> bool:
    """Mask the secret values a finished run's copy of the plan carried. True when it changed the file."""
    try:
        with open(path, 'rb') as source:
            content = source.read()
    except OSError:
        return False
    if SECRET_MARK.encode() not in content:
        return False
    root = ET.fromstring(content)
    for element in root.iter():
        if (element.get('testname') or '').startswith(SECRET_MARK):
            for prop in element.iter('stringProp'):
                if prop.get('name') in ('Header.value', 'Argument.value'):
                    prop.text = SECRET_MASK
    with open(path, 'wb') as target:
        target.write(ET.tostring(root, encoding='utf-8', xml_declaration=True))
    return True
