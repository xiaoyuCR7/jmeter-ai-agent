"""Find the parts of a JMX that reach beyond load generation on the machine running JMeter.

In local execution mode JMeter has the user's own privileges. Scripts are the obvious
risk, but a plan with none can still read and write any file through JMeter's own
functions and elements, then send what it read anywhere. The app's own generator emits
none of this; the scan lets the user approve such a plan knowingly.
"""
import hashlib
import os
import re
import xml.etree.ElementTree as ET

from app.core.config import settings

# Script-backed elements, the OS Process Sampler, samplers that load arbitrary Java classes,
# and IncludeController, which pulls in a file this scan cannot see.
_CLASS = re.compile(r'(JSR223|BeanShell|BSF)\w*|SystemSampler|IncludeController|JavaSampler|JUnitSampler')
# Code, file access, and __eval/__evalVar, which run functions assembled at run time.
_FUNCTION = re.compile(r'\$\{\s*(__groovy|__BeanShell|__javaScript|__jexl2|__jexl3|__FileToString|__StringToFile'
                       r'|__StringFromFile|__CSVRead|__XPath|__eval|__evalVar)\s*\(')
# Properties naming a local file: CSV data, uploads, listener output, keystores, and so on.
_FILE_PROPERTY = re.compile(r'file|keystore', re.IGNORECASE)
_NOT_A_PATH = re.compile(r'encoding$|^FileSaver\.(?!base_filename)|^File\.(?!path)', re.IGNORECASE)
_CLASSPATH = 'TestPlan.user_define_classpath'


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _short_class(name: str) -> str:
    # JMeter also accepts fully qualified class names.
    return name.rsplit('.', 1)[-1]


def _element_kind(element) -> str | None:
    names = (element.tag, element.get('testclass'), element.get('elementType'), element.get('class'))
    return next((_short_class(name) for name in names if name and _CLASS.fullmatch(_short_class(name))), None)


def _roots() -> list[str]:
    return [os.path.realpath(root) for root in (settings.TEST_PLANS_DIR, settings.REPORTS_DIR,
                                                os.path.join(settings.BASE_DIR, 'test_data'))]


def _path_is_contained(value: str, base_dir: str | None) -> bool:
    """True when ``value`` stays in the app's own data folders; a variable cannot be checked."""
    if '$' in value:
        return False
    if not os.path.isabs(value):
        if not base_dir or '..' in re.split(r'[\\/]', value):
            return False
        value = os.path.join(base_dir, value)
    resolved = os.path.normcase(os.path.realpath(value))
    for root in _roots():
        root = os.path.normcase(root)
        try:
            if os.path.commonpath([resolved, root]) == root:
                return True
        except ValueError:
            continue  # different drives
    return False


def scan(content: bytes, base_dir: str | None = None) -> list[dict]:
    """``[{"name", "kind"}]``, one entry per risky element, file path and function used.

    Everything is read from the parsed XML: JMeter decodes entities and CDATA before it
    evaluates a value, so the raw bytes do not show what will run. ``base_dir`` is the
    plan's folder, which relative paths resolve against.
    """
    findings = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return [{'name': 'JMX could not be parsed, so it was not checked', 'kind': 'unparsed'}]
    functions = {}
    owners = {child: parent for parent in root.iter() for child in parent}
    for element in root.iter():
        kind = _element_kind(element)
        if kind and element.get('enabled', 'true') != 'false':
            findings.append({'name': element.get('testname') or kind, 'kind': kind})
        for value in (element.text or '', *element.attrib.values()):
            functions.update(dict.fromkeys(_FUNCTION.findall(value)))
        property_name, value = element.get('name', ''), (element.text or '').strip()
        if property_name == _CLASSPATH and value:
            findings.append({'name': value, 'kind': _CLASSPATH})
        elif (value and element.tag == 'stringProp' and _FILE_PROPERTY.search(property_name)
                and not _NOT_A_PATH.search(property_name) and not _path_is_contained(value, base_dir)):
            owner = element
            while owner is not None and not owner.get('testclass'):
                owner = owners.get(owner)
            findings.append({'name': value, 'kind': _short_class(owner.get('testclass')) if owner is not None
                             else property_name})
    findings.extend({'name': '${' + function + '(...)}', 'kind': function} for function in functions)
    return findings


def describe(findings: list[dict]) -> str:
    shown = ', '.join(f"{item['name']} ({item['kind']})" if item['name'] != item['kind'] else item['kind']
                      for item in findings[:10])
    more = f' and {len(findings) - 10} more' if len(findings) > 10 else ''
    return shown + more


class ScriptApprovalRequired(ValueError):
    """Local execution of such a plan needs the user's approval of these exact bytes."""

    def __init__(self, findings: list[dict], sha256: str):
        self.findings = findings
        self.sha256 = sha256
        super().__init__('This plan runs scripts or commands, or reaches files outside the app\'s folders, on this '
                         f'machine with your privileges: {describe(findings)}. Approve it explicitly, '
                         'or use container execution mode.')
