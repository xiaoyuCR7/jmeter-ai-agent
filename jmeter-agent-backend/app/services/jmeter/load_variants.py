"""The same plan at another load: its thread groups scaled to a new total, nothing else changed.

Verification runs and capacity searches run a saved plan with more or fewer users. The plan
file itself is never edited; the scaled copy is what the run snapshots and JMeter reads.
Groups keep their shares, so a stepped or spike profile keeps its shape.
"""
import xml.etree.ElementTree as ET
from app.core.text import tr

MAX_THREADS = 100_000


def _threads_prop(group):
    return next((child for child in group if child.get('name') == 'ThreadGroup.num_threads'), None)


def load_groups(root) -> list:
    """The enabled standard thread groups, which carry the load; setUp and tearDown groups do not."""
    return [group for group in root.iter('ThreadGroup') if group.get('enabled') != 'false']


def total_threads(content: bytes) -> int:
    root = ET.fromstring(content)
    total = 0
    for group in load_groups(root):
        prop = _threads_prop(group)
        text = (prop.text or '').strip() if prop is not None else ''
        total += int(text) if text.isdigit() else 0
    return total


def _prop(group, name: str) -> str:
    prop = next((child for child in group if child.get('name') == name), None)
    return (prop.text or '').strip() if prop is not None else ''


def planned_seconds(content: bytes):
    """How long the run should take, when every load group runs for a fixed time; None when it ends by loop count.

    A scheduled group runs for its startup delay plus its duration (the ramp-up is part of the duration).
    """
    try:
        groups = load_groups(ET.fromstring(content))
    except ET.ParseError:
        return None
    longest = 0
    for group in groups:
        duration, delay = _prop(group, 'ThreadGroup.duration'), _prop(group, 'ThreadGroup.delay') or '0'
        if _prop(group, 'ThreadGroup.scheduler').lower() != 'true' or not duration.isdigit() or not delay.isdigit():
            return None
        longest = max(longest, int(delay) + int(duration))
    return longest or None


def scale_threads(content: bytes, total: int) -> bytes:
    """``content`` with its load groups' thread counts scaled to add up to ``total``.

    Refuses plans whose thread counts are variables or functions: there is no number to scale.
    """
    if not isinstance(total, int) or not 1 <= total <= MAX_THREADS:
        raise ValueError(tr(f'线程数必须是 1 到 {MAX_THREADS} 之间的整数',
                            f'The thread count must be a whole number from 1 to {MAX_THREADS}'))
    root = ET.fromstring(content)
    groups = load_groups(root)
    if not groups:
        raise ValueError(tr('计划里没有启用的标准线程组（ThreadGroup），无法改变负载',
                            'The plan has no enabled standard Thread Group, so its load cannot be changed'))
    counts = []
    for group in groups:
        prop = _threads_prop(group)
        text = (prop.text or '').strip() if prop is not None else ''
        if not text.isdigit():
            raise ValueError(tr(
                f'线程组「{group.get("testname", "")}」的线程数不是固定数字（{text or "空"}），无法按比例调整',
                f'Thread group "{group.get("testname", "")}" has no fixed thread count ({text or "empty"}), '
                'so it cannot be scaled'))
        counts.append(int(text))
    current = sum(counts)
    if current <= 0:
        raise ValueError(tr('计划的线程数合计为 0，无法按比例调整',
                            "The plan's thread counts add up to 0, so there is nothing to scale"))
    active = [i for i, count in enumerate(counts) if count > 0]
    if total < len(active):
        raise ValueError(tr(f'计划有 {len(active)} 个线程组，线程数至少要 {len(active)}',
                            f'The plan has {len(active)} thread groups, so it needs at least {len(active)} threads'))
    # Largest remainder, with every group that had users keeping at least one.
    exact = {i: counts[i] * total / current for i in active}
    scaled = {i: max(1, int(exact[i])) for i in active}
    while sum(scaled.values()) > total:
        largest = max((i for i in active if scaled[i] > 1), key=lambda i: scaled[i] - exact[i])
        scaled[largest] -= 1
    for i in sorted(active, key=lambda i: exact[i] - int(exact[i]), reverse=True):
        if sum(scaled.values()) >= total:
            break
        scaled[i] += 1
    for i, group in enumerate(groups):
        if i in scaled:
            _threads_prop(group).text = str(scaled[i])
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)
