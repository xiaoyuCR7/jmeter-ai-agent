"""Starting from a document: the streamed workflow and OpenAPI import."""
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from app.models.schemas import AIWorkflowRequest
from app.services.ai import usage as ai_usage
from app.services.ai.adapters.factory import AIAdapterFactory
from app.services.openapi_importer import OpenAPIImporter
from app.services.storage import store
from app.services.execution_lifecycle import persist_project

router = APIRouter()


# ===== AI Workflow =====
@router.post("/ai/workflow/stream")
async def ai_workflow_stream(request: AIWorkflowRequest):
    """AI 工作流 SSE 接口 —— 从API文档创建项目、测试计划和性能需求草稿"""
    from app.services.ai.agents.workflow_agent import WorkflowAgent

    lang = request.lang or "zh-CN"
    is_en = lang == "en"

    async def generate():
        with ai_usage.metered() as used:
            async for event in steps():
                if event.get('step') == 'complete':
                    event['usage'] = used
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    async def steps():
        try:
            agent = WorkflowAgent(AIAdapterFactory.get_adapter(request.ai_model), lang)
            result = None
            # Generation takes minutes; each stage reports itself so the page never looks stuck.
            async for kind, payload in agent.stream_from_doc(request.api_documentation, request.testing_requirements):
                if kind == 'result':
                    result = payload
                else:
                    yield payload
            if not result['success']:
                message = result.get('error') or 'Unknown error'
                if result.get('error_code') == 'document_too_large':
                    message = (f"The document is about {result['tokens']} tokens, more than this model can work with "
                               f"(about {result['limit']}). Paste only the endpoints to test, or choose a model with a larger context."
                               if is_en else f"文档约 {result['tokens']} token，超出这个模型能处理的范围（约 {result['limit']}）。"
                               "请只粘贴要测的接口，或换一个上下文更大的模型。")
                yield {'step': 'error', 'message': message, 'progress': 100, 'error': True}
                return
            count = len(result['test_plan_ids'])
            yield {
                'step': 'complete', 'progress': 100,
                'message': (f"Created the project with {count} test plans" if is_en
                            else f"已创建项目，共 {count} 个测试计划"),
                'project_id': result['project_id'], 'test_plan_ids': result['test_plan_ids'],
                'missing_endpoints': result['missing_endpoints'],
                'diagnostic_summary': result['diagnostic_summary'],
                'requirements_draft': result['requirements_draft'],
            }
        except Exception as e:
            err_msg = f"Workflow failed: {str(e)}" if is_en else f"工作流执行失败: {str(e)}"
            yield {'step': 'error', 'message': err_msg, 'progress': 100, 'error': True}

    return StreamingResponse(generate(), media_type="text/event-stream")


# ===== OpenAPI/Swagger Import =====

@router.post("/openapi/import")
async def openapi_import(request: dict):
    """Import an OpenAPI/Swagger spec from URL or JSON body.

    Accepts:
    - {"url": "https://petstore.swagger.io/v2/swagger.json"} — fetches spec from URL
    - {"spec": {...}} — parses spec dict directly
    - {"url": "...", "project_name": "...", "execute": true/false}

    Returns ImportResult with project_id, endpoints_found, auth_detected, jmx_paths.
    """
    url = request.get("url", "")
    spec_dict = request.get("spec")
    project_name = request.get("project_name", "")
    execute = request.get("execute", False)
    if not isinstance(execute, bool):
        raise HTTPException(status_code=422, detail="execute must be a JSON boolean")

    importer = OpenAPIImporter()

    if spec_dict:
        result = await importer.import_from_dict(spec_dict, source_url=url, execute_tests=execute)
    elif url:
        result = await importer.import_from_url(url, execute_tests=execute)
    else:
        raise HTTPException(status_code=400, detail="Either 'url' or 'spec' is required")

    if project_name and result.success:
        p = store.get_project(result.project_id)
        if p:
            p = p.model_copy(deep=True)
            p.name = project_name
            await persist_project(p, create=False)
            result.project_name = project_name

    return result.model_dump() if not result.success else {
        **result.model_dump(),
        "test_plans": [{"name": p.name, "jmx_path": p.jmx_path, "id": p.id}
                       for p in store.test_plans.values()
                       if getattr(p, 'project_id', '') == result.project_id],
    }
