"""Find the parts of a JMX that run arbitrary code on the machine executing JMeter.

In local execution mode JMeter has the user's own privileges, so a plan from
elsewhere can read files or run commands through these. The app's own generator
never emits them; the scan lets the user approve such a plan knowingly.
"""
import re
import xml.etree.ElementTree as ET

# Script-backed elements and the OS Process Sampler; IncludeController pulls in a
# file this scan cannot see.
_CLASS = re.compile(r'(JSR223|BeanShell|BSF)\w*|SystemSampler|IncludeController')
_FUNCTION = re.compile(r'\$\{\s*(__groovy|__BeanShell|__javaScript|__jexl2|__jexl3)\s*\(')


def scan(content: bytes) -> list[dict]:
    """``[{"name", "kind"}]``: one entry per enabled risky element, plus each scripting function used."""
    findings = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return [{'name': 'JMX could not be parsed, so it was not checked', 'kind': 'unparsed'}]
    for element in root.iter():
        kind = element.get('testclass') or element.tag
        if _CLASS.fullmatch(kind) and element.get('enabled', 'true') != 'false':
            findings.append({'name': element.get('testname') or kind, 'kind': kind})
    text = content.decode('utf-8', errors='replace')
    for function in dict.fromkeys(_FUNCTION.findall(text)):
        findings.append({'name': '${' + function + '(...)}', 'kind': function})
    return findings


def describe(findings: list[dict]) -> str:
    shown = ', '.join(f"{item['name']} ({item['kind']})" if item['name'] != item['kind'] else item['kind']
                      for item in findings[:10])
    more = f' and {len(findings) - 10} more' if len(findings) > 10 else ''
    return shown + more


class ScriptApprovalRequired(ValueError):
    """Local execution of a plan with scripts needs the user's explicit approval."""

    def __init__(self, findings: list[dict]):
        self.findings = findings
        super().__init__('This plan runs scripts or commands on this machine with your privileges: '
                         f'{describe(findings)}. Approve it explicitly, or use container execution mode.')
