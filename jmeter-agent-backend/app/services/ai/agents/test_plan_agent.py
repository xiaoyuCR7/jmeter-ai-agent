"""TestPlanAgent — generate, validate, and iterate on JMX test plans."""

from typing import Optional

from app.services.ai.adapters.base import BaseAIAdapter
from app.services.ai.agent.base import BaseAgent
from app.services.ai.agent.tool_registry import ToolRegistry
from app.services.ai.agent.tools import get_all_builtin_tools
from app.services.ai.prompts.jmx_generator import SCENARIO_RULES


TEST_PLAN_SYSTEM_PROMPT = f"""JMeter test plan generator agent. Extract APIs from docs, detect dependencies, generate .jmx files.

{SCENARIO_RULES}

## Dependency detection rules
- Extract types: json_path ($.data.token), regex ("token":"([^"]+)"), header (Set-Cookie)
- Order endpoints: no-dependency first, dependent later

## Test types
The load comes from the user's words or documents. If they give none, stay small; never pick a large load yourself.
- baseline: one steady level | concurrency: many users at once
- ramp_up: rising steps, set step_users and step_duration | stress: rises in five steps to concurrent_users
- stability: long steady run, set stability_hours | spike: background load, then a sudden burst

## Workflow
Read the document, then call create_jmx once with the whole scenario.
create_jmx checks the plan and has JMeter load it. A saved plan is valid: do not validate it again or read it back.
If it refuses the plan, nothing was saved: fix the arguments from its issues and call it again.
Finish with: test_plan_id, load, each request with its expected status and checks, and any warnings. Keep it short."""


# Generating a plan needs no way to run tests, write files or delete anything.
PLAN_TOOLS = {'create_jmx', 'validate_jmx', 'update_jmx', 'clone_jmx', 'parse_jmx_tree', 'list_test_plans',
              'list_projects', 'get_project_info', 'create_project', 'get_performance_requirements'}


class TestPlanAgent(BaseAgent):
    """Specialized agent for generating and validating JMX test plans.

    Usage:
        adapter = AIAdapterFactory.create("openai")
        agent = TestPlanAgent(adapter)
        result = await agent.run(
            "为 https://httpbin.org 的 GET /get 和 POST /post 接口生成基准测试"
        )
    """

    plans_steps = False  # One tool call does the job; see BaseAgent.plans_steps.

    def __init__(
        self,
        adapter: BaseAIAdapter,
        tools: Optional[ToolRegistry] = None,
        session_id: Optional[str] = None,
    ):
        if tools is None:
            tools = ToolRegistry()
            tools.register_many([tool for tool in get_all_builtin_tools() if tool.name in PLAN_TOOLS])

        super().__init__(
            name="TestPlanAgent",
            adapter=adapter,
            tools=tools,
            system_prompt=TEST_PLAN_SYSTEM_PROMPT,
            max_iterations=12,
            session_id=session_id,
        )

    async def generate_from_doc(
        self,
        api_doc: str,
        test_type: str = "baseline",
        base_url: str = "",
        **test_params,
    ) -> dict:
        """Generate a test plan from API documentation.

        Args:
            api_doc: API documentation text (OpenAPI, Swagger, or free text).
            test_type: Type of test.
            base_url: Base URL of the target system.
            **test_params: Override default test parameters (concurrent_users, duration_seconds, etc.)
        """
        task = f"""请根据以下API文档生成测试计划：

测试类型: {test_type}
基础URL: {base_url}
自定义参数: {test_params or '使用默认值'}

API文档:
{api_doc}

请执行以下步骤：
1. 从文档中提取所有API接口（HTTP方法、路径、参数等）
2. 使用 create_jmx 工具为提取的接口生成测试计划（它会自行校验）
3. 报告结果"""

        result = await self.run(task)
        return {
            "success": result.success,
            "output": result.output,
            "steps": len(result.plan),
            "iterations": result.iterations,
            "error": result.error,
        }
