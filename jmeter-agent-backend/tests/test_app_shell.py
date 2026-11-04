"""The app shell: files of the built UI, and services' refusals as HTTP responses."""
import httpx
import pytest


@pytest.fixture
def client(monkeypatch, tmp_path):
    from app import main
    (tmp_path / 'index.html').write_text('<html>app</html>', encoding='utf-8')
    (tmp_path / 'logo.svg').write_text('<svg/>', encoding='utf-8')
    monkeypatch.setattr(main, 'frontend_dist', str(tmp_path))
    # The access token guard is tested elsewhere; here only routing matters.
    app = main.FastAPI()
    app.add_exception_handler(main.ServiceError, main.service_error)
    app.add_api_route('/{path:path}', main.catch_all)

    @app.get('/api/v1/refused')
    async def refused():
        raise main.ServiceError(409, 'Business record changed; refresh and retry')
    app.router.routes.insert(0, app.router.routes.pop())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost')


@pytest.mark.asyncio
async def test_a_file_of_the_built_ui_is_served_as_itself_and_page_routes_get_the_app(client):
    async with client:
        logo = await client.get('/logo.svg')
        page = await client.get('/executions/run-1')
        outside = await client.get('/..%2Fsecret.txt')
        api = await client.get('/api/v1/missing')
    assert logo.status_code == 200 and logo.text == '<svg/>' and 'svg' in logo.headers['content-type']
    assert page.text == '<html>app</html>'
    assert outside.text == '<html>app</html>'
    assert api.status_code == 404


@pytest.mark.asyncio
async def test_a_service_refusal_keeps_its_status_and_message(client):
    async with client:
        response = await client.get('/api/v1/refused')
    assert response.status_code == 409
    assert response.json() == {'detail': 'Business record changed; refresh and retry'}


def test_services_do_not_depend_on_the_web_layer():
    import pathlib
    services = pathlib.Path(__file__).parent.parent / 'app' / 'services'
    offenders = [str(path) for path in services.rglob('*.py')
                 if 'from app.api' in (text := path.read_text(encoding='utf-8')) or 'HTTPException' in text]
    assert offenders == []


def test_a_wildcard_cors_origin_stops_startup(monkeypatch):
    from app.core import config
    monkeypatch.setattr(config.settings, 'CORS_ORIGINS', ['*'])
    with pytest.raises(RuntimeError, match='CORS_ORIGINS'):
        config.validate_deployment_security()
