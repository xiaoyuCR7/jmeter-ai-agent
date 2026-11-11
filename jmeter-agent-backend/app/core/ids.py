"""IDs that become folder names: executions, projects, plans, capacity searches."""
import re

_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}


def validate_id(value: str) -> str:
    """IDs are portable single path components, including on Windows."""
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,49}", value)
            or value.endswith('.') or value.split('.')[0].upper() in _RESERVED):
        raise ValueError('Invalid execution/project/plan ID: expected a safe path component')
    return value
