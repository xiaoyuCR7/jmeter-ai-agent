"""JMX XML 构建器：将 JSON 树结构构建为 .jmx 文件"""
import xml.etree.ElementTree as ET
from xml.dom import minidom
from typing import Any, Dict

from app.services.jmeter.jmx_parser import ROOT_ATTRS_KEY, SOURCE_KEY

THREAD_GROUP_CLASSES = {"ThreadGroup", "SetupThreadGroup", "PostThreadGroup"}
HTTP_SAMPLER_CLASSES = {"HTTPSamplerProxy", "HTTPSampler"}

LISTENER_TYPE_LABELS = {
    "ViewResultsFullVisualizer": "View Results Tree",
    "SummaryReport": "Summary Report",
    "StatVisualizer": "Aggregate Report",
    "GraphVisualizer": "Graph Results",
    "RespTimeGraphVisualizer": "Response Time Graph",
    "TableVisualizer": "View Results in Table",
    "SimpleDataWriter": "Simple Data Writer",
    "AssertionVisualizer": "Assertion Results",
    "MailerVisualizer": "Mailer Visualizer",
    "BackendListenerGui": "Backend Listener",
}


# Properties the parser turns into simpler editing fields, and the fields each one comes from.
DERIVED_PROPS = {
    "ThreadGroup.main_controller": ("loops",),
    "HTTPsampler.Arguments": ("__body_data", "__http_params", "HTTPSampler.postBodyRaw"),
}
DERIVED_FIELDS = {"loops", "__body_data", "__http_params"}
_MISSING = object()


def build_jmx(tree: Dict[str, Any]) -> str:
    """将 JSON 树构建为 JMX XML 字符串"""
    root = ET.Element("jmeterTestPlan")
    root.set("version", "1.2")
    root.set("properties", "5.0")
    root.set("jmeter", "5.6.3")
    for key, value in (tree.get("properties", {}).get(ROOT_ATTRS_KEY) or {}).items():
        root.set(key, value)

    hash_tree = ET.SubElement(root, "hashTree")
    _build_node(hash_tree, tree)

    rough_string = ET.tostring(root, encoding="unicode")
    reparsed = minidom.parseString(rough_string)
    pretty = reparsed.toprettyxml(indent="  ", encoding="UTF-8")
    lines = [line for line in pretty.decode("utf-8").split("\n") if line.strip()]
    return "\n".join(lines) + "\n"


def _build_node(parent_hash_tree: ET.Element, node: Dict[str, Any]) -> None:
    """将一个 JSON 节点及其子节点添加到父 hashTree 中"""
    testclass = node.get("testclass", "TestPlan")
    if testclass == "HTTPSampler":
        testclass = "HTTPSamplerProxy"
    testname = node.get("testname", "")
    enabled = "true" if node.get("enabled", True) else "false"

    properties = dict(node.get("properties", {}))
    source = properties.pop(SOURCE_KEY, None)
    if not (isinstance(source, dict) and isinstance(source.get("props"), list)
            and isinstance(source.get("snapshot"), dict)):
        source = None
    listener_type = properties.pop("_listener_type", None) if testclass == "ResultCollector" else None

    elem = ET.SubElement(parent_hash_tree, testclass)
    elem.set("guiclass", listener_type or (source and source.get("guiclass")) or _get_guiclass(testclass, node))
    elem.set("testclass", testclass)
    elem.set("testname", testname)
    elem.set("enabled", enabled)

    if source:
        _build_from_source(elem, testclass, properties, source)
    else:
        _build_fresh(elem, testclass, properties)

    children = node.get("children", [])
    if children:
        child_hash_tree = ET.SubElement(parent_hash_tree, "hashTree")
        for child in children:
            _build_node(child_hash_tree, child)
    else:
        ET.SubElement(parent_hash_tree, "hashTree")


def _build_fresh(elem: ET.Element, testclass: str, properties: Dict[str, Any]) -> None:
    if testclass in THREAD_GROUP_CLASSES:
        _build_thread_group_properties(elem, properties)

    if testclass in HTTP_SAMPLER_CLASSES:
        _build_http_sampler_properties(elem, properties)

    for prop_name, prop_value in properties.items():
        if prop_name.startswith("__"):
            continue
        _build_property(elem, prop_name, prop_value)


def _build_from_source(elem: ET.Element, testclass: str, properties: Dict[str, Any], source: Dict[str, Any]) -> None:
    """Write each property back from the original XML unless it was edited, so
    attributes and types the JSON form cannot express (guiclass, intProp, objProp...) survive."""
    snapshot = source["snapshot"]

    def unchanged(key: str) -> bool:
        return properties.get(key, _MISSING) == snapshot.get(key, _MISSING)

    def rebuild_derived(name: str) -> None:
        if name == "ThreadGroup.main_controller" and testclass in THREAD_GROUP_CLASSES:
            _build_thread_group_properties(elem, dict(properties))
        elif name == "HTTPsampler.Arguments" and testclass in HTTP_SAMPLER_CLASSES:
            _build_http_sampler_properties(elem, dict(properties))

    written = set()
    for name, xml in source["props"]:
        if name in DERIVED_PROPS:
            written.add(name)
            if all(unchanged(k) for k in DERIVED_PROPS[name]):
                elem.append(ET.fromstring(xml))
            else:
                rebuild_derived(name)
        elif not name or (name in properties and unchanged(name)):
            elem.append(ET.fromstring(xml))
            written.add(name)
        elif name in properties:
            _build_property(elem, name, properties[name])
            written.add(name)
        # A property the user deleted is simply not written.

    for name, fields in DERIVED_PROPS.items():
        if name not in written and not all(unchanged(k) for k in fields):
            rebuild_derived(name)

    for name, value in properties.items():
        if name in written or name.startswith("__") or name in DERIVED_FIELDS:
            continue
        _build_property(elem, name, value)


def _build_thread_group_properties(elem: ET.Element, properties: Dict[str, Any]) -> None:
    loops = properties.pop("loops", 1)
    continue_forever = loops == -1 or loops == "-1"

    loop_ctrl = ET.SubElement(elem, "elementProp")
    loop_ctrl.set("name", "ThreadGroup.main_controller")
    loop_ctrl.set("elementType", "LoopController")
    loop_ctrl.set("guiclass", "LoopControlPanel")
    loop_ctrl.set("testclass", "LoopController")
    loop_ctrl.set("testname", "Loop Controller")
    loop_ctrl.set("enabled", "true")

    cf = ET.SubElement(loop_ctrl, "boolProp")
    cf.set("name", "LoopController.continue_forever")
    cf.text = "true" if continue_forever else "false"

    lp = ET.SubElement(loop_ctrl, "stringProp")
    lp.set("name", "LoopController.loops")
    lp.text = str(loops) if not continue_forever else "1"


def _build_http_sampler_properties(elem: ET.Element, properties: Dict[str, Any]) -> None:
    body_data = properties.pop("__body_data", None)
    http_params = properties.pop("__http_params", None)
    post_body_raw = properties.get("HTTPSampler.postBodyRaw", False)

    args_elem = ET.SubElement(elem, "elementProp")
    args_elem.set("name", "HTTPsampler.Arguments")
    args_elem.set("elementType", "Arguments")
    args_elem.set("guiclass", "HTTPArgumentsPanel")
    args_elem.set("testclass", "Arguments")
    args_elem.set("testname", "")
    args_elem.set("enabled", "true")

    coll_elem = ET.SubElement(args_elem, "collectionProp")
    coll_elem.set("name", "Arguments.arguments")

    if post_body_raw and body_data is not None:
        _build_http_argument(coll_elem, {
            "Argument.name": "",
            "Argument.value": body_data,
            "Argument.metadata": "=",
            "HTTPArgument.always_encode": False,
        })
    elif http_params:
        for param in http_params:
            _build_http_argument(coll_elem, param)


def _build_http_argument(parent: ET.Element, param: Dict[str, Any]) -> None:
    arg_elem = ET.SubElement(parent, "elementProp")
    arg_elem.set("name", param.get("Argument.name", param.get("_name", "")))
    arg_elem.set("elementType", "HTTPArgument")

    for key in ("HTTPArgument.always_encode", "HTTPArgument.use_equals"):
        if key in param:
            bool_elem = ET.SubElement(arg_elem, "boolProp")
            bool_elem.set("name", key)
            bool_elem.text = "true" if param[key] else "false"

    for key in ("Argument.value", "Argument.metadata", "HTTPArgument.content_type"):
        if key in param:
            str_elem = ET.SubElement(arg_elem, "stringProp")
            str_elem.set("name", key)
            str_elem.text = str(param[key]) if param[key] is not None else ""

    if param.get("Argument.name"):
        name_elem = ET.SubElement(arg_elem, "stringProp")
        name_elem.set("name", "Argument.name")
        name_elem.text = str(param["Argument.name"])


def _build_property(parent: ET.Element, name: str, value: Any) -> None:
    """根据值的类型构建对应的属性元素"""
    if isinstance(value, bool):
        elem = ET.SubElement(parent, "boolProp")
        elem.set("name", name)
        elem.text = "true" if value else "false"
    elif isinstance(value, int):
        elem = ET.SubElement(parent, "stringProp")
        elem.set("name", name)
        elem.text = str(value)
    elif isinstance(value, float):
        elem = ET.SubElement(parent, "stringProp")
        elem.set("name", name)
        elem.text = str(value)
    elif isinstance(value, list):
        elem = ET.SubElement(parent, "collectionProp")
        elem.set("name", name)
        for item in value:
            if isinstance(item, dict) and "_elementType" in item:
                child = ET.SubElement(elem, "elementProp")
                child.set("name", item.get("_name", item.get("Argument.name", "")))
                child.set("elementType", item.get("_elementType", ""))
                if item.get("_testclass"):
                    child.set("testclass", item["_testclass"])
                if item.get("_testname"):
                    child.set("testname", item["_testname"])
                if "_enabled" in item:
                    child.set("enabled", "true" if item["_enabled"] else "false")
                for k, v in item.items():
                    if not k.startswith("_"):
                        _build_property(child, k, v)
            elif isinstance(item, dict) and "Header.name" in item:
                child = ET.SubElement(elem, "elementProp")
                child.set("name", item.get("_name", item.get("Header.name", "")))
                child.set("elementType", item.get("_elementType", "Header"))
                for k, v in item.items():
                    if not k.startswith("_"):
                        _build_property(child, k, v)
            elif isinstance(item, list):
                nested = ET.SubElement(elem, "collectionProp")
                nested.set("name", "")
                for sub in item:
                    if isinstance(sub, dict):
                        sub_elem = ET.SubElement(nested, "elementProp")
                        sub_elem.set("name", sub.get("_name", ""))
                        sub_elem.set("elementType", sub.get("_elementType", ""))
                        for k, v in sub.items():
                            if not k.startswith("_"):
                                _build_property(sub_elem, k, v)
                    else:
                        sp = ET.SubElement(nested, "stringProp")
                        sp.set("name", "")
                        sp.text = str(sub) if sub is not None else ""
            else:
                child = ET.SubElement(elem, "stringProp")
                child.set("name", "")
                child.text = str(item) if item is not None else ""
    elif isinstance(value, dict):
        elem = ET.SubElement(parent, "elementProp")
        elem.set("name", name)
        elem.set("elementType", value.get("_elementType", ""))
        if value.get("_testclass"):
            elem.set("testclass", value["_testclass"])
        if value.get("_testname"):
            elem.set("testname", value["_testname"])
        if "_enabled" in value:
            elem.set("enabled", "true" if value["_enabled"] else "false")
        for k, v in value.items():
            if not k.startswith("_"):
                _build_property(elem, k, v)
    else:
        elem = ET.SubElement(parent, "stringProp")
        elem.set("name", name)
        elem.text = str(value) if value is not None else ""


def _get_guiclass(testclass: str, node: Dict[str, Any]) -> str:
    """根据 testclass 返回对应的 guiclass"""
    mapping = {
        "TestPlan": "TestPlanGui",
        "ThreadGroup": "ThreadGroupGui",
        "SetupThreadGroup": "SetupThreadGroupGui",
        "PostThreadGroup": "PostThreadGroupGui",
        "HTTPSamplerProxy": "HttpTestSampleGui",
        "JSR223Sampler": "TestBeanGUI",
        "DebugSampler": "TestPanel",
        "ConfigTestElement": "HttpDefaultsGui",
        "HeaderManager": "HeaderPanel",
        "CookieManager": "CookiePanel",
        "CacheManager": "CacheManagerGui",
        "CSVDataSet": "TestBeanGUI",
        "Arguments": "ArgumentsPanel",
        "ResponseAssertion": "AssertionGui",
        "JSONAssertion": "JSONAssertionGui",
        "JSR223Assertion": "TestBeanGUI",
        "DurationAssertion": "DurationAssertionGui",
        "SizeAssertion": "SizeAssertionGui",
        "JSONPostProcessor": "JSONPostProcessorGui",
        "RegexExtractor": "RegexExtractorGui",
        "BoundaryExtractor": "BoundaryExtractorGui",
        "XPath2Extractor": "XPath2ExtractorGui",
        "JSR223PostProcessor": "TestBeanGUI",
        "JSR223PreProcessor": "TestBeanGUI",
        "UserParameters": "UserParametersGui",
        "ConstantTimer": "ConstantTimerGui",
        "UniformRandomTimer": "UniformRandomTimerGui",
        "GaussianRandomTimer": "GaussianRandomTimerGui",
        "PoissonRandomTimer": "PoissonRandomTimerGui",
        "SyncTimer": "SyncTimerGui",
        "ConstantThroughputTimer": "TestBeanGUI",
        "IfController": "IfControllerPanel",
        "LoopController": "LoopControlPanel",
        "WhileController": "WhileControllerGui",
        "TransactionController": "TransactionControllerGui",
        "SimpleController": "SimpleControllerGui",
        "RandomController": "RandomControllerGui",
        "ThroughputController": "ThroughputControllerGui",
        "ForEachController": "ForEachControllerGui",
        "JDBCConnectionConfiguration": "TestBeanGUI",
        "DNSCacheManager": "DNSCachePanel",
        "AuthManager": "AuthPanel",
        "RandomVariableConfig": "TestBeanGUI",
        "ResultCollector": "ViewResultsFullVisualizer",
    }

    if testclass == "ResultCollector":
        props = node.get("properties", {})
        listener_type = props.get("_listener_type")
        if listener_type:
            return listener_type
        testname = node.get("testname", "")
        if "Summary" in testname:
            return "SummaryReport"
        if "Aggregate" in testname:
            return "StatVisualizer"
        return "ViewResultsFullVisualizer"

    return mapping.get(testclass, testclass + "Gui")
