import xml.etree.ElementTree as ET
from xml.dom import minidom
import hashlib
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit, urlencode

from app.core.config import settings
from pydantic import BaseModel, Field

from app.models.schemas import APIEndpoint, CsvDataFile, TestConfig, TestType, ExtractionType
from app.services.ai.adapters.factory import AIAdapterFactory
from app.services.ai.prompts.jmx_generator import SCENARIO_SYSTEM_PROMPT, build_scenario_prompt



# Models that think spend this budget before they answer.
SCENARIO_MAX_TOKENS = 8000
SCENARIO_ATTEMPTS = 3

# Steps of a stress test: a rising load shows where the system starts to give way.
STRESS_STEPS = 5
SPIKE_BASE_SHARE = 10  # One user in ten stays on as background load.


@dataclass(frozen=True)
class LoadGroup:
    name: str
    users: int
    ramp_seconds: int
    delay_seconds: int
    duration_seconds: int


def _steps(prefix: str, users: int, step_users: int, step_seconds: int) -> list[LoadGroup]:
    """Each step is a thread group starting later and ending with the others, which needs no plugin."""
    count = max(math.ceil(users / max(step_users, 1)), 1)
    total = count * step_seconds
    return [LoadGroup(f"{prefix} step {index + 1}", min(step_users, users - index * step_users),
                      max(step_seconds // 6, 1), index * step_seconds, total - index * step_seconds)
            for index in range(count)]


def load_shape(config: TestConfig) -> list[LoadGroup]:
    """How the test type turns users and time into thread groups."""
    users = config.concurrent_users or 1
    ramp = config.ramp_up_seconds or 0
    duration = config.duration_seconds or 0
    kind = config.test_type
    if kind == TestType.RAMP_UP:
        return _steps("RAMP_UP", users, config.step_users or 10, config.step_duration or 60)
    if kind == TestType.STRESS and duration:
        steps = min(STRESS_STEPS, users)
        return _steps("STRESS", users, math.ceil(users / steps), max(duration // steps, 1))
    if kind == TestType.SPIKE and duration:
        base = max(users // SPIKE_BASE_SHARE, 1)
        groups = [LoadGroup("SPIKE base", base, min(ramp, duration // 6), 0, duration)]
        if users > base:
            groups.append(LoadGroup("SPIKE burst", users - base, min(ramp, max(duration // 12, 1)), duration // 3,
                                    max(duration // 3, 1)))
        return groups
    if kind == TestType.STABILITY and config.stability_hours:
        duration = math.ceil(config.stability_hours * 3600)
    return [LoadGroup(f"{kind.value.upper()} Test", users, ramp, 0, duration)]


class Scenario(BaseModel):
    """What the model decides; everything about JMX syntax stays in this module."""
    base_url: Optional[str] = None
    endpoints: List[APIEndpoint] = Field(..., min_length=1)
    csv_data_files: Optional[List[CsvDataFile]] = None


def normalize_scenario(endpoints: List[APIEndpoint]) -> List[APIEndpoint]:
    """Make a model's plan comparable and lean.

    Labels are the key history is compared on, and a model names the same request differently
    each time. A value nothing reads only costs the extractor.
    """
    used = json.dumps([ep.model_dump(exclude={"extractions"}) for ep in endpoints], ensure_ascii=False)
    names, seen = {}, {}
    for endpoint in endpoints:
        label = f"{endpoint.method.upper()} {endpoint.path.split('?')[0]}"
        seen[label] = seen.get(label, 0) + 1
        names[endpoint.name] = label if seen[label] == 1 else f"{label} #{seen[label]}"
    return [endpoint.model_copy(update={
        "name": names[endpoint.name],
        "depends_on": [names.get(name, name) for name in endpoint.depends_on or []],
        "extractions": [rule for rule in endpoint.extractions or [] if "${" + rule.ref_name + "}" in used
                        or f'"variable_name": "{rule.ref_name}"' in used],
    }) for endpoint in endpoints]


def _parse_scenario(response: str) -> Scenario:
    text = (response or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("The reply has no JSON object")
    return Scenario.model_validate(json.loads(text[start:end + 1]))


class JMXGenerator:
    """JMeter测试计划生成器"""
    
    def __init__(self, model_type: Optional[str] = None):
        self.model_type = model_type
    
    async def generate_from_ai(self, api_doc: str, endpoints: List[APIEndpoint],
                               test_config: TestConfig, base_url: Optional[str] = None) -> str:
        """让模型设计场景，再由生成器写出 JMX；校验不通过时把问题交还模型修改"""
        from app.models.schemas import AIChatMessage
        from app.services.jmeter import jmx_lint
        adapter = AIAdapterFactory.create(self.model_type)
        messages = [
            AIChatMessage(role="system", content=SCENARIO_SYSTEM_PROMPT),
            AIChatMessage(role="user", content=build_scenario_prompt(
                api_doc, [ep.model_dump() for ep in endpoints] if endpoints else None,
                test_config.model_dump(), base_url), pinned=True),
        ]
        problem = ""
        for _ in range(SCENARIO_ATTEMPTS):
            prepared = adapter.prepare_chat_messages(messages, response_tokens=SCENARIO_MAX_TOKENS)
            response = await adapter.chat(prepared, temperature=0.1, max_tokens=SCENARIO_MAX_TOKENS)
            try:
                scenario = _parse_scenario(response)
                target = base_url or scenario.base_url
                if not target:
                    raise ValueError("The document gives no base_url")
                config = test_config
                if scenario.csv_data_files:
                    config = test_config.model_copy(update={"csv_data_files": scenario.csv_data_files})
                jmx = self.generate_basic_jmx(normalize_scenario(scenario.endpoints), config, target,
                                              f"{test_config.test_type.value}_test")
                failures = jmx_lint.errors(jmx_lint.lint(jmx))
                if not failures:
                    return jmx
                problem = jmx_lint.describe(failures)
            except ValueError as error:  # pydantic's ValidationError is a ValueError
                problem = str(error)[:1500]
            messages.append(AIChatMessage(role="assistant", content=(response or "")[:4000]))
            messages.append(AIChatMessage(role="user", content=f"这个场景不能使用：{problem}\n修正后重新输出完整 JSON。"))
        raise ValueError(f"AI could not produce a valid plan: {problem}")

    def generate_basic_jmx(self, endpoints: List[APIEndpoint], test_config: TestConfig,
                          base_url: str, test_name: str = "Test Plan") -> str:
        """生成基础JMX（无需AI）"""
        if test_config.target_rps is not None and test_config.target_rps < 0:
            raise ValueError("target_rps must be non-negative")
        base = self._split_base_url(base_url)
        root = ET.Element("jmeterTestPlan")
        root.set("version", "1.2")
        root.set("properties", "5.0")
        root.set("jmeter", "5.6.3")

        hash_tree = ET.SubElement(root, "hashTree")
        test_plan = self._create_test_plan(hash_tree, test_name)
        test_plan_hash = ET.SubElement(hash_tree, "hashTree")

        ordered_endpoints = self._topological_sort(endpoints)
        keeps_session = any(endpoint.phase == "once_per_user" for endpoint in endpoints)

        # Configuration lives on the plan, so every thread group of a load shape shares it.
        if test_config.target_rps:
            timer = ET.SubElement(test_plan_hash, "ConstantThroughputTimer", {
                "guiclass": "TestBeanGUI", "testclass": "ConstantThroughputTimer",
                "testname": "Target RPS (all active threads, shared)", "enabled": "true"})
            # JMeter's persisted mode is the enum ordinal, not the UI label.
            ET.SubElement(timer, "intProp", name="calcMode").text = "3"
            throughput = ET.SubElement(timer, "doubleProp")
            ET.SubElement(throughput, "name").text = "throughput"
            ET.SubElement(throughput, "value").text = str(float(test_config.target_rps * 60))
            ET.SubElement(throughput, "savedValue").text = "0.0"
            ET.SubElement(test_plan_hash, "hashTree")

        self._create_http_defaults(test_plan_hash, base_url, test_config)
        ET.SubElement(test_plan_hash, "hashTree")

        # Only common headers shared by ALL endpoints, so one endpoint's headers never reach the others.
        global_headers = self._compute_common_headers(endpoints)
        if global_headers:
            self._create_header_manager(test_plan_hash, global_headers)
            ET.SubElement(test_plan_hash, "hashTree")

        # A session from a once-per-user login has to survive into later iterations.
        self._create_cookie_manager(test_plan_hash, clear_each_iteration=not keeps_session)
        ET.SubElement(test_plan_hash, "hashTree")

        self._create_cache_manager(test_plan_hash)
        ET.SubElement(test_plan_hash, "hashTree")

        for csv_file in test_config.csv_data_files or []:
            self._create_csv_dataset(
                test_plan_hash, filename=self._csv_path(csv_file), variable_names=",".join(csv_file.variables),
                delimiter=csv_file.delimiter, recycle=csv_file.recycle, stop_thread=csv_file.stop_thread,
                sharing_mode=csv_file.sharing)
            ET.SubElement(test_plan_hash, "hashTree")

        setup = [endpoint for endpoint in ordered_endpoints if endpoint.phase == "setup"]
        flow = [endpoint for endpoint in ordered_endpoints if endpoint.phase != "setup"]
        if setup:
            self._create_thread_group(test_plan_hash, LoadGroup("Setup", 1, 0, 0, 0), test_config, element="SetupThreadGroup")
            self._add_flow(ET.SubElement(test_plan_hash, "hashTree"), setup, base, 0)
        for group in load_shape(test_config):
            self._create_thread_group(test_plan_hash, group, test_config)
            self._add_flow(ET.SubElement(test_plan_hash, "hashTree"), flow, base, test_config.think_time_ms or 0)

        return self._finalize_jmx(root, test_plan_hash)

    def _add_flow(self, parent: ET.Element, endpoints: list, base, think_time: int):
        """Add the requests one thread group runs, each inside the controllers its role needs."""
        shared, shared_weight = None, None
        for endpoint in endpoints:
            target = parent
            if endpoint.phase == "once_per_user":
                target = self._create_once_only_controller(target, endpoint.name)
            if endpoint.weight and endpoint.weight < 100:
                # Adjacent requests of one weight are one branch: a purchase funnel runs whole or not at all.
                if endpoint.weight != shared_weight or target is not parent:
                    shared = self._create_throughput_controller(target, endpoint.name, endpoint.weight)
                    shared_weight = endpoint.weight
                target = shared
            else:
                shared_weight = None
            self._add_endpoint(target, endpoint, base, think_time)

    def _add_endpoint(self, parent: ET.Element, endpoint, base, think_time: int):
        # Variable bindings from upstream extractions; a copy, since a load shape adds it many times.
        resolved_endpoint = self._resolve_bindings(endpoint)
        endpoint_url = urlsplit(resolved_endpoint.path)
        if not endpoint_url.scheme and not endpoint_url.netloc:
            path = base.path.rstrip("/") + "/" + endpoint_url.path.lstrip("/")
            query = "&".join(value for value in (base.query, endpoint_url.query) if value)
            resolved_endpoint.path = urlunsplit(("", "", path, query, ""))

        self._create_http_request(parent, resolved_endpoint)
        req_hash = ET.SubElement(parent, "hashTree")

        # Per-endpoint header manager (with resolved variables)
        per_endpoint_headers = self._resolve_header_bindings(endpoint)
        if resolved_endpoint.content_type and not any(
                name.lower() == "content-type" for name in per_endpoint_headers):
            per_endpoint_headers["Content-Type"] = resolved_endpoint.content_type
        if per_endpoint_headers:
            self._create_header_manager(req_hash, per_endpoint_headers)
            ET.SubElement(req_hash, "hashTree")

        self._create_response_assertion(req_hash, resolved_endpoint.expected_status or 200)
        ET.SubElement(req_hash, "hashTree")

        for check in (endpoint.response_checks or []):
            self._create_json_assertion(req_hash, check)
            ET.SubElement(req_hash, "hashTree")

        # Add Post-Processors for variable extraction
        for extraction in (endpoint.extractions or []):
            if extraction.extract_type == "json_path":
                self._create_json_extractor(req_hash, extraction)
            elif extraction.extract_type == "regex":
                self._create_regex_extractor(req_hash, extraction)
            elif extraction.extract_type == "boundary":
                self._create_boundary_extractor(req_hash, extraction)
            elif extraction.extract_type == "xpath":
                self._create_xpath_extractor(req_hash, extraction)
            elif extraction.extract_type == "header":
                self._create_header_extractor(req_hash, extraction)
            ET.SubElement(req_hash, "hashTree")

        # Add think time timer after each request (if configured)
        if think_time > 0:
            # A spread keeps users from settling into lockstep at the same moments.
            self._create_uniform_random_timer(req_hash, round(think_time * 0.75), round(think_time * 1.25))
            ET.SubElement(req_hash, "hashTree")

        # Without its token every later request of this user fails, and the error rate says nothing.
        if endpoint.phase == "once_per_user":
            self._create_stop_user_on_error(req_hash)
            ET.SubElement(req_hash, "hashTree")

    @staticmethod
    def _csv_path(csv_file) -> str:
        """Where JMeter reads the file: rows are stored by content, so identical data shares one file."""
        from app.core.config import safe_path
        data_dir = os.path.join(settings.BASE_DIR, "test_data")
        if csv_file.rows:
            content = "\n".join(csv_file.delimiter.join(row) for row in csv_file.rows) + "\n"
            name = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16] + ".csv"
            path = os.path.join(data_dir, name)
            os.makedirs(data_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            return path
        path = safe_path(os.path.join(data_dir, csv_file.filename or ""), [data_dir])
        if not csv_file.filename or not os.path.isfile(path):
            raise ValueError(f"Test data file not found in test_data: {csv_file.filename}")
        return path

    def _finalize_jmx(self, root: ET.Element, test_plan_hash: ET.Element) -> str:
        """Add listeners and serialize the JMX XML tree to a pretty-printed string."""
        # Add listeners at test plan level
        self._create_listeners(test_plan_hash)

        # Pretty print
        rough_string = ET.tostring(root, encoding="unicode")
        reparsed = minidom.parseString(rough_string)
        pretty = reparsed.toprettyxml(indent="  ")
        # Remove empty lines
        lines = [line for line in pretty.split("\n") if line.strip()]
        return "\n".join(lines)
    
    def _create_test_plan(self, parent: ET.Element, name: str) -> ET.Element:
        elem = ET.SubElement(parent, "TestPlan")
        elem.set("guiclass", "TestPlanGui")
        elem.set("testclass", "TestPlan")
        elem.set("testname", name)
        elem.set("enabled", "true")
        
        string_prop = ET.SubElement(elem, "stringProp")
        string_prop.set("name", "TestPlan.comments")
        
        bool_prop = ET.SubElement(elem, "boolProp")
        bool_prop.set("name", "TestPlan.functional_mode")
        bool_prop.text = "false"
        
        bool_prop2 = ET.SubElement(elem, "boolProp")
        bool_prop2.set("name", "TestPlan.tearDown_on_shutdown")
        bool_prop2.text = "true"
        
        bool_prop3 = ET.SubElement(elem, "boolProp")
        bool_prop3.set("name", "TestPlan.serialize_threadgroups")
        bool_prop3.text = "false"
        
        collection = ET.SubElement(elem, "elementProp")
        collection.set("name", "TestPlan.user_defined_variables")
        collection.set("elementType", "Arguments")
        collection.set("guiclass", "ArgumentsPanel")
        collection.set("testclass", "Arguments")
        collection.set("testname", "User Defined Variables")
        collection.set("enabled", "true")
        
        col_hash = ET.SubElement(collection, "collectionProp")
        col_hash.set("name", "Arguments.arguments")
        
        return elem
    
    def _create_thread_group(self, parent: ET.Element, group: "LoadGroup", config: TestConfig,
                             element: str = "ThreadGroup") -> ET.Element:
        elem = ET.SubElement(parent, element)
        elem.set("guiclass", f"{element}Gui")
        elem.set("testclass", element)
        elem.set("testname", group.name)
        elem.set("enabled", "true")

        string_prop = ET.SubElement(elem, "stringProp")
        string_prop.set("name", "ThreadGroup.on_sample_error")
        string_prop.text = "continue"

        controller = ET.SubElement(elem, "elementProp")
        controller.set("name", "ThreadGroup.main_controller")
        controller.set("elementType", "LoopController")
        controller.set("guiclass", "LoopControlPanel")
        controller.set("testclass", "LoopController")
        controller.set("testname", "Loop Controller")
        controller.set("enabled", "true")

        bool_prop = ET.SubElement(controller, "boolProp")
        bool_prop.set("name", "LoopController.continue_forever")
        bool_prop.text = "false"

        int_prop = ET.SubElement(controller, "intProp")
        int_prop.set("name", "LoopController.loops")
        if element == "SetupThreadGroup":
            int_prop.text = "1"
        elif config.iterations:
            int_prop.text = str(config.iterations)
        else:
            int_prop.text = "-1"  # Forever, controlled by duration

        string_prop2 = ET.SubElement(elem, "stringProp")
        string_prop2.set("name", "ThreadGroup.num_threads")
        string_prop2.text = str(group.users)

        string_prop3 = ET.SubElement(elem, "stringProp")
        string_prop3.set("name", "ThreadGroup.ramp_time")
        string_prop3.text = str(group.ramp_seconds)

        bool_prop2 = ET.SubElement(elem, "boolProp")
        bool_prop2.set("name", "ThreadGroup.scheduler")
        bool_prop2.text = "true" if group.duration_seconds else "false"

        string_prop4 = ET.SubElement(elem, "stringProp")
        string_prop4.set("name", "ThreadGroup.duration")
        string_prop4.text = str(group.duration_seconds)

        string_prop5 = ET.SubElement(elem, "stringProp")
        string_prop5.set("name", "ThreadGroup.delay")
        string_prop5.text = str(group.delay_seconds)

        return elem

    @staticmethod
    def _split_base_url(base_url: str):
        parsed = urlsplit(base_url if "://" in base_url else "https://" + base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an HTTP(S) URL with a host")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Unsupported credentials or fragment in base_url; use request headers")
        # Accessing port validates malformed/non-numeric/out-of-range ports.
        parsed.port
        return parsed

    def _create_http_defaults(self, parent: ET.Element, base_url: str, config: TestConfig):
        elem = ET.SubElement(parent, "ConfigTestElement")
        elem.set("guiclass", "HttpDefaultsGui")
        elem.set("testclass", "ConfigTestElement")
        elem.set("testname", "HTTP Request Defaults")
        elem.set("enabled", "true")
        
        parsed = self._split_base_url(base_url)
        protocol, domain = parsed.scheme, parsed.hostname
        port = str(parsed.port) if parsed.port is not None else ""
        
        props = [
            ("HTTPSampler.protocol", protocol),
            ("HTTPSampler.domain", domain),
            ("HTTPSampler.port", port),
            ("HTTPSampler.path", ""),
            ("HTTPSampler.contentEncoding", ""),
            ("HTTPSampler.concurrentPool", "6"),
            # Without limits a stalled server holds its threads and the run never reaches its load.
            ("HTTPSampler.connect_timeout", str(config.connect_timeout_ms or "")),
            ("HTTPSampler.response_timeout", str(config.response_timeout_ms or "")),
        ]
        for name, value in props:
            sp = ET.SubElement(elem, "stringProp")
            sp.set("name", name)
            sp.text = value
        
        bool_prop = ET.SubElement(elem, "boolProp")
        bool_prop.set("name", "HTTPSampler.image_parser")
        bool_prop.text = "false"
        
        bool_prop2 = ET.SubElement(elem, "boolProp")
        bool_prop2.set("name", "HTTPSampler.md5")
        bool_prop2.text = "false"
    
    def _create_header_manager(self, parent: ET.Element, headers: dict):
        elem = ET.SubElement(parent, "HeaderManager")
        elem.set("guiclass", "HeaderPanel")
        elem.set("testclass", "HeaderManager")
        elem.set("testname", "HTTP Header Manager")
        elem.set("enabled", "true")

        collection = ET.SubElement(elem, "collectionProp")
        collection.set("name", "HeaderManager.headers")

        for name, value in (headers or {}).items():
            header = ET.SubElement(collection, "elementProp")
            header.set("name", "")
            header.set("elementType", "Header")

            sp1 = ET.SubElement(header, "stringProp")
            sp1.set("name", "Header.name")
            sp1.text = str(name)

            sp2 = ET.SubElement(header, "stringProp")
            sp2.set("name", "Header.value")
            sp2.text = str(value)
    
    def _create_cookie_manager(self, parent: ET.Element, clear_each_iteration: bool = True):
        elem = ET.SubElement(parent, "CookieManager")
        elem.set("guiclass", "CookiePanel")
        elem.set("testclass", "CookieManager")
        elem.set("testname", "HTTP Cookie Manager")
        elem.set("enabled", "true")
        
        collection = ET.SubElement(elem, "collectionProp")
        collection.set("name", "CookieManager.cookies")
        
        bool_prop = ET.SubElement(elem, "boolProp")
        bool_prop.set("name", "CookieManager.clearEachIteration")
        bool_prop.text = "true" if clear_each_iteration else "false"
    
    def _create_cache_manager(self, parent: ET.Element):
        elem = ET.SubElement(parent, "CacheManager")
        elem.set("guiclass", "CacheManagerGui")
        elem.set("testclass", "CacheManager")
        elem.set("testname", "HTTP Cache Manager")
        elem.set("enabled", "true")
        
        bool_prop = ET.SubElement(elem, "boolProp")
        bool_prop.set("name", "clearEachIteration")
        bool_prop.text = "true"
        
        bool_prop2 = ET.SubElement(elem, "boolProp")
        bool_prop2.set("name", "useExpires")
        bool_prop2.text = "true"
    
    def _create_http_request(self, parent: ET.Element, endpoint: APIEndpoint):
        elem = ET.SubElement(parent, "HTTPSamplerProxy")
        elem.set("guiclass", "HttpTestSampleGui")
        elem.set("testclass", "HTTPSamplerProxy")
        elem.set("testname", endpoint.name)
        elem.set("enabled", "true")
        
        string_prop = ET.SubElement(elem, "stringProp")
        string_prop.set("name", "HTTPSampler.domain")
        string_prop.text = ""
        
        string_prop2 = ET.SubElement(elem, "stringProp")
        string_prop2.set("name", "HTTPSampler.port")
        string_prop2.text = ""
        
        string_prop3 = ET.SubElement(elem, "stringProp")
        string_prop3.set("name", "HTTPSampler.protocol")
        string_prop3.text = ""
        
        string_prop4 = ET.SubElement(elem, "stringProp")
        string_prop4.set("name", "HTTPSampler.path")
        parsed = urlsplit(endpoint.path)
        query = urlencode(endpoint.query_params or {}, doseq=True, safe="${}")
        string_prop4.text = urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                        "&".join(value for value in (parsed.query, query) if value), ""))
        
        string_prop5 = ET.SubElement(elem, "stringProp")
        string_prop5.set("name", "HTTPSampler.method")
        string_prop5.text = endpoint.method.upper()
        
        # Query data belongs in the URL, never in the raw-body Arguments list.
        element = ET.SubElement(elem, "elementProp")
        element.set("name", "HTTPsampler.Arguments")
        element.set("elementType", "Arguments")
        element.set("guiclass", "HTTPArgumentsPanel")
        element.set("testclass", "Arguments")
        element.set("testname", "User Defined Variables")
        element.set("enabled", "true")

        collection = ET.SubElement(element, "collectionProp")
        collection.set("name", "Arguments.arguments")

        if endpoint.body is not None:
            if isinstance(endpoint.body, str):
                body_str = endpoint.body
            elif isinstance(endpoint.body, dict) and "x-www-form-urlencoded" in (endpoint.content_type or ""):
                body_str = urlencode(endpoint.body, doseq=True, safe="${}()")
            else:
                body_str = json.dumps(endpoint.body, ensure_ascii=False)
            bool_prop = ET.SubElement(elem, "boolProp")
            bool_prop.set("name", "HTTPSampler.postBodyRaw")
            bool_prop.text = "true"

            ep = ET.SubElement(collection, "elementProp")
            ep.set("name", "")
            ep.set("elementType", "HTTPArgument")

            sp = ET.SubElement(ep, "boolProp")
            sp.set("name", "HTTPArgument.always_encode")
            sp.text = "false"

            sp2 = ET.SubElement(ep, "stringProp")
            sp2.set("name", "Argument.value")
            sp2.text = body_str

            sp3 = ET.SubElement(ep, "stringProp")
            sp3.set("name", "Argument.metadata")
            sp3.text = "="

        bool_prop = ET.SubElement(elem, "boolProp")
        bool_prop.set("name", "HTTPSampler.follow_redirects")
        bool_prop.text = "true"
        
        bool_prop2 = ET.SubElement(elem, "boolProp")
        bool_prop2.set("name", "HTTPSampler.auto_redirects")
        bool_prop2.text = "false"
        
        bool_prop3 = ET.SubElement(elem, "boolProp")
        bool_prop3.set("name", "HTTPSampler.use_keepalive")
        bool_prop3.text = "true"
        
        bool_prop4 = ET.SubElement(elem, "boolProp")
        bool_prop4.set("name", "HTTPSampler.DO_MULTIPART_POST")
        bool_prop4.text = "false"
    
    def _create_response_assertion(self, parent: ET.Element, expected_status: int):
        elem = ET.SubElement(parent, "ResponseAssertion")
        elem.set("guiclass", "AssertionGui")
        elem.set("testclass", "ResponseAssertion")
        elem.set("testname", "Response Assertion")
        elem.set("enabled", "true")
        
        # "Asserion" is JMeter's own spelling. Any other name loads fine and asserts nothing.
        collection = ET.SubElement(elem, "collectionProp")
        collection.set("name", "Asserion.test_strings")

        string_prop = ET.SubElement(collection, "stringProp")
        string_prop.set("name", str(expected_status))
        string_prop.text = str(expected_status)
        
        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "Assertion.test_field")
        sp.text = "Assertion.response_code"
        
        bool_prop = ET.SubElement(elem, "boolProp")
        # The expected status decides on its own, so an expected 404 can pass.
        bool_prop.set("name", "Assertion.assume_success")
        bool_prop.text = "true"
        
        int_prop = ET.SubElement(elem, "intProp")
        int_prop.set("name", "Assertion.test_type")
        int_prop.text = "8"  # Equals
        
        string_prop = ET.SubElement(elem, "stringProp")
        string_prop.set("name", "Assertion.custom_message")
        string_prop.text = f"Expected status {expected_status}"

    def _create_json_assertion(self, parent: ET.Element, check):
        """Check the business result in the body: an HTTP 200 can still carry a failure."""
        elem = ET.SubElement(parent, "JSONPathAssertion")
        elem.set("guiclass", "JSONPathAssertionGui")
        elem.set("testclass", "JSONPathAssertion")
        elem.set("testname", f"Check: {check.json_path}")
        elem.set("enabled", "true")

        for name, value in (("JSON_PATH", check.json_path), ("EXPECTED_VALUE", check.expected or "")):
            ET.SubElement(elem, "stringProp", name=name).text = value
        for name, value in (("JSONVALIDATION", check.expected is not None), ("EXPECT_NULL", False),
                            ("INVERT", False), ("ISREGEX", False)):
            ET.SubElement(elem, "boolProp", name=name).text = "true" if value else "false"

    # ── Controllers ─────────────────────────────────────────

    def _create_once_only_controller(self, parent: ET.Element, name: str) -> ET.Element:
        elem = ET.SubElement(parent, "OnceOnlyController")
        elem.set("guiclass", "OnceOnlyControllerGui")
        elem.set("testclass", "OnceOnlyController")
        elem.set("testname", f"Once: {name}")
        elem.set("enabled", "true")
        return ET.SubElement(parent, "hashTree")

    def _create_throughput_controller(self, parent: ET.Element, name: str, percent: int) -> ET.Element:
        elem = ET.SubElement(parent, "ThroughputController")
        elem.set("guiclass", "ThroughputControllerGui")
        elem.set("testclass", "ThroughputController")
        elem.set("testname", f"{percent}%: {name}")
        elem.set("enabled", "true")
        ET.SubElement(elem, "intProp", name="ThroughputController.style").text = "1"  # Percent executions
        ET.SubElement(elem, "boolProp", name="ThroughputController.perThread").text = "false"
        ET.SubElement(elem, "intProp", name="ThroughputController.maxThroughput").text = "1"
        share = ET.SubElement(elem, "FloatProperty")
        ET.SubElement(share, "name").text = "ThroughputController.percentThroughput"
        ET.SubElement(share, "value").text = str(float(percent))
        ET.SubElement(share, "savedValue").text = "0.0"
        return ET.SubElement(parent, "hashTree")

    # ── Request Correlation: Extractors ─────────────────────

    def _create_json_extractor(self, parent: ET.Element, extraction):
        """Generate a JSON PostProcessor (JSON Path Extractor)."""
        elem = ET.SubElement(parent, "JSONPostProcessor")
        elem.set("guiclass", "JSONPostProcessorGui")
        elem.set("testclass", "JSONPostProcessor")
        elem.set("testname", f"Extract: {extraction.ref_name}")
        elem.set("enabled", "true")

        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "JSONPostProcessor.referenceNames")
        sp.text = extraction.ref_name

        sp2 = ET.SubElement(elem, "stringProp")
        sp2.set("name", "JSONPostProcessor.jsonPathExprs")
        sp2.text = extraction.expression

        sp3 = ET.SubElement(elem, "stringProp")
        sp3.set("name", "JSONPostProcessor.match_numbers")
        sp3.text = str(extraction.match_number or 1)

        sp4 = ET.SubElement(elem, "stringProp")
        sp4.set("name", "JSONPostProcessor.defaultValues")
        sp4.text = extraction.default_value or "NOT_FOUND"

    def _create_regex_extractor(self, parent: ET.Element, extraction):
        """Generate a Regex Extractor."""
        elem = ET.SubElement(parent, "RegexExtractor")
        elem.set("guiclass", "RegexExtractorGui")
        elem.set("testclass", "RegexExtractor")
        elem.set("testname", f"Extract: {extraction.ref_name}")
        elem.set("enabled", "true")

        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "RegexExtractor.useHeaders")
        sp.text = "true" if extraction.target_field == "headers" else "false"

        sp2 = ET.SubElement(elem, "stringProp")
        sp2.set("name", "RegexExtractor.refname")
        sp2.text = extraction.ref_name

        sp3 = ET.SubElement(elem, "stringProp")
        sp3.set("name", "RegexExtractor.regex")
        sp3.text = extraction.expression

        sp4 = ET.SubElement(elem, "stringProp")
        sp4.set("name", "RegexExtractor.template")
        sp4.text = extraction.template or "$1$"

        sp5 = ET.SubElement(elem, "stringProp")
        sp5.set("name", "RegexExtractor.match_number")
        sp5.text = str(extraction.match_number or 1)

        sp6 = ET.SubElement(elem, "stringProp")
        sp6.set("name", "RegexExtractor.default")
        sp6.text = extraction.default_value or "NOT_FOUND"

    def _create_boundary_extractor(self, parent: ET.Element, extraction):
        """Generate a Boundary Extractor (left/right text matching)."""
        elem = ET.SubElement(parent, "BoundaryExtractor")
        elem.set("guiclass", "BoundaryExtractorGui")
        elem.set("testclass", "BoundaryExtractor")
        elem.set("testname", f"Extract: {extraction.ref_name}")
        elem.set("enabled", "true")

        # expression format: "left=|right="
        left, right = "", ""
        if "|" in (extraction.expression or ""):
            parts = extraction.expression.split("|", 1)
            left = parts[0].replace("left=", "") if "left=" in parts[0] else parts[0]
            right = parts[1].replace("right=", "") if "right=" in parts[1] else parts[1]

        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "BoundaryExtractor.refname")
        sp.text = extraction.ref_name

        sp2 = ET.SubElement(elem, "stringProp")
        sp2.set("name", "BoundaryExtractor.lboundary")
        sp2.text = left

        sp3 = ET.SubElement(elem, "stringProp")
        sp3.set("name", "BoundaryExtractor.rboundary")
        sp3.text = right

        sp4 = ET.SubElement(elem, "stringProp")
        sp4.set("name", "BoundaryExtractor.match_number")
        sp4.text = str(extraction.match_number or 1)

        sp5 = ET.SubElement(elem, "stringProp")
        sp5.set("name", "BoundaryExtractor.default")
        sp5.text = extraction.default_value or "NOT_FOUND"

    def _create_xpath_extractor(self, parent: ET.Element, extraction):
        """Generate an XPath2 Extractor."""
        elem = ET.SubElement(parent, "XPath2Extractor")
        elem.set("guiclass", "XPath2ExtractorGui")
        elem.set("testclass", "XPath2Extractor")
        elem.set("testname", f"Extract: {extraction.ref_name}")
        elem.set("enabled", "true")

        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "XPath2Extractor.refname")
        sp.text = extraction.ref_name

        sp2 = ET.SubElement(elem, "stringProp")
        sp2.set("name", "XPath2Extractor.xpathQuery")
        sp2.text = extraction.expression

        sp3 = ET.SubElement(elem, "stringProp")
        sp3.set("name", "XPath2Extractor.default")
        sp3.text = extraction.default_value or "NOT_FOUND"

        sp4 = ET.SubElement(elem, "stringProp")
        sp4.set("name", "XPath2Extractor.matchNumber")
        sp4.text = str(extraction.match_number or 1)

    def _create_header_extractor(self, parent: ET.Element, extraction):
        """Extract a value from a response header (uses Regex Extractor in header mode)."""
        extraction.target_field = "headers"
        extraction.expression = extraction.expression or "(.+)"
        extraction.template = "$1$"
        extraction.match_number = 1
        self._create_regex_extractor(parent, extraction)

    # ── Request Correlation: Binding Resolution ─────────────

    def _compute_common_headers(self, endpoints) -> dict:
        """Return headers that are present with identical values in ALL endpoints."""
        if not endpoints:
            return {}
        common = dict(endpoints[0].headers or {})
        for ep in endpoints[1:]:
            ep_headers = ep.headers or {}
            common = {
                k: v for k, v in common.items()
                if k in ep_headers and ep_headers[k] == v
            }
            if not common:
                break
        # Content-Type is needed for raw JSON bodies; keep it only if it is common.
        return common

    def _topological_sort(self, endpoints) -> list:
        """Sort endpoints so dependencies come before dependents (Kahn's algorithm).

        Endpoints with no unresolved `depends_on` are placed first. Remaining
        endpoints (cycle victims) are appended at the end to avoid infinite loops.
        """
        if not endpoints:
            return []

        ep_map = {ep.name: ep for ep in endpoints}
        # name → index for stable ordering tie-breaker
        name_to_order = {
            ep.name: ep.order_hint if ep.order_hint is not None else i
            for i, ep in enumerate(endpoints)
        }

        # Build dependency graph
        in_degree = {ep.name: 0 for ep in endpoints}
        dependents = {ep.name: [] for ep in endpoints}
        for ep in endpoints:
            for dep in (ep.depends_on or []):
                if dep in ep_map:
                    in_degree[ep.name] += 1
                    dependents[dep].append(ep.name)

        # Start with endpoints that have no dependencies
        queue = sorted(
            [ep for ep in endpoints if in_degree[ep.name] == 0],
            key=lambda e: name_to_order.get(e.name, 999),
        )
        result = []
        placed = set()

        while queue:
            ep = queue.pop(0)
            result.append(ep)
            placed.add(ep.name)
            for dep_name in dependents.get(ep.name, []):
                in_degree[dep_name] -= 1
                if in_degree[dep_name] == 0 and dep_name not in placed:
                    queue.append(ep_map[dep_name])
            queue.sort(key=lambda e: name_to_order.get(e.name, 999))

        # Append any remaining endpoints (cycle detected); keep deterministic order
        remaining = sorted(
            [ep for ep in endpoints if ep.name not in placed],
            key=lambda e: name_to_order.get(e.name, 999),
        )
        if remaining:
            import logging
            logging.getLogger(__name__).warning(
                "Circular dependency detected among endpoints: %s",
                [ep.name for ep in remaining]
            )
            result.extend(remaining)

        return result

    def _resolve_bindings(self, endpoint):
        """Return a modified endpoint with variable references injected.

        For headers and body that contain `${variable_name}`, the values
        are already in the correct format. This method ensures binding
        values use proper template format.
        """
        # Always a copy: the caller rewrites the path, and a load shape adds an endpoint many times.
        resolved = endpoint.model_copy(deep=True)
        if not endpoint.bindings:
            return resolved

        for binding in endpoint.bindings:
            var_name = binding.variable_name
            if binding.template:
                var_ref = binding.template
            else:
                var_ref = f"${{{var_name}}}"

            # Inject into the right location
            target = binding.target_location
            key = binding.target_key

            if target == "header":
                if resolved.headers is None:
                    resolved.headers = {}
                resolved.headers[key] = var_ref
            elif target == "query":
                if resolved.query_params is None:
                    resolved.query_params = {}
                resolved.query_params[key] = var_ref
            elif target == "body":
                if resolved.body is None:
                    resolved.body = {}
                if key:
                    if not isinstance(resolved.body, dict):
                        raise ValueError(f"{endpoint.name}: body field binding needs a JSON object body")
                    resolved.body[key] = var_ref
                else:
                    # Replace entire body with template
                    resolved.body = var_ref
            elif target == "path":
                # Replace placeholder in path, e.g. /users/{id} → /users/${id}. A path that already
                # says ${id} is done; replacing inside it would leave a stray $ in the URL.
                resolved.path = re.sub(r"(?<!\$)\{" + re.escape(key) + r"\}", lambda _: var_ref, resolved.path)
            elif target == "url":
                # Template replaces the entire URL construction
                pass  # handled by the template itself in headers

        return resolved

    def _resolve_header_bindings(self, endpoint) -> dict:
        """Return per-endpoint headers that include variable-based auth tokens."""
        if not endpoint.bindings:
            return endpoint.headers or {}

        headers = dict(endpoint.headers or {})
        for binding in endpoint.bindings:
            if binding.target_location == "header":
                var_name = binding.variable_name
                key = binding.target_key or "Authorization"
                if binding.template:
                    headers[key] = binding.template
                else:
                    headers[key] = f"${{{var_name}}}"
        return headers
    
    # ── Timers ─────────────────────────────────────────────

    def _create_constant_timer(self, parent: ET.Element, delay_ms: int):
        """Generate a Constant Timer (think time between requests)."""
        if delay_ms <= 0:
            return
        elem = ET.SubElement(parent, "ConstantTimer")
        elem.set("guiclass", "ConstantTimerGui")
        elem.set("testclass", "ConstantTimer")
        elem.set("testname", f"Think Time: {delay_ms}ms")
        elem.set("enabled", "true")
        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "ConstantTimer.delay")
        sp.text = str(delay_ms)

    def _create_uniform_random_timer(self, parent: ET.Element, min_ms: int, max_ms: int):
        """Generate a Uniform Random Timer for realistic think times."""
        elem = ET.SubElement(parent, "UniformRandomTimer")
        elem.set("guiclass", "UniformRandomTimerGui")
        elem.set("testclass", "UniformRandomTimer")
        elem.set("testname", f"Think Time: {min_ms}-{max_ms}ms")
        elem.set("enabled", "true")
        ET.SubElement(elem, "stringProp", name="ConstantTimer.delay").text = str(min_ms)
        ET.SubElement(elem, "stringProp", name="RandomTimer.range").text = str(float(max_ms - min_ms))

    def _create_stop_user_on_error(self, parent: ET.Element):
        elem = ET.SubElement(parent, "ResultAction")
        elem.set("guiclass", "ResultActionGui")
        elem.set("testclass", "ResultAction")
        elem.set("testname", "Stop this user if the request failed")
        elem.set("enabled", "true")
        ET.SubElement(elem, "intProp", name="OnError.action").text = "1"  # Stop the thread

    # ── CSV DataSet Config ──────────────────────────────────

    def _create_csv_dataset(self, parent: ET.Element, filename: str, variable_names: str,
                            delimiter: str = ",", recycle: bool = True,
                            stop_thread: bool = False, sharing_mode: str = "all"):
        """Generate a CSV DataSet Config for data-driven testing.

        Args:
            filename: CSV file name (path relative to test plan).
            variable_names: Comma-separated variable names.
            delimiter: CSV delimiter.
            recycle: Whether to recycle the file when EOF reached.
            stop_thread: Whether to stop thread on EOF.
            sharing_mode: all/current_thread/current_thread_group.
        """
        elem = ET.SubElement(parent, "CSVDataSet")
        elem.set("guiclass", "TestBeanGUI")
        elem.set("testclass", "CSVDataSet")
        elem.set("testname", f"CSV Data: {filename}")
        elem.set("enabled", "true")

        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "filename")
        sp.text = filename

        sp2 = ET.SubElement(elem, "stringProp")
        sp2.set("name", "fileEncoding")
        sp2.text = "UTF-8"

        sp3 = ET.SubElement(elem, "stringProp")
        sp3.set("name", "variableNames")
        sp3.text = variable_names

        sp4 = ET.SubElement(elem, "stringProp")
        sp4.set("name", "delimiter")
        sp4.text = delimiter

        sp5 = ET.SubElement(elem, "stringProp")
        sp5.set("name", "quotedData")
        sp5.text = "false"

        sp6 = ET.SubElement(elem, "stringProp")
        sp6.set("name", "recycle")
        sp6.text = "true" if recycle else "false"

        sp7 = ET.SubElement(elem, "stringProp")
        sp7.set("name", "stopThread")
        sp7.text = "true" if stop_thread else "false"

        sp8 = ET.SubElement(elem, "stringProp")
        sp8.set("name", "shareMode")
        share_map = {"all": "shareMode.all", "current_thread": "shareMode.thread",
                     "current_thread_group": "shareMode.group"}
        sp8.text = share_map.get(sharing_mode, "shareMode.all")

    # ── Transaction Controller ──────────────────────────────

    def _create_transaction_controller(self, parent: ET.Element, name: str) -> ET.Element:
        """Generate a Transaction Controller that groups requests into a logical transaction.

        Returns the hashTree element where children should be placed.
        """
        elem = ET.SubElement(parent, "TransactionController")
        elem.set("guiclass", "TransactionControllerGui")
        elem.set("testclass", "TransactionController")
        elem.set("testname", name)
        elem.set("enabled", "true")

        sp = ET.SubElement(elem, "boolProp")
        sp.set("name", "TransactionController.includeTimers")
        sp.text = "false"

        sp2 = ET.SubElement(elem, "boolProp")
        sp2.set("name", "TransactionController.parent")
        sp2.text = "true"  # Generate parent sample

        tc_hash = ET.SubElement(parent, "hashTree")
        return tc_hash

    # ── JDBC Sampler ────────────────────────────────────────

    def _create_jdbc_request(self, parent: ET.Element, query: str, data_source: str = "jdbcConfig",
                             query_type: str = "Select Statement", result_variable: str = ""):
        """Generate a JDBC Request sampler for database performance testing.

        Args:
            query: SQL query to execute.
            data_source: Name of the JDBC Connection Configuration element.
            query_type: Select Statement / Update Statement / Prepared Select / etc.
            result_variable: Variable name to store query result.
        """
        elem = ET.SubElement(parent, "JDBCSampler")
        elem.set("guiclass", "TestBeanGUI")
        elem.set("testclass", "JDBCSampler")
        elem.set("testname", f"JDBC: {query[:60]}")
        elem.set("enabled", "true")

        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "dataSource")
        sp.text = data_source

        sp2 = ET.SubElement(elem, "stringProp")
        sp2.set("name", "queryType")
        sp2.text = query_type

        sp3 = ET.SubElement(elem, "stringProp")
        sp3.set("name", "query")
        sp3.text = query

        sp4 = ET.SubElement(elem, "stringProp")
        sp4.set("name", "resultVariable")
        sp4.text = result_variable

        sp5 = ET.SubElement(elem, "stringProp")
        sp5.set("name", "queryTimeout")
        sp5.text = ""

        sp6 = ET.SubElement(elem, "stringProp")
        sp6.set("name", "resultSetMaxRows")
        sp6.text = ""

    # ── JDBC Connection Configuration ─────────────────────────

    def _create_jdbc_config(self, parent: ET.Element, name: str = "jdbcConfig",
                            pool_max: int = 10, driver: str = "com.mysql.cj.jdbc.Driver",
                            url: str = "jdbc:mysql://localhost:3306/test",
                            username: str = "", password: str = "",
                            validation_query: str = "SELECT 1"):
        """Generate a JDBC Connection Configuration element."""
        elem = ET.SubElement(parent, "JDBCDataSource")
        elem.set("guiclass", "TestBeanGUI")
        elem.set("testclass", "JDBCDataSource")
        elem.set("testname", name)
        elem.set("enabled", "true")

        props = [
            ("dataSource", name),
            ("driver", driver),
            ("dbUrl", url),
            ("username", username),
            ("password", password),
            ("poolMax", str(pool_max)),
            ("timeout", "10000"),
            ("connectionAge", "5000"),
            ("trimInterval", "60000"),
            ("validationQuery", validation_query),
        ]
        for pname, pvalue in props:
            sp = ET.SubElement(elem, "stringProp")
            sp.set("name", pname)
            sp.text = pvalue

    # ── JMS Samplers (Point-to-Point + Publisher/Subscriber) ──

    def _create_jms_point_to_point(self, parent: ET.Element, name: str = "JMS P2P",
                                    jndi_initial_factory: str = "",
                                    jndi_provider_url: str = "",
                                    connection_factory: str = "",
                                    queue_name: str = "",
                                    message: str = "",
                                    jms_properties: dict = None):
        """Generate a JMS Point-to-Point sampler for message queue testing.

        Simulates a producer sending messages to a JMS queue (ActiveMQ, RabbitMQ JMS, IBM MQ, etc.).
        """
        elem = ET.SubElement(parent, "JMSSampler")
        elem.set("guiclass", "JMSSamplerGui")
        elem.set("testclass", "JMSSampler")
        elem.set("testname", name)
        elem.set("enabled", "true")

        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "JMSSampler.initialContextFactory")
        sp.text = jndi_initial_factory or "org.apache.activemq.jndi.ActiveMQInitialContextFactory"

        sp2 = ET.SubElement(elem, "stringProp")
        sp2.set("name", "JMSSampler.contextProviderUrl")
        sp2.text = jndi_provider_url or "tcp://localhost:61616"

        sp3 = ET.SubElement(elem, "stringProp")
        sp3.set("name", "JMSSampler.jndiProperties")
        sp3.text = ""

        sp4 = ET.SubElement(elem, "stringProp")
        sp4.set("name", "JMSSampler.queueConnectionFactory")
        sp4.text = connection_factory or "ConnectionFactory"

        sp5 = ET.SubElement(elem, "stringProp")
        sp5.set("name", "JMSSampler.sendQueue")
        sp5.text = queue_name

        sp6 = ET.SubElement(elem, "stringProp")
        sp6.set("name", "JMSSampler.xmlMessage")
        sp6.text = message

        sp7 = ET.SubElement(elem, "stringProp")
        sp7.set("name", "JMSSampler.communicationStyle")
        sp7.text = "request"  # request / one_way

        sp8 = ET.SubElement(elem, "stringProp")
        sp8.set("name", "JMSSampler.jmsSelector")
        sp8.text = ""

        sp9 = ET.SubElement(elem, "stringProp")
        sp9.set("name", "JMSSampler.contentType")
        sp9.text = "text/plain"

        # JMS Properties collection
        if jms_properties:
            collection = ET.SubElement(elem, "collectionProp")
            collection.set("name", "JMSSampler.JMSProperties")
            for prop_name, prop_value in jms_properties.items():
                ep = ET.SubElement(collection, "elementProp")
                ep.set("name", prop_name)
                ep.set("elementType", "JMSProperty")
                sp_n = ET.SubElement(ep, "stringProp")
                sp_n.set("name", "name")
                sp_n.text = str(prop_name)
                sp_v = ET.SubElement(ep, "stringProp")
                sp_v.set("name", "value")
                sp_v.text = str(prop_value)

    def _create_jms_subscriber(self, parent: ET.Element, name: str = "JMS Subscriber",
                                jndi_initial_factory: str = "",
                                jndi_provider_url: str = "",
                                connection_factory: str = "",
                                topic_name: str = "",
                                timeout_ms: str = "2000"):
        """Generate a JMS Subscriber sampler for topic-based messaging.

        Reads messages from a JMS topic. Use with JMS Publisher for full
        pub/sub performance testing.
        """
        elem = ET.SubElement(parent, "SubscriberSampler")
        elem.set("guiclass", "SubscriberSamplerGui")
        elem.set("testclass", "SubscriberSampler")
        elem.set("testname", name)
        elem.set("enabled", "true")

        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "SubscriberSampler.initialContextFactory")
        sp.text = jndi_initial_factory or "org.apache.activemq.jndi.ActiveMQInitialContextFactory"

        sp2 = ET.SubElement(elem, "stringProp")
        sp2.set("name", "SubscriberSampler.contextProviderUrl")
        sp2.text = jndi_provider_url or "tcp://localhost:61616"

        sp3 = ET.SubElement(elem, "stringProp")
        sp3.set("name", "SubscriberSampler.topicConnectionFactory")
        sp3.text = connection_factory or "ConnectionFactory"

        sp4 = ET.SubElement(elem, "stringProp")
        sp4.set("name", "SubscriberSampler.topic")
        sp4.text = topic_name

        sp5 = ET.SubElement(elem, "stringProp")
        sp5.set("name", "SubscriberSampler.timeout")
        sp5.text = timeout_ms

        sp6 = ET.SubElement(elem, "stringProp")
        sp6.set("name", "SubscriberSampler.jmsSelector")
        sp6.text = ""

        bool_p = ET.SubElement(elem, "boolProp")
        bool_p.set("name", "SubscriberSampler.useJNDIProperties")
        bool_p.text = "true"

        bool_p2 = ET.SubElement(elem, "boolProp")
        bool_p2.set("name", "SubscriberSampler.readResponse")
        bool_p2.text = "true"

    # ── TCP Sampler ───────────────────────────────────────────

    def _create_tcp_sampler(self, parent: ET.Element, name: str = "TCP Request",
                            server: str = "localhost", port: str = "8080",
                            message: str = "", timeout_ms: str = "2000",
                            end_of_line_byte: str = "", close_connection: bool = False,
                            reuse_connection: bool = True):
        """Generate a TCP Sampler for raw socket/packet testing.

        Use for: Redis, Memcached, custom TCP protocols, game servers,
        IoT/MQTT gateways, etc.
        """
        elem = ET.SubElement(parent, "TCPSampler")
        elem.set("guiclass", "TCPSamplerGui")
        elem.set("testclass", "TCPSampler")
        elem.set("testname", name)
        elem.set("enabled", "true")

        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "TCPSampler.server")
        sp.text = server

        sp2 = ET.SubElement(elem, "stringProp")
        sp2.set("name", "TCPSampler.port")
        sp2.text = port

        sp3 = ET.SubElement(elem, "stringProp")
        sp3.set("name", "TCPSampler.classname")
        sp3.text = "TCPClientImpl"  # TCPClientImpl / BinaryTCPClientImpl / LengthPrefixedBinaryTCPClientImpl

        sp4 = ET.SubElement(elem, "stringProp")
        sp4.set("name", "TCPSampler.timeout")
        sp4.text = timeout_ms

        sp5 = ET.SubElement(elem, "stringProp")
        sp5.set("name", "TCPSampler.requestData")
        sp5.text = message

        sp6 = ET.SubElement(elem, "stringProp")
        sp6.set("name", "TCPSampler.eolByte")
        sp6.text = end_of_line_byte  # e.g. "10" for newline

        bool_p = ET.SubElement(elem, "boolProp")
        bool_p.set("name", "TCPSampler.closeConnection")
        bool_p.text = "true" if close_connection else "false"

        bool_p2 = ET.SubElement(elem, "boolProp")
        bool_p2.set("name", "TCPSampler.reUseConnection")
        bool_p2.text = "true" if reuse_connection else "false"

        bool_p3 = ET.SubElement(elem, "boolProp")
        bool_p3.set("name", "TCPSampler.nodelay")
        bool_p3.text = "true"  # Disable Nagle's algorithm

    # ── JSR223 Sampler (scriptable sampler) ────────────────────

    def _create_jsr223_sampler(self, parent: ET.Element, name: str = "JSR223 Script",
                                script_language: str = "groovy",
                                script: str = "",
                                parameters: str = "",
                                cache_key: bool = True):
        """Generate a JSR223 Sampler for custom scripting.

        Supported languages: groovy (default), javascript, python/jython, beanshell.

        Use for:
        - Custom pre/post processing logic
        - Complex assertions that require scripting
        - Dynamic test data generation
        - Custom protocol clients (gRPC, WebSocket, etc.)
        - Integration with external libraries
        """
        elem = ET.SubElement(parent, "JSR223Sampler")
        elem.set("guiclass", "TestBeanGUI")
        elem.set("testclass", "JSR223Sampler")
        elem.set("testname", name)
        elem.set("enabled", "true")

        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "scriptLanguage")
        sp.text = script_language

        sp2 = ET.SubElement(elem, "stringProp")
        sp2.set("name", "parameters")
        sp2.text = parameters

        sp3 = ET.SubElement(elem, "stringProp")
        sp3.set("name", "filename")
        sp3.text = ""

        sp4 = ET.SubElement(elem, "stringProp")
        sp4.set("name", "cacheKey")
        sp4.text = "true" if cache_key else "false"

        sp5 = ET.SubElement(elem, "stringProp")
        sp5.set("name", "script")
        sp5.text = script

    # ── JSR223 PreProcessor / PostProcessor ────────────────────

    def _create_jsr223_preprocessor(self, parent: ET.Element, name: str = "JSR223 PreProcessor",
                                     script_language: str = "groovy", script: str = ""):
        """Generate a JSR223 PreProcessor for scripted request modification."""
        elem = ET.SubElement(parent, "JSR223PreProcessor")
        elem.set("guiclass", "TestBeanGUI")
        elem.set("testclass", "JSR223PreProcessor")
        elem.set("testname", name)
        elem.set("enabled", "true")

        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "scriptLanguage")
        sp.text = script_language

        sp2 = ET.SubElement(elem, "stringProp")
        sp2.set("name", "parameters")
        sp2.text = ""

        sp3 = ET.SubElement(elem, "stringProp")
        sp3.set("name", "filename")
        sp3.text = ""

        sp4 = ET.SubElement(elem, "stringProp")
        sp4.set("name", "cacheKey")
        sp4.text = "true"

        sp5 = ET.SubElement(elem, "stringProp")
        sp5.set("name", "script")
        sp5.text = script

    def _create_jsr223_postprocessor(self, parent: ET.Element, name: str = "JSR223 PostProcessor",
                                      script_language: str = "groovy", script: str = ""):
        """Generate a JSR223 PostProcessor for scripted response handling."""
        elem = ET.SubElement(parent, "JSR223PostProcessor")
        elem.set("guiclass", "TestBeanGUI")
        elem.set("testclass", "JSR223PostProcessor")
        elem.set("testname", name)
        elem.set("enabled", "true")

        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "scriptLanguage")
        sp.text = script_language

        sp2 = ET.SubElement(elem, "stringProp")
        sp2.set("name", "parameters")
        sp2.text = ""

        sp3 = ET.SubElement(elem, "stringProp")
        sp3.set("name", "filename")
        sp3.text = ""

        sp4 = ET.SubElement(elem, "stringProp")
        sp4.set("name", "cacheKey")
        sp4.text = "true"

        sp5 = ET.SubElement(elem, "stringProp")
        sp5.set("name", "script")
        sp5.text = script

    # ── Backend Listener (InfluxDB / Prometheus) ───────────────

    def _create_backend_listener(self, parent: ET.Element,
                                  backend_class: str = "org.apache.jmeter.visualizers.backend.influxdb.InfluxdbBackendListenerClient",
                                  influxdb_url: str = "http://localhost:8086",
                                  influxdb_db: str = "jmeter",
                                  influxdb_measurement: str = "jmeter",
                                  influxdb_user: str = "", influxdb_password: str = "",
                                  application_name: str = "JMeter Test",
                                  metrics_interval: str = "5"):
        """Generate a Backend Listener for sending metrics to InfluxDB/Grafana/Prometheus.

        Supported backends:
        - InfluxDB (org.apache.jmeter.visualizers.backend.influxdb.InfluxdbBackendListenerClient)
        - Graphite (org.apache.jmeter.visualizers.backend.graphite.GraphiteBackendListenerClient)
        - Prometheus requires InfluxDB as intermediary or custom backend
        """
        elem = ET.SubElement(parent, "BackendListener")
        elem.set("guiclass", "BackendListenerGui")
        elem.set("testclass", "BackendListener")
        elem.set("testname", f"Backend: {backend_class.split('.')[-1]}")
        elem.set("enabled", "true")

        sp = ET.SubElement(elem, "stringProp")
        sp.set("name", "classname")
        sp.text = backend_class

        # Arguments collection for backend
        args_elem = ET.SubElement(elem, "elementProp")
        args_elem.set("name", "arguments")
        args_elem.set("elementType", "Arguments")
        args_elem.set("guiclass", "ArgumentsPanel")
        args_elem.set("testclass", "Arguments")
        args_elem.set("enabled", "true")

        collection = ET.SubElement(args_elem, "collectionProp")
        collection.set("name", "Arguments.arguments")

        backend_args = [
            ("influxdbUrl", influxdb_url),
            ("influxdbDB", influxdb_db),
            ("measurementName", influxdb_measurement),
            ("influxdbUser", influxdb_user),
            ("influxdbPassword", influxdb_password),
            ("application", application_name),
            ("summaryOnly", "false"),
            ("samplersRegex", ".*"),
            ("percentiles", "90;95;99"),
            ("testTitle", application_name),
            ("eventTags", ""),
        ]

        for arg_name, arg_value in backend_args:
            ep = ET.SubElement(collection, "elementProp")
            ep.set("name", arg_name)
            ep.set("elementType", "Argument")
            sp_n = ET.SubElement(ep, "stringProp")
            sp_n.set("name", "Argument.name")
            sp_n.text = arg_name
            sp_v = ET.SubElement(ep, "stringProp")
            sp_v.set("name", "Argument.value")
            sp_v.text = arg_value

    # ── Listeners ───────────────────────────────────────────

    def _create_listeners(self, parent: ET.Element):
        # View Results Tree
        elem1 = ET.SubElement(parent, "ResultCollector")
        elem1.set("guiclass", "ViewResultsFullVisualizer")
        elem1.set("testclass", "ResultCollector")
        elem1.set("testname", "View Results Tree")
        elem1.set("enabled", "true")
        
        bool_prop = ET.SubElement(elem1, "boolProp")
        bool_prop.set("name", "ResultCollector.error_logging")
        bool_prop.text = "false"
        ET.SubElement(parent, "hashTree")
        
        # Summary Report
        elem2 = ET.SubElement(parent, "ResultCollector")
        elem2.set("guiclass", "SummaryReport")
        elem2.set("testclass", "ResultCollector")
        elem2.set("testname", "Summary Report")
        elem2.set("enabled", "true")
        
        bool_prop2 = ET.SubElement(elem2, "boolProp")
        bool_prop2.set("name", "ResultCollector.error_logging")
        bool_prop2.text = "false"
        ET.SubElement(parent, "hashTree")
        
        # Aggregate Report
        elem3 = ET.SubElement(parent, "ResultCollector")
        elem3.set("guiclass", "StatVisualizer")
        elem3.set("testclass", "ResultCollector")
        elem3.set("testname", "Aggregate Report")
        elem3.set("enabled", "true")
        
        bool_prop3 = ET.SubElement(elem3, "boolProp")
        bool_prop3.set("name", "ResultCollector.error_logging")
        bool_prop3.text = "false"
        ET.SubElement(parent, "hashTree")
    
    def save_jmx(self, content: str, name: Optional[str] = None,
                 project_id: Optional[str] = None, plan_id: Optional[str] = None) -> str:
        """保存JMX文件，返回文件路径。

        Args:
            content: JMX XML content.
            name: Descriptive name prefix (e.g. 'baseline_test').
            project_id: Project ID for directory structure.
            plan_id: Explicit plan ID to use as filename (e.g. 'd3ee03d4').
                     When provided, filename is '{plan_id}.jmx'.
                     When omitted, generates '{name}_{random_id}.jmx'.
        """
        from app.services.execution_lifecycle import validate_id
        project_id = validate_id(project_id or 'default')
        if plan_id:
            validate_id(plan_id)
            filename = f"{plan_id}.jmx"
        else:
            test_id = str(uuid.uuid4())[:8]
            filename = f"{name or 'test_plan'}_{test_id}.jmx"

        project_dir = os.path.join(settings.TEST_PLANS_DIR, project_id)
        filepath = os.path.join(project_dir, filename)
        if os.path.dirname(os.path.realpath(filepath)) != os.path.realpath(project_dir):
            raise ValueError('JMX filename must remain within its project')
        os.makedirs(project_dir, exist_ok=True)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return filepath
