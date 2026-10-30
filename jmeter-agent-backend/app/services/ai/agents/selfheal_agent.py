"""SelfHealAgent — autonomous test plan repair when APIs change.

When a test fails because extraction rules are broken (e.g., token moved
from $.data.token to $.result.jwt), this agent:
1. Detects the failure pattern (401/403 errors on dependent requests)
2. Analyzes the upstream response to find where the token/key moved
3. Updates the extraction rules in the JMX
4. Re-runs the test automatically
"""

import asyncio
import json
import logging
import math
import re
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ExtractionFix:
    """A discovered fix for a broken extraction rule."""
    old_expression: str
    new_expression: str
    variable_name: str
    confidence: float  # 0-1, how confident the fix is correct
    source: str        # "json_path" | "regex" | "header"


@dataclass
class SelfHealResult:
    """Result of a self-healing attempt."""
    success: bool
    applied: bool = False
    verified: bool = False
    suggestions: list[str] = field(default_factory=list)
    fixes_applied: list[ExtractionFix] = field(default_factory=list)
    test_re_run: bool = False
    re_run_success: bool = False
    analysis: str = ""
    error: Optional[str] = None

    def model_dump(self) -> dict:
        return {
            "success": self.success,
            "applied": self.applied,
            "verified": self.verified,
            "suggestions": self.suggestions,
            "fixes_applied": [
                {"variable": f.variable_name, "old": f.old_expression,
                 "new": f.new_expression, "confidence": f.confidence}
                for f in self.fixes_applied
            ],
            "test_re_run": self.test_re_run,
            "re_run_success": self.re_run_success,
            "analysis": self.analysis,
            "error": self.error,
        }


class SelfHealAgent:
    """Autonomous test plan repair agent.

    Usage:
        agent = SelfHealAgent()
        result = await agent.diagnose_and_fix(
            jmx_path="test_plans/default/my_test.jmx",
            failed_execution_id="abc123",
        )
    """

    # Common token field names in JSON responses
    TOKEN_FIELD_PATTERNS = [
        r'"token"\s*:\s*"([^"]+)"',
        r'"access_token"\s*:\s*"([^"]+)"',
        r'"auth_token"\s*:\s*"([^"]+)"',
        r'"jwt"\s*:\s*"([^"]+)"',
        r'"bearer"\s*:\s*"([^"]+)"',
        r'"api_key"\s*:\s*"([^"]+)"',
        r'"session_id"\s*:\s*"([^"]+)"',
        r'"authorization"\s*:\s*"([^"]+)"',
    ]

    async def diagnose_and_fix(
        self,
        jmx_path: str,
        failed_execution_id: str,
        auto_re_run: bool = True,
    ) -> SelfHealResult:
        """Analyze a failed test and attempt to repair extraction rules.

        Args:
            jmx_path: Path to the JMX test plan.
            failed_execution_id: ID of the failed test execution.
            auto_re_run: If True, automatically re-run the test after fixing.
        """
        import os
        from app.core.config import settings, safe_path
        from app.services.execution_lifecycle import validate_id
        try:
            validate_id(failed_execution_id)
            jmx_path = safe_path(jmx_path, allowed_dirs=[settings.TEST_PLANS_DIR])
        except ValueError as exc:
            return SelfHealResult(success=False, error=str(exc))

        # Step 1: Read the test execution results
        report_dir = os.path.join(settings.REPORTS_DIR, failed_execution_id)
        jtl_path = os.path.join(report_dir, "result.jtl")
        log_path = os.path.join(report_dir, "jmeter.log")
        try:
            jtl_path = safe_path(jtl_path, allowed_dirs=[settings.REPORTS_DIR])
            log_path = safe_path(log_path, allowed_dirs=[settings.REPORTS_DIR])
        except ValueError as exc:
            return SelfHealResult(success=False, error=str(exc))

        if not os.path.exists(jtl_path):
            return SelfHealResult(
                success=False, error="JTL file not found — cannot diagnose",
            )

        # Step 2: Parse the JTL to find error patterns
        from app.services.jmeter.report_parser import ReportParser
        parser = ReportParser()

        try:
            summary = await asyncio.to_thread(parser.parse_jtl, jtl_path)
        except Exception:
            # Try reading raw JTL for error patterns
            error_patterns = self._analyze_raw_jtl(jtl_path)
        else:
            error_patterns = self._analyze_error_patterns(summary)

        if not error_patterns:
            return SelfHealResult(
                success=False,
                analysis="No authentication/extraction error patterns detected — cannot auto-heal",
            )

        # Step 3: Parse the JMX to find extraction rules
        from app.services.jmeter.jmx_parser import parse_jmx
        try:
            tree = parse_jmx(jmx_path)
        except Exception as e:
            return SelfHealResult(success=False, error=f"Cannot parse JMX: {e}")

        # Extract current extraction rules
        extractions = self._find_extractions(tree)
        if not extractions:
            return SelfHealResult(
                success=False, analysis="No extraction rules found in test plan",
            )

        # Step 4: Try to fix the extraction rules
        fixes = []
        response_bodies = self._extract_failed_response_bodies(jtl_path)
        if not response_bodies:
            return SelfHealResult(success=False,
                analysis='No response evidence; the original JMX was not changed',
                suggestions=['Capture a response from the affected endpoint and validate its extraction path before applying a fix'])

        # Read the log to find actual response bodies
        log_content = ""
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                log_content = f.read()

        for ext in extractions:
            # Try to find the variable in log output / response samples
            new_expr = self._find_new_expression(ext, log_content, jtl_path)
            if new_expr and new_expr != ext.get("expression"):
                fixes.append(ExtractionFix(
                    old_expression=ext.get("expression", ""),
                    new_expression=new_expr,
                    variable_name=ext.get("ref_name", ""),
                    confidence=0.7,
                    source=ext.get("extract_type", "json_path"),
                ))

        if not fixes:
            # Try LLM-based diagnosis with response body samples
            body_samples = "\n---\n".join([
                f"Response {rb['response_code']} on {rb['label']}:\n{rb['response_body'][:2000]}"
                for rb in response_bodies[:3]
            ]) if response_bodies else log_content[:4000]
            llm_fixes = await self._llm_diagnose(extractions, error_patterns, body_samples)
            fixes.extend(llm_fixes)

        known = {extraction.get('ref_name'): extraction for extraction in extractions}
        suggestions, verified_fixes = [], []
        for fix in fixes:
            extraction = known.get(fix.variable_name)
            if (not extraction or extraction.get('extract_type') != 'json_path'
                    or extraction.get('expression') == fix.new_expression
                    or not self._has_response_evidence(fix.new_expression, response_bodies)):
                suggestions.append(f'{fix.variable_name}: {fix.new_expression}')
            else:
                fix.old_expression = extraction.get('expression', '')
                verified_fixes.append(fix)
        fixes = verified_fixes

        if not fixes:
            return SelfHealResult(
                success=False,
                analysis=f"Detected {len(error_patterns)} error patterns but could not determine fixes. "
                          f"Manual review needed for extraction rules.",
                suggestions=suggestions,
            )

        # Step 5: Apply fixes to the JMX
        try:
            self._apply_fixes(tree, fixes)
            from app.services.jmeter.jmx_builder import build_jmx
            new_xml = build_jmx(tree)
            # Backup original
            import uuid
            backup_path = jmx_path + f".selfheal.{uuid.uuid4().hex}.bak"
            if os.path.exists(jmx_path):
                import shutil
                shutil.copy2(jmx_path, backup_path)
            with open(jmx_path, "w", encoding="utf-8") as f:
                f.write(new_xml)
        except Exception as e:
            return SelfHealResult(
                success=False, error=f"Failed to apply fixes: {e}",
                fixes_applied=fixes,
            )

        # Step 6: Optionally re-run the test
        result = SelfHealResult(
            success=True,
            applied=True,
            fixes_applied=fixes,
            suggestions=suggestions,
            test_re_run=auto_re_run,
            analysis=f"Fixed {len(fixes)} extraction rule(s)",
        )

        if auto_re_run:
            try:
                from app.services.ai.agent.tools.jmeter import execute_with_script_approval
                import uuid
                exec_result = await execute_with_script_approval(
                    'selfheal_test', jmx_path, f"selfheal_{str(uuid.uuid4())[:6]}"
                )
                summary = getattr(exec_result, 'summary', None)
                result.re_run_success = bool(exec_result.status.value == 'completed' and summary
                    and summary.total_requests > 0 and summary.error_count == 0
                    and math.isfinite(float(summary.error_rate)) and summary.error_rate == 0)
                result.verified = result.re_run_success
                result.success = result.re_run_success
                if not result.re_run_success:
                    result.error = getattr(exec_result, 'error_message', None) or 'Re-run did not verify a nonempty error-free report'
                    result.analysis += f" — Re-run failed: {result.error}"
                else:
                    result.analysis += " — Re-run succeeded!"
            except Exception as e:
                result.success = False
                result.error = f'Re-run error: {e}'
                result.analysis += f" — Re-run error: {e}"

        return result

    def _analyze_error_patterns(self, summary) -> list[str]:
        """Identify auth/extraction-related errors from the test summary."""
        patterns = []
        for err in (summary.error_details or []):
            err_type = (err.error_type or "").lower()
            if any(kw in err_type for kw in ("401", "403", "unauthorized", "forbidden", "auth")):
                patterns.append(f"Auth error: {err.error_type} ({err.count} occurrences)")
        return patterns

    def _analyze_raw_jtl(self, jtl_path: str) -> list[str]:
        """Scan raw JTL for 401/403 response codes."""
        patterns = []
        try:
            with open(jtl_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "401" in line or "403" in line:
                        patterns.append(f"Auth failure detected in JTL")
                        break
        except Exception:
            pass
        return patterns

    def _find_extractions(self, tree: dict) -> list[dict]:
        """Recursively find all extraction rules in a JMX tree."""
        results = []
        self._walk_tree(tree, results)
        return results

    def _walk_tree(self, node, results: list):
        if not isinstance(node, dict):
            return
        tc = node.get("testclass", "")
        if "Extractor" in tc or "PostProcessor" in tc:
            props = {}
            for name, value in node.get("properties", {}).items():
                if "refname" in name or "ref_name" in name or "referenceNames" in name:
                    props["ref_name"] = value
                if "regex" in name or "expression" in name or "jsonPathExpr" in name:
                    props["expression"] = value
                if "default" in name or "defaultValues" in name:
                    props["default_value"] = value
            if props.get("expression"):
                # Determine extract type
                if "JSON" in tc:
                    props["extract_type"] = "json_path"
                elif "Regex" in tc:
                    props["extract_type"] = "regex"
                elif "Boundary" in tc:
                    props["extract_type"] = "boundary"
                elif "XPath" in tc:
                    props["extract_type"] = "xpath"
                results.append(props)
        for child in node.get("children", []):
            self._walk_tree(child, results)

    def _extract_failed_response_bodies(self, jtl_path: str) -> list[dict]:
        """Extract response bodies from failed requests in JTL.

        With response_data_on_error=true, JMeter saves the full response
        body for failed requests. This gives us the actual API response
        to analyze for extraction path changes.

        Returns list of {"response_code": str, "response_body": str, "label": str}.
        """
        import csv
        import os as _os
        results = []
        if not _os.path.exists(jtl_path):
            return results
        try:
            with open(jtl_path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.DictReader(f, delimiter=",")
                for row in reader:
                    code = row.get("responseCode", "")
                    if code in ("401", "403", "500"):
                        body = row.get("responseData", row.get("responseMessage", ""))
                        if body and len(body) > 10:
                            results.append({
                                "response_code": code,
                                "response_body": body[:4000],
                                "label": row.get("label", ""),
                            })
                        if len(results) >= 5:  # Enough samples
                            break
        except Exception as e:
            logger.warning(f"SelfHeal: failed to parse JTL for response bodies: {e}")
        return results

    def _find_new_expression(self, extraction: dict, log_content: str, jtl_path: str) -> Optional[str]:
        """Try to find where the variable moved using response body analysis.

        Strategy priority:
        1. Parse JTL response bodies → find actual JSON structure → match token location
        2. Return None; unobserved paths are suggestions, never automatic edits.
        """
        var_name = extraction.get("ref_name", "")
        expr = extraction.get("expression", "")

        # Strategy 1: Analyze actual response bodies from failed requests
        response_bodies = self._extract_failed_response_bodies(jtl_path)
        if response_bodies:
            for rb in response_bodies:
                body = rb["response_body"]
                # Try to parse as JSON and search for token-like fields
                try:
                    data = json.loads(body)
                    new_path = self._search_json_for_token(data, var_name)
                    if new_path and new_path != expr:
                        logger.info(
                            f"SelfHeal: found token at {new_path} in response body "
                            f"(was {expr})"
                        )
                        return new_path
                except (json.JSONDecodeError, TypeError):
                    # Not JSON — try regex-based token search
                    for pattern in self.TOKEN_FIELD_PATTERNS:
                        matches = re.findall(pattern, body)
                        if matches:
                            # Found a token value — infer JSON path from context
                            logger.info(
                                f"SelfHeal: found token value in response body, "
                                f"but cannot infer JSON path without LLM"
                            )
                            # Return None to trigger LLM diagnosis with the response body
                            return None

        return None

    @staticmethod
    def _has_response_evidence(expression: str, response_bodies: list) -> bool:
        """Only simple JSON object/index paths can be verified automatically."""
        if not re.fullmatch(r'\$(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])+', expression):
            return False
        parts = re.findall(r'\.([A-Za-z_][A-Za-z0-9_]*)|\[([0-9]+)\]', expression)
        for response in response_bodies:
            try:
                value = json.loads(response['response_body'])
                for key, index in parts:
                    value = value[key] if key else value[int(index)]
                if value is not None and value != '' and not isinstance(value, (dict, list)):
                    return True
            except (ValueError, TypeError, KeyError, IndexError):
                continue
        return False

    def _search_json_for_token(self, data, var_name: str, prefix: str = "$") -> Optional[str]:
        """Recursively search a JSON object for token-like fields.

        Returns JSONPath expression if found, None otherwise.
        """
        if isinstance(data, dict):
            for key, value in data.items():
                current_path = f"{prefix}.{key}"
                # Check if key looks like a token/credential field
                key_lower = key.lower()
                if any(kw in key_lower for kw in (
                    "token", "access_token", "auth_token", "jwt",
                    "bearer", "api_key", "session", "authorization",
                )):
                    if isinstance(value, str) and len(value) > 8:
                        return current_path
                # Recurse into nested objects
                if isinstance(value, (dict, list)):
                    found = self._search_json_for_token(value, var_name, current_path)
                    if found:
                        return found
        elif isinstance(data, list) and len(data) > 0:
            # Check first element for token patterns
            if isinstance(data[0], dict):
                return self._search_json_for_token(data[0], var_name, f"{prefix}[0]")
        return None

    async def _llm_diagnose(
        self, extractions: list[dict], error_patterns: list[str], log_snippet: str,
    ) -> list[ExtractionFix]:
        """Use LLM to suggest new extraction expressions."""
        try:
            from app.services.ai.adapters.factory import AIAdapterFactory
            from app.models.schemas import AIChatMessage

            prompt = f"""A JMeter test plan failed because authentication extraction rules are broken.

Current extraction rules:
{json.dumps(extractions, indent=2)}

Error patterns:
{json.dumps(error_patterns, indent=2)}

Log snippet:
{log_snippet[:2000]}

Suggest new extraction expressions. Respond with JSON array:
[{{"variable_name": "...", "new_expression": "...", "confidence": 0.8}}]"""

            adapter = AIAdapterFactory.create()
            messages = adapter.prepare_chat_messages(
                [AIChatMessage(role="user", content=prompt)], response_tokens=500)
            response = await adapter.chat(
                messages,
                temperature=0.2, max_tokens=500,
            )

            # Try to parse JSON from response
            match = re.search(r'\[.*\]', response, re.DOTALL)
            if match:
                suggestions = json.loads(match.group())
                return [
                    ExtractionFix(
                        old_expression=extractions[0].get("expression", "") if extractions else "",
                        new_expression=s.get("new_expression", ""),
                        variable_name=s.get("variable_name", ""),
                        confidence=min(s.get("confidence", 0.5), 0.9),
                        source="llm",
                    )
                    for s in suggestions if s.get("new_expression")
                ]
        except Exception as e:
            logger.warning(f"LLM self-heal diagnosis failed: {e}")

        return []

    def _apply_fixes(self, tree: dict, fixes: list[ExtractionFix]):
        """Apply extraction fixes to the JMX tree."""
        for fix in fixes:
            self._apply_fix_to_tree(tree, fix)

    def _apply_fix_to_tree(self, node: dict, fix: ExtractionFix):
        if not isinstance(node, dict):
            return
        tc = node.get("testclass", "")
        if "Extractor" in tc or "PostProcessor" in tc:
            properties = node.get("properties", {})
            for name, value in properties.items():
                if "refname" in name or "referenceNames" in name:
                    if value == fix.variable_name:
                        # Found the right extractor — update expression
                        for pname in properties:
                            if "regex" in pname or "expression" in pname or "jsonPathExpr" in pname:
                                properties[pname] = fix.new_expression
                                logger.info(
                                    f"SelfHeal: Updated {fix.variable_name} expression: "
                                    f"{fix.old_expression} → {fix.new_expression}"
                                )
                                return
        for child in node.get("children", []):
            self._apply_fix_to_tree(child, fix)


selfheal = SelfHealAgent()
