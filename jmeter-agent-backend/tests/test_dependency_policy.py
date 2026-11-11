from pathlib import Path

from packaging.requirements import Requirement


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _requirements(path: Path):
    logical_lines = []
    pending = ''
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or line.startswith('--'):
            continue
        pending += line[:-1].strip() if line.endswith('\\') else line
        if not line.endswith('\\'):
            logical_lines.append(Requirement(pending))
            pending = ''
    assert not pending, f'Unterminated requirement in {path.name}'
    return logical_lines


def _is_exact(requirement: Requirement):
    specs = list(requirement.specifier)
    return len(specs) == 1 and specs[0].operator == '==' and '*' not in specs[0].version


def test_runtime_requirements_are_generated_from_exact_direct_dependencies():
    direct_path = BACKEND_ROOT / 'requirements.in'
    lock_path = BACKEND_ROOT / 'requirements.txt'

    assert direct_path.is_file(), 'requirements.in must own direct runtime dependencies'
    direct = _requirements(direct_path)
    locked = _requirements(lock_path)

    assert direct and all(_is_exact(requirement) for requirement in direct)
    assert locked and all(_is_exact(requirement) for requirement in locked)

    locked_versions = {requirement.name.lower(): str(requirement.specifier) for requirement in locked}
    for requirement in direct:
        assert locked_versions.get(requirement.name.lower()) == str(requirement.specifier)


def test_runtime_lock_records_reproducible_generation_command():
    header = (BACKEND_ROOT / 'requirements.txt').read_text(encoding='utf-8')[:1000]
    assert 'uv pip compile --universal' in header
    assert 'requirements.in' in header


def test_development_requirements_are_also_a_generated_exact_lock():
    lock_path = BACKEND_ROOT / 'requirements-dev.txt'
    locked = _requirements(lock_path)
    runtime = _requirements(BACKEND_ROOT / 'requirements.txt')
    header = lock_path.read_text(encoding='utf-8')[:1000]

    assert locked and all(_is_exact(requirement) for requirement in locked)
    assert 'uv pip compile --universal' in header
    assert 'requirements-dev.in' in header
    development_versions = {requirement.name.lower(): str(requirement.specifier)
                            for requirement in locked}
    for requirement in runtime:
        assert development_versions.get(requirement.name.lower()) == str(requirement.specifier)


def test_runtime_security_migrations_are_explicit_and_remove_unsafe_jose_stack():
    direct = {requirement.name.lower(): requirement
              for requirement in _requirements(BACKEND_ROOT / 'requirements.in')}
    locked = {requirement.name.lower(): requirement
              for requirement in _requirements(BACKEND_ROOT / 'requirements.txt')}

    assert str(direct['fastapi'].specifier) == '==0.136.3'
    assert str(direct['starlette'].specifier) == '==1.3.1'
    # Left from the multi-user version; nothing imports them in the no-login local mode.
    for unused in ('pyjwt', 'passlib', 'jinja2', 'xmltodict', 'aiofiles', 'python-dateutil', 'lxml', 'websockets'):
        assert unused not in direct
    for unused in ('pyjwt', 'passlib', 'bcrypt', 'xmltodict', 'aiofiles', 'python-dateutil', 'lxml'):
        assert unused not in locked
    assert 'python-jose' not in direct
    assert 'python-jose' not in locked
    assert 'ecdsa' not in locked


def test_fastapi_uses_the_supported_native_router_without_global_monkeypatch():
    main_source = (BACKEND_ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
    assert 'fastapi_patch' not in main_source
    assert 'patch_fastapi' not in main_source
