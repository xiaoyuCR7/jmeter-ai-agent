"""Agent/MCP tools for the requirements → run → verdict loop.

They expose the same deterministic evidence the report page shows, so an
external assistant (Claude Code, Cursor, …) analyses identical numbers.
"""
from app.services.ai.agent.tool_registry import Tool
from app.services.execution_lifecycle import validate_id


def _project(project_id: str):
    from app.services.storage import store
    validate_id(project_id)
    if project_id not in store.projects:
        raise ValueError(f'Project not found: {project_id}')


async def get_performance_requirements(project_id: str) -> dict:
    from app.services.requirements import get_requirements
    _project(project_id)
    stored = await get_requirements(project_id)
    if stored is None:
        return {'project_id': project_id, 'requirements': None,
                'note': 'No requirements recorded; extract them from a test document first.'}
    return {'project_id': project_id, 'requirements': stored.model_dump()}


async def extract_performance_requirements(project_id: str, document_text: str, source_name: str = '') -> dict:
    from app.services.ai.adapters.factory import AIAdapterFactory
    from app.services.requirements import extract_requirements
    _project(project_id)
    draft = await extract_requirements(document_text, AIAdapterFactory.create(), source_name)
    return {'project_id': project_id, 'draft': draft.model_dump(), 'saved': False,
            'note': 'Draft only. Review items whose source_verified is false, then call save_performance_requirements.'}


async def save_performance_requirements(project_id: str, requirements: dict) -> dict:
    from app.services.requirements import PerformanceRequirements, save_requirements
    _project(project_id)
    saved = await save_requirements(project_id, PerformanceRequirements.model_validate(requirements))
    return {'project_id': project_id, 'saved': True, 'requirements': saved.model_dump()}


async def get_execution_evidence(execution_id: str) -> dict:
    from app.services.evidence import build_evidence, format_for_prompt
    from app.services.storage import store
    validate_id(execution_id)
    execution = store.executions.get(execution_id)
    if execution is None:
        return {'error': f'Execution not found: {execution_id}'}
    if not execution.summary or not execution.summary.total_requests:
        return {'error': 'Execution has no usable parsed report yet'}
    evidence = await build_evidence(execution, execution.summary)
    data = evidence.model_dump()
    if data.get('server_metrics'):
        data['server_metrics'].pop('chart', None)  # plot points are for the UI, not for reasoning
    return {'evidence': data, 'summary_for_analysis': format_for_prompt(evidence)}


async def promote_execution_to_baseline(execution_id: str) -> dict:
    from app.api.baseline_routes import promote_execution_to_baseline as promote
    validate_id(execution_id)
    return await promote(execution_id)


# Spelled out so the model uses the real field names; with a bare "object" it once sent
# load_profile.concurrent_users, which was silently dropped.
_REQUIREMENTS_SCHEMA = {
    'type': 'object',
    'description': 'PerformanceRequirements object as returned by extract/get',
    'properties': {
        'items': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'id': {'type': 'string', 'description': 'R1, R2, ...'},
            'scope': {'type': 'string', 'enum': ['global', 'transaction']},
            'transaction': {'type': ['string', 'null'],
                            'description': 'For scope=transaction: endpoint such as "POST /api/order", or sampler label'},
            'metric': {'type': 'string', 'enum': ['avg_response_time', 'p90', 'p95', 'p99', 'max_response_time',
                                                  'error_rate', 'throughput']},
            'operator': {'type': 'string', 'enum': ['lte', 'lt', 'gte', 'gt']},
            'value': {'type': 'number', 'description': 'ms for times, % for error_rate, TPS for throughput'},
            'source': {'type': 'string', 'description': 'Exact clause quoted from the document'},
            'source_verified': {'type': 'boolean'},
        }, 'required': ['id', 'scope', 'metric', 'operator', 'value']}},
        'load_profile': {'type': 'object', 'properties': {
            'concurrency': {'type': ['integer', 'null'], 'description': 'Concurrent users'},
            'ramp_up_seconds': {'type': ['integer', 'null']},
            'duration_seconds': {'type': ['integer', 'null']},
            'target_tps': {'type': ['number', 'null']},
        }},
        'assumptions': {'type': 'array', 'items': {'type': 'string'}},
        'source_name': {'type': 'string'},
    },
    'required': ['items'],
}

evidence_tools = [
    Tool(
        name='get_performance_requirements',
        description='Get the structured performance requirements (thresholds per metric/transaction and load profile) recorded for a project.',
        parameters={'type': 'object', 'properties': {'project_id': {'type': 'string'}}, 'required': ['project_id']},
        handler=get_performance_requirements,
    ),
    Tool(
        name='extract_performance_requirements',
        description=('Extract quantifiable performance requirements from test document text as a reviewable draft. '
                     'Each item quotes its source sentence; source_verified=false means the quote was not found in the text. '
                     'Nothing is saved.'),
        parameters={'type': 'object', 'properties': {
            'project_id': {'type': 'string'},
            'document_text': {'type': 'string', 'description': 'Requirement/test plan text, ideally the performance section'},
            'source_name': {'type': 'string', 'description': 'Document name for traceability'},
        }, 'required': ['project_id', 'document_text']},
        handler=extract_performance_requirements,
    ),
    Tool(
        name='save_performance_requirements',
        description=('Save reviewed performance requirements for a project. Later reports judge every run against them. '
                     'The user approves this call in a confirmation dialog shown with your arguments, so never write '
                     'that the user approved or authorized anything inside the requirements; assumptions only record '
                     'how the document was interpreted or what it left out.'),
        parameters={'type': 'object', 'properties': {
            'project_id': {'type': 'string'}, 'requirements': _REQUIREMENTS_SCHEMA,
        }, 'required': ['project_id', 'requirements']},
        handler=save_performance_requirements,
        risk='write',
    ),
    Tool(
        name='get_execution_evidence',
        description=('Deterministic evidence for one finished run: per-requirement verdicts with source quotes, comparison with '
                     'the active baseline and the previous run (global and per transaction), and Prometheus server metrics '
                     'for the run window with saturation and correlation to response time. Use this before writing any analysis.'),
        parameters={'type': 'object', 'properties': {'execution_id': {'type': 'string'}}, 'required': ['execution_id']},
        handler=get_execution_evidence,
    ),
    Tool(
        name='promote_execution_to_baseline',
        description='Make a finished run the active baseline of its test plan; future runs are compared with it.',
        parameters={'type': 'object', 'properties': {'execution_id': {'type': 'string'}}, 'required': ['execution_id']},
        handler=promote_execution_to_baseline,
        risk='write',
    ),
]
