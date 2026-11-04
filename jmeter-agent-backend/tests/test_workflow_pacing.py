"""Plans from a document must be able to meet the load the user asked for, and every requirement must name a request."""
import json

from app.services import requirements as req
from app.services.ai.agents import workflow_agent
from app.services.openapi_importer import endpoints_from_document

LOAD = {'concurrency': 30, 'ramp_up_seconds': 10, 'duration_seconds': 60, 'target_tps': 50.0}


def test_a_rate_limit_at_the_required_tps_is_raised_above_it():
    plan, notes, refusal = workflow_agent.paced({'concurrent_users': 30, 'target_rps': 50}, LOAD)
    assert refusal is None and plan['target_rps'] == 60 and 'raised to 60' in notes[0]
    assert workflow_agent.paced({'concurrent_users': 30, 'target_rps': 80}, LOAD)[0]['target_rps'] == 80


def test_think_time_is_left_out_when_the_user_asked_for_a_rate():
    """The baseline type's default user pace (1 s) capped 30 users near 27 TPS against a 50 TPS requirement."""
    assert workflow_agent.paced({'concurrent_users': 30}, LOAD)[0]['think_time_ms'] == 0
    assert 'think_time_ms' not in workflow_agent.paced({'concurrent_users': 30}, {'concurrency': 30})[0]


def test_a_think_time_that_cannot_reach_the_required_tps_is_refused_for_the_model_to_fix():
    _, _, refusal = workflow_agent.paced({'concurrent_users': 30, 'think_time_ms': 1000}, LOAD)
    assert '30 requests/s' in refusal and 'think_time_ms' in refusal
    assert workflow_agent.paced({'concurrent_users': 30, 'think_time_ms': 200}, LOAD)[2] is None


def test_requirements_naming_a_request_no_plan_has_are_reported():
    requirements = req.PerformanceRequirements(items=[
        req.RequirementItem(id='R1', scope='each', metric='p95', operator='lt', value=500),
        req.RequirementItem(id='R2', scope='transaction', transaction='下单接口', metric='p95', operator='lt', value=800),
        req.RequirementItem(id='R3', scope='transaction', transaction='POST /api/orders', metric='p95', operator='lt', value=800),
        req.RequirementItem(id='R4', scope='transaction', transaction='GET /api/products/{id}', metric='p95', operator='lt', value=300)])
    plans = [{'requests': ['POST /api/orders', 'GET /api/products/3'],
              'endpoints': [{'name': 'Place an order', 'method': 'POST', 'path': '/api/orders'},
                            {'name': 'Get one product', 'method': 'GET', 'path': '/api/products/3'}]}]
    assert workflow_agent.unmatched_requirements(requirements, plans) == ['R2「下单接口」']


def test_documented_responses_reach_the_model_so_it_extracts_real_fields():
    spec = {'openapi': '3.0.3', 'info': {'title': 'Shop', 'version': '1'}, 'servers': [{'url': 'http://shop.test'}],
            'components': {'schemas': {'Token': {'type': 'object', 'properties': {'token': {'type': 'string'}}}}},
            'paths': {'/login': {'post': {'summary': 'Log in', 'responses': {'200': {'description': 'ok', 'content': {
                'application/json': {'schema': {'$ref': '#/components/schemas/Token'}}}}}}},
                '/items': {'get': {'summary': 'Items', 'responses': {'200': {'description': 'ok', 'content': {
                    'application/json': {'example': {'items': [{'id': 1}], 'total': 1}}}}}}}}}
    endpoints = {item['path']: item for item in endpoints_from_document(json.dumps(spec))['endpoints']}
    assert endpoints['/login']['response'] == {'status': 200, 'json': {'token': 'string'}}
    assert endpoints['/items']['response']['json'] == {'items': [{'id': 1}], 'total': 1}
