"""TaskPlanner — decompose a user task into executable steps."""

import json
import re
from typing import Optional

from app.models.schemas import AIChatMessage
from app.services.ai.agent.types import PlanStep
from app.services.ai.adapters.base import BaseAIAdapter


PLANNER_RESPONSE_TOKENS = 4000

PLANNER_SYSTEM_PROMPT = """Task planner. Decompose user task into ordered steps.

Each step: step_id(int), description(Chinese), tool_name(or null), tool_args(or null), expected_outcome(Chinese).

Output ONLY valid JSON:
{"steps": [{"step_id": 1, "description": "...", "tool_name": "tool_name or null", "tool_args": {"key": "value"} or null, "expected_outcome": "..."}]}

Rules: one action per step, note dependencies in description, only assign tool_name when needed, match tool args to schema.
Plan only what the task asks for. Leave tool_args null when they are long: the step decides them when it runs.
CRITICAL: Output MUST start with '{' and end with '}'. No markdown, no explanation."""


class TaskPlanner:
    """Decompose user tasks into ordered PlanSteps using LLM."""

    def __init__(self, adapter: BaseAIAdapter, safety_prompt: str = "", domain_prompt: str = ""):
        self.adapter = adapter
        self.system_prompt = "\n\n".join(filter(None, [PLANNER_SYSTEM_PROMPT, safety_prompt, domain_prompt]))

    async def plan(
        self,
        task: str,
        tools_description: str,
        context: Optional[str] = None,
    ) -> list[PlanStep]:
        """Generate a plan from a task description, with retry on parse failure.

        Args:
            task: The user's task description.
            tools_description: Human-readable description of available tools.
            context: Optional additional context (previous results, etc.)
        """
        prompt_parts = [
            f"## 用户任务\n{task}",
            f"\n## 可用工具\n{tools_description}",
        ]
        if context:
            prompt_parts.append(f"\n## 上下文\n{context}")

        prompt = "\n".join(prompt_parts)

        messages = [
            AIChatMessage(role="system", content=self.system_prompt),
            AIChatMessage(role="user", content=prompt, pinned=True),
        ]

        # Try up to 2 times if JSON parsing fails
        plan = []
        for attempt in range(2):
            selected = self.adapter.prepare_chat_messages(messages, response_tokens=PLANNER_RESPONSE_TOKENS)
            response = await self.adapter.chat(selected, temperature=0.0, max_tokens=PLANNER_RESPONSE_TOKENS)
            plan = self._parse_plan(response)
            if plan:
                return plan  # A valid reasoning step may deliberately have no tool.
            if attempt == 0:
                # Retry: ask LLM to fix the format
                messages.append(AIChatMessage(role="assistant", content=response[:500]))
                messages.append(AIChatMessage(
                    role="user",
                    content="Invalid format. Output ONLY valid JSON starting with { and ending with }. "
                            "Use tool_name=null for reasoning-only steps. No markdown, no explanation.",
                ))
            else:
                return plan

        return plan

    def _parse_plan(self, response: str) -> list[PlanStep]:
        """Extract plan steps from LLM response with robust JSON extraction."""
        if not response or not response.strip():
            return self._fallback_plan(response)

        text = response.strip()

        # Strip markdown fences
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        # Find balanced outermost JSON object
        json_str = self._extract_json_object(text)
        if not json_str:
            return self._fallback_plan(response)

        try:
            data = json.loads(json_str)
            if not isinstance(data, dict) or not isinstance(data.get('steps'), list):
                return []
            steps_data = data['steps']
            steps = []
            seen_ids = set()
            for s in steps_data:
                if not isinstance(s, dict):
                    return []
                step_id = s.get('step_id', len(steps) + 1)
                if (type(step_id) is not int or step_id <= 0 or step_id in seen_ids
                    or not isinstance(s.get('description'), str) or not s['description'].strip()
                    or (s.get('tool_name') is not None and not isinstance(s['tool_name'], str))
                    or (s.get('tool_args') is not None and not isinstance(s['tool_args'], dict))
                    or not isinstance(s.get('expected_outcome', ''), str)):
                    return []
                seen_ids.add(step_id)
                steps.append(PlanStep(
                    step_id=step_id,
                    description=s['description'],
                    tool_name=s.get("tool_name"),
                    tool_args=s.get("tool_args"),
                    expected_outcome=s.get("expected_outcome", ""),
                ))
            return steps if steps else self._fallback_plan(response)
        except (json.JSONDecodeError, KeyError) as e:
            return self._fallback_plan(response)

    @staticmethod
    def _extract_json_object(text: str) -> Optional[str]:
        """Extract the outermost balanced JSON object from text.

        More robust than greedy regex — handles nested braces.
        """
        start = text.find("{")
        if start == -1:
            return None
        try:
            _, length = json.JSONDecoder().raw_decode(text[start:])
            return text[start:start + length]
        except (ValueError, RecursionError):
            return None

    def _fallback_plan(self, raw_response: str) -> list[PlanStep]:
        """Signal parse failure so BaseAgent can use the real tool fallback."""
        return []

    async def replan(
        self,
        original_plan: list[PlanStep],
        failed_step: PlanStep,
        error_message: str,
        tools_description: str,
    ) -> list[PlanStep]:
        """Adjust the plan after a step failure."""
        plan_desc = "\n".join([
            f"Step {s.step_id}: {s.description} (success={s.success})"
            for s in original_plan
        ])

        prompt = f"""以下计划执行失败，请调整后续步骤：

## 原始计划
{plan_desc}

## 失败的步骤
Step {failed_step.step_id}: {failed_step.description}
错误: {error_message}

## 可用工具
{tools_description}

请输出调整后的剩余步骤（不包括已成功的步骤），JSON格式：
{{"steps": [...]}}"""

        messages = [
            AIChatMessage(role="system", content=self.system_prompt),
            AIChatMessage(role="user", content=prompt),
        ]

        messages = self.adapter.prepare_chat_messages(messages, response_tokens=PLANNER_RESPONSE_TOKENS)
        response = await self.adapter.chat(messages, temperature=0.3, max_tokens=PLANNER_RESPONSE_TOKENS)
        return self._parse_plan(response)
