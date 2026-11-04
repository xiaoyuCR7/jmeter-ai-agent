"""The same plan at another load: its thread groups scaled to a new total, nothing else changed.

Verification runs and capacity searches run a saved plan with more or fewer users. The plan
file itself is never edited; the scaled copy is what the run snapshots and JMeter reads.
Groups keep their shares, so a stepped or spike profile keeps its shape.
"""
import xml.etree.ElementTree as ET

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


def scale_threads(content: bytes, total: int) -> bytes:
    """``content`` with its load groups' thread counts scaled to add up to ``total``.

    Refuses plans whose thread counts are variables or functions: there is no number to scale.
    """
    if not isinstance(total, int) or not 1 <= total <= MAX_THREADS:
        raise ValueError(f'线程数必须是 1 到 {MAX_THREADS} 之间的整数')
    root = ET.fromstring(content)
    groups = load_groups(root)
    if not groups:
        raise ValueError('计划里没有启用的标准线程组（ThreadGroup），无法改变负载')
    counts = []
    for group in groups:
        prop = _threads_prop(group)
        text = (prop.text or '').strip() if prop is not None else ''
        if not text.isdigit():
            raise ValueError(f'线程组「{group.get("testname", "")}」的线程数不是固定数字（{text or "空"}），无法按比例调整')
        counts.append(int(text))
    current = sum(counts)
    if current <= 0:
        raise ValueError('计划的线程数合计为 0，无法按比例调整')
    active = [i for i, count in enumerate(counts) if count > 0]
    if total < len(active):
        raise ValueError(f'计划有 {len(active)} 个线程组，线程数至少要 {len(active)}')
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
