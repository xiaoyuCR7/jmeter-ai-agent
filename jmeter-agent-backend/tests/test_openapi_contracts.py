"""OpenAPI import contracts using only temporary SQLite/files and fake execution."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import xml.etree.ElementTree as ET

import pytest
import pytest_asyncio

from app.services.openapi_importer import OpenAPIImporter


def spec():
    return {'openapi': '3.0.3', 'info': {'title': 'Fixture', 'version': '1'},
        'servers': [{'url': 'https://api.invalid/v1'}],
        'security': [{'token': []}],
        'components': {'securitySchemes': {'token': {'type': 'http', 'scheme': 'bearer'}},
            'parameters': {'Id': {'in': 'path', 'name': 'id', 'required': True, 'schema': {'type': 'string', 'example': 'a b'}}},
            'requestBodies': {'Payload': {'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Payload'}}}}},
            'schemas': {'Payload': {'type': 'object', 'properties': {'name': {'type': 'string', 'example': 'Alice'}}}}},
        'paths': {'/users/{id}': {'parameters': [{'$ref': '#/components/parameters/Id'}],
            'post': {'requestBody': {'$ref': '#/components/requestBodies/Payload'},
                'parameters': [{'in': 'query', 'name': 'page', 'schema': {'type': 'integer', 'default': 2}}],
                'responses': {'201': {'description': 'Created'}}}}}}


@pytest.mark.asyncio
@pytest.mark.parametrize('location', ['http://169.254.169.254/latest', 'https://other.invalid/spec',
                                    'http://docs.invalid/spec', 'https://user:password@docs.invalid/spec'])
async def test_url_import_does_not_follow_redirect_to_new_authority(monkeypatch, location):
    import httpx
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={'location': location})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    result = await OpenAPIImporter().import_from_url('https://docs.invalid/start')
    assert not result.success
    assert len(calls) == 1
    assert 'password' not in result.error


@pytest.mark.asyncio
async def test_url_import_same_origin_redirect_uses_final_base_url(monkeypatch):
    import httpx
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path == '/start':
            return httpx.Response(302, headers={'location': '/v2/spec.json'})
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(spec()).encode()))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    importer = OpenAPIImporter()
    importer.import_from_dict = AsyncMock(return_value='imported')
    assert await importer.import_from_url('https://docs.invalid/start') == 'imported'
    assert importer.import_from_dict.call_args.args[1] == 'https://docs.invalid/v2/spec.json'
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_url_import_caps_stream_before_parsing_and_closes(monkeypatch):
    import httpx
    import app.services.openapi_importer as module
    class Body(httpx.AsyncByteStream):
        consumed = 0
        closed = False
        async def __aiter__(self):
            for _ in range(20):
                self.consumed += 1
                yield b'x' * 65536
        async def aclose(self):
            self.closed = True
    body = Body()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body)), **kwargs))
    monkeypatch.setattr(module, 'MAX_SPEC_BYTES', 65536, raising=False)
    importer = OpenAPIImporter()
    importer.import_from_dict = AsyncMock()
    result = await importer.import_from_url('https://docs.invalid/spec')
    assert not result.success and 'size' in result.error.lower()
    assert body.consumed <= 2 and body.closed
    importer.import_from_dict.assert_not_awaited()


@pytest.mark.asyncio
async def test_url_import_total_deadline_cancels_stalled_stream(monkeypatch):
    import asyncio
    import httpx
    import app.services.openapi_importer as module
    class StalledBody(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b'never'
        async def aclose(self):
            self.closed = True
    body = StalledBody()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body)), **kwargs))
    monkeypatch.setattr(module, 'SPEC_FETCH_TIMEOUT_SECONDS', .01)
    result = await asyncio.wait_for(OpenAPIImporter().import_from_url('https://docs.invalid/spec?token=secret'), .5)
    assert not result.success and 'TimeoutError' in result.error
    assert body.closed and 'secret' not in result.error


def spec_transport(monkeypatch, payload, encoding='', headers=None):
    """Keep raw wire bytes lazy, as in a real streaming HTTP response."""
    import httpx
    requests = []
    class Body(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            for offset in range(0, len(payload), 7):
                yield payload[offset:offset + 7]
        async def aclose(self):
            self.closed = True
    body = Body()
    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=body, headers={'content-encoding': encoding, **(headers or {})})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    return body, requests


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['identity', 'gzip', 'deflate', 'raw-deflate', 'gzip-members'])
async def test_spec_bounded_decoding_preserves_negotiated_encodings(monkeypatch, kind):
    import gzip
    import zlib
    raw = json.dumps(spec()).encode()
    encoding = kind
    if kind == 'identity':
        payload = raw
    elif kind == 'gzip':
        payload = gzip.compress(raw)
    elif kind == 'deflate':
        payload = zlib.compress(raw)
    elif kind == 'raw-deflate':
        encoding, payload = 'deflate', zlib.compress(raw, wbits=-zlib.MAX_WBITS)
    else:
        encoding, payload = 'gzip', gzip.compress(raw[:50]) + gzip.compress(raw[50:])
    body, requests = spec_transport(monkeypatch, payload, encoding)
    importer = OpenAPIImporter()
    importer.import_from_dict = AsyncMock(return_value='imported')
    assert await importer.import_from_url('https://docs.invalid/spec') == 'imported'
    assert importer.import_from_dict.call_args.args[0] == spec()
    assert body.closed
    assert set(requests[0].headers['accept-encoding'].replace(' ', '').split(',')) <= {'gzip', 'deflate', 'identity'}


@pytest.mark.asyncio
async def test_compressed_spec_caps_each_decompression_allocation(monkeypatch):
    import gzip
    import zlib
    import app.services.openapi_importer as module
    payload = gzip.compress(b'a' * 1_000_000)
    body, _ = spec_transport(monkeypatch, payload, 'gzip')
    monkeypatch.setattr(module, 'MAX_SPEC_BYTES', 2048)
    original = zlib.decompressobj
    calls = []
    class BoundedSpy:
        def __init__(self, *args, **kwargs):
            self.inner = original(*args, **kwargs)
        def __getattr__(self, name):
            return getattr(self.inner, name)
        def decompress(self, data, max_length=0):
            output = self.inner.decompress(data, max_length)
            calls.append((max_length, len(output)))
            return output
    monkeypatch.setattr(zlib, 'decompressobj', BoundedSpy)
    importer = OpenAPIImporter()
    importer.import_from_dict = AsyncMock()
    result = await importer.import_from_url('https://docs.invalid/spec')
    assert not result.success and 'size' in result.error.lower() and body.closed
    assert calls and all(0 < cap <= 2049 and size <= cap for cap, size in calls)
    importer.import_from_dict.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['truncated', 'checksum', 'unsupported', 'stacked', 'wire_limit'])
async def test_bad_compressed_spec_fails_before_import(monkeypatch, kind):
    import gzip
    import app.services.openapi_importer as module
    encoding = 'gzip'
    payload = gzip.compress(json.dumps(spec()).encode())
    if kind == 'truncated':
        payload = payload[:-8]
    elif kind == 'checksum':
        payload = payload[:-8] + b'\x00' * 8
    elif kind == 'unsupported':
        encoding, payload = 'unknown', json.dumps(spec()).encode()
    elif kind == 'stacked':
        encoding = 'gzip, gzip'
        payload = gzip.compress(payload)
    else:
        # Decoded JSON fits, but compressed padding exceeds the wire limit.
        monkeypatch.setattr(module, 'MAX_SPEC_BYTES', 2048)
        payload += b'\x00' * 4096
    body, _ = spec_transport(monkeypatch, payload, encoding)
    importer = OpenAPIImporter()
    importer.import_from_dict = AsyncMock(return_value='unexpected import')
    result = await importer.import_from_url('https://docs.invalid/spec')
    assert result != 'unexpected import' and not result.success and body.closed
    importer.import_from_dict.assert_not_awaited()


@pytest.mark.asyncio
async def test_compressed_spec_accepts_exact_decoded_limit(monkeypatch):
    import gzip
    import app.services.openapi_importer as module
    raw = json.dumps(spec()).encode()
    monkeypatch.setattr(module, 'MAX_SPEC_BYTES', len(raw))
    spec_transport(monkeypatch, gzip.compress(raw), 'gzip')
    importer = OpenAPIImporter()
    importer.import_from_dict = AsyncMock(return_value='imported')
    assert await importer.import_from_url('https://docs.invalid/spec') == 'imported'


@pytest.mark.asyncio
async def test_many_empty_gzip_members_are_bounded_before_import(monkeypatch):
    import gzip
    import app.services.openapi_importer as module
    monkeypatch.setattr(module, 'MAX_SPEC_GZIP_MEMBERS', 2, raising=False)
    payload = gzip.compress(b'') * 2 + gzip.compress(json.dumps(spec()).encode())
    spec_transport(monkeypatch, payload, 'gzip')
    importer = OpenAPIImporter()
    importer.import_from_dict = AsyncMock(return_value='unexpected import')
    result = await importer.import_from_url('https://docs.invalid/spec')
    assert result != 'unexpected import' and not result.success and 'member' in result.error.lower()
    importer.import_from_dict.assert_not_awaited()


@pytest_asyncio.fixture
async def imported(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models import db
    from app.core.config import settings
    from app.services.storage import store
    import app.services.execution_lifecycle as lifecycle
    import subprocess
    def forbidden(*args, **kwargs):
        raise AssertionError('Real subprocesses are forbidden in import contract tests')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(lifecycle, 'get_executor', forbidden)
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'state.db'))
    monkeypatch.setattr(db, '_engine', engine)
    monkeypatch.setattr(db, '_async_session_factory', async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(settings, 'TEST_PLANS_DIR_DEFAULT', str(tmp_path / 'plans'))
    monkeypatch.setattr(settings, 'REPORTS_DIR_DEFAULT', str(tmp_path / 'reports'))
    monkeypatch.setattr('app.core.config.load_paths_config', lambda: {})
    for key in ('_projects', '_test_plans', '_executions'):
        monkeypatch.setattr(store, key, {})
    await db.init_db()
    execute = AsyncMock(return_value=SimpleNamespace(id='fake-run', status=SimpleNamespace(value='completed'), error_message=None))
    monkeypatch.setattr(lifecycle.execution_lifecycle, 'execute', execute)
    yield SimpleNamespace(execute=execute, store=store)
    await engine.dispose()


def test_standard_http_bearer_and_local_refs_are_parsed():
    parsed = OpenAPIImporter()._parse_spec(spec(), '')
    assert parsed.auth_type == 'bearer'
    assert parsed.endpoints[0].parameters[0]['name'] == 'id'
    assert parsed.endpoints[0].security == [{'token': []}]


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('info', []), ('paths', []), ('components', None), ('servers', [False]),
    ('servers', [{'url': 'http://fixture.invalid', 'variables': None}]),
    ('components', {'securitySchemes': {'bad': {'type': None}}}),
    ('paths', {'/': {'get': {'parameters': [None]}}})])
async def test_invalid_spec_sections_fail_before_any_project_creation(field, value):
    document = spec()
    document[field] = value
    result = await OpenAPIImporter().import_from_dict(document)
    assert not result.success and result.error


@pytest.mark.parametrize('kind', ['cycle', 'deep', 'large', 'expanded_refs'])
def test_spec_structure_and_reference_expansion_have_finite_budgets(monkeypatch, kind):
    import app.services.openapi_importer as module
    document = spec()
    if kind == 'cycle':
        document['info']['self'] = document['info']
    elif kind == 'deep':
        cursor = document['info']
        for i in range(70):
            cursor['child'] = {}
            cursor = cursor['child']
    elif kind == 'large':
        monkeypatch.setattr(module, 'MAX_SPEC_BYTES', 512)
        document['info']['description'] = 'x' * 513
    else:
        monkeypatch.setattr(module, 'MAX_SPEC_NODES', 1000, raising=False)
        schema = {'type': 'string'}
        document['components']['schemas']['level0'] = schema
        for level in range(1, 9):
            document['components']['schemas'][f'level{level}'] = {
                'left': {'$ref': f'#/components/schemas/level{level-1}'},
                'right': {'$ref': f'#/components/schemas/level{level-1}'}}
        document['paths']['/users/{id}']['post']['requestBody'] = {'$ref': '#/components/schemas/level8'}
    with pytest.raises(ValueError, match='budget|limit|cyclic'):
        OpenAPIImporter()._parse_spec(document, '')


def test_swagger_host_basepath_and_scheme_are_preserved():
    parsed = OpenAPIImporter()._parse_spec({'swagger': '2.0', 'host': '[::1]:8080',
        'basePath': '/v2', 'schemes': ['http'], 'paths': {'/users': {'get': {}}}}, 'https://docs.invalid/spec.json')
    assert parsed.base_url == 'http://[::1]:8080/v2'
    assert len(parsed.endpoints) == 1


@pytest.mark.asyncio
async def test_import_preserves_request_and_registers_plan_and_project(imported):
    from app.services.db_store import db_store
    result = await OpenAPIImporter().import_from_dict(spec())
    assert result.success, result.error
    assert await db_store.get_project(result.project_id)
    assert len(imported.store.test_plans) == 1
    root = ET.parse(result.jmx_paths[0]).getroot()
    sampler = root.find('.//HTTPSamplerProxy')
    assert sampler.find("stringProp[@name='HTTPSampler.path']").text == '/v1/users/a%20b?page=2'
    assert sampler.find(".//stringProp[@name='Argument.value']").text == '{"name": "Alice"}'
    assert root.find(".//collectionProp[@name='Asserion.test_strings']/stringProp").text == '201'
    assert 'Bearer ${auth_token}' in ET.tostring(root, encoding='unicode')
    assert any('auth_token' in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_generation_failure_is_not_success(imported, monkeypatch):
    from app.services.jmeter.jmx_generator import JMXGenerator
    def fail(*args, **kwargs):
        raise ValueError('fixture generation failure')
    monkeypatch.setattr(JMXGenerator, 'generate_basic_jmx', fail)
    result = await OpenAPIImporter().import_from_dict(spec())
    assert not result.success
    assert 'fixture generation failure' in result.error
    assert not result.jmx_paths


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_point', ['save', 'register'])
async def test_incomplete_baseline_import_rolls_back_new_project_and_file(imported, monkeypatch,
                                                                          failure_point):
    from pathlib import Path
    from app.core.config import settings
    from app.services.db_store import db_store
    from app.services.jmeter.jmx_generator import JMXGenerator
    import app.services.execution_lifecycle as lifecycle

    if failure_point == 'save':
        monkeypatch.setattr(JMXGenerator, 'save_jmx', lambda *args, **kwargs:
                            (_ for _ in ()).throw(OSError('fixture save failure')))
    else:
        monkeypatch.setattr(lifecycle.execution_lifecycle, 'register_path',
                            AsyncMock(side_effect=RuntimeError('fixture registration failure')))

    result = await OpenAPIImporter().import_from_dict(spec())

    assert not result.success
    assert await db_store.get_project(result.project_id) is None
    assert imported.store.get_project(result.project_id) is None
    assert not list((Path(settings.TEST_PLANS_DIR) / result.project_id).glob('*.jmx'))


@pytest.mark.asyncio
async def test_execute_flag_uses_registered_plan_and_failed_execution_fails(imported):
    document = spec()
    document['security'] = []
    result = await OpenAPIImporter().import_from_dict(document, execute_tests=True)
    assert result.success, result.error
    assert result.execution_ids == ['fake-run']
    imported.execute.assert_awaited_once()
    assert imported.execute.call_args.kwargs['test_plan_id'] in imported.store.test_plans
    imported.execute.return_value.status.value = 'failed'
    imported.execute.return_value.error_message = 'fixture failure'
    result = await OpenAPIImporter().import_from_dict(document, execute_tests=True)
    assert not result.success and 'fixture failure' in result.error


@pytest.mark.asyncio
async def test_external_or_cyclic_refs_are_actionable_failure(imported):
    document = spec()
    document['paths']['/users/{id}']['parameters'][0]['$ref'] = 'https://outside.invalid/params.json'
    result = await OpenAPIImporter().import_from_dict(document)
    assert not result.success and '$ref' in result.error
    imported.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_api_forwards_execute_flag(monkeypatch):
    from app.api import routes
    from app.services.openapi_importer import ImportResult
    fake = AsyncMock(return_value=ImportResult(success=False, error='fixture'))
    monkeypatch.setattr(OpenAPIImporter, 'import_from_dict', fake)
    await routes.openapi_import({'spec': spec(), 'execute': True})
    assert fake.call_args.kwargs['execute_tests'] is True


@pytest.mark.asyncio
async def test_inline_api_project_rename_keeps_cache_version_in_sync(imported):
    from app.api import routes
    from app.services.db_store import db_store
    from app.services.execution_lifecycle import persist_project

    result = await routes.openapi_import({
        'spec': spec(), 'execute': False, 'project_name': 'Renamed fixture',
    })
    project_id = result['project_id']
    persisted = await db_store.get_project(project_id)
    cached = imported.store.get_project(project_id)

    assert cached.name == persisted['name'] == 'Renamed fixture'
    assert cached.storage_version == persisted['storage_version']
    cached.description = 'next valid update'
    await persist_project(cached, create=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['false', 'true', 0, 1, None])
async def test_openapi_execute_requires_a_json_boolean(monkeypatch, value):
    from fastapi import HTTPException
    from app.api import routes
    fake = AsyncMock()
    monkeypatch.setattr(OpenAPIImporter, 'import_from_dict', fake)
    with pytest.raises(HTTPException) as error:
        await routes.openapi_import({'spec': spec(), 'execute': value})
    assert error.value.status_code == 422
    fake.assert_not_awaited()


@pytest.mark.asyncio
async def test_bearer_without_credentials_does_not_auto_execute(imported):
    result = await OpenAPIImporter().import_from_dict(spec(), execute_tests=True)
    assert not result.success and 'auth_token' in result.error
    imported.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_cyclic_ref_and_missing_path_example_fail_without_execution(imported):
    document = spec()
    document['components']['parameters']['Id'] = {'$ref': '#/components/parameters/Id'}
    result = await OpenAPIImporter().import_from_dict(document)
    assert not result.success and 'Cyclic $ref' in result.error
    document = spec()
    del document['components']['parameters']['Id']['schema']['example']
    result = await OpenAPIImporter().import_from_dict(document)
    assert not result.success and 'required parameter id' in result.error
    imported.execute.assert_not_awaited()


def test_swagger_without_host_preserves_basepath_from_source_origin():
    parsed = OpenAPIImporter()._parse_spec({'swagger': '2.0', 'basePath': '/v2',
        'paths': {'/health': {'get': {}}}}, 'https://docs.invalid/swagger.json')
    assert parsed.base_url == 'https://docs.invalid/v2'


@pytest.mark.asyncio
async def test_operation_server_override_is_not_silently_ignored(imported):
    document = spec()
    document['paths']['/users/{id}']['post']['servers'] = [{'url': 'https://other.invalid'}]
    result = await OpenAPIImporter().import_from_dict(document)
    assert not result.success and 'server' in result.error
