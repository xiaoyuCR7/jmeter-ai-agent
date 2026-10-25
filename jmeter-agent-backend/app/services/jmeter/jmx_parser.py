"""JMX XML 解析器：将 .jmx 文件解析为 JSON 树结构"""
import copy
import os
import xml.etree.ElementTree as ET
import uuid
from typing import Any, Dict, List


THREAD_GROUP_CLASSES = {"ThreadGroup", "SetupThreadGroup", "PostThreadGroup"}
HTTP_SAMPLER_CLASSES = {"HTTPSamplerProxy", "HTTPSampler"}
RESULT_COLLECTOR_GUICLASS = {
    "ViewResultsFullVisualizer": "ViewResultsFullVisualizer",
    "SummaryReport": "SummaryReport",
    "StatVisualizer": "StatVisualizer",
    "GraphVisualizer": "GraphVisualizer",
    "RespTimeGraphVisualizer": "RespTimeGraphVisualizer",
    "TableVisualizer": "TableVisualizer",
    "SimpleDataWriter": "SimpleDataWriter",
    "AssertionVisualizer": "AssertionVisualizer",
    "MailerVisualizer": "MailerVisualizer",
    "BackendListenerGui": "BackendListenerGui",
}


def parse_jmx(source: str) -> Dict[str, Any]:
    """将 JMX XML 字符串或文件路径解析为 JSON 树"""
    if os.path.isfile(source):
        with open(source, "r", encoding="utf-8") as f:
            xml_content = f.read()
    else:
        xml_content = source

    root = ET.fromstring(xml_content)
    if root.tag != "jmeterTestPlan":
        raise ValueError("Invalid JMX file: root element must be <jmeterTestPlan>")

    hash_tree = root.find("hashTree")
    if hash_tree is None:
        raise ValueError("Invalid JMX file: missing <hashTree>")

    children = _parse_hash_tree(hash_tree)
    if not children:
        raise ValueError("Invalid JMX file: empty test plan")

    plan = children[0]
    plan["properties"][ROOT_ATTRS_KEY] = dict(root.attrib)
    return plan


# Hidden keys (the builder skips "__" properties) that let build_jmx write
# untouched properties back byte-for-byte instead of re-deriving them.
SOURCE_KEY = "__source"
ROOT_ATTRS_KEY = "__root_attrs"


def strip_source(node: Dict[str, Any]) -> Dict[str, Any]:
    """Drop the round-trip bookkeeping, e.g. before showing a tree to a model."""
    props = node.get("properties", {})
    props.pop(SOURCE_KEY, None)
    props.pop(ROOT_ATTRS_KEY, None)
    for child in node.get("children", []):
        strip_source(child)
    return node


def _raw_xml(elem: ET.Element) -> str:
    clone = copy.deepcopy(elem)
    clone.tail = None
    for e in clone.iter():
        # Drop indentation so the builder's pretty-printer can re-indent cleanly.
        if len(e) and e.text is not None and not e.text.strip():
            e.text = None
        if e is not clone and e.tail is not None and not e.tail.strip():
            e.tail = None
    return ET.tostring(clone, encoding="unicode")


def _parse_hash_tree(hash_tree: ET.Element) -> List[Dict[str, Any]]:
    """解析一个 <hashTree>，返回其中的组件节点列表"""
    nodes = []
    i = 0
    children = list(hash_tree)

    while i < len(children):
        elem = children[i]
        if elem.tag == "hashTree":
            i += 1
            continue

        node = _parse_element(elem)

        if i + 1 < len(children) and children[i + 1].tag == "hashTree":
            node["children"] = _parse_hash_tree(children[i + 1])
            i += 2
        else:
            node["children"] = []
            i += 1

        nodes.append(node)

    return nodes


def _parse_element(elem: ET.Element) -> Dict[str, Any]:
    """解析单个 JMeter 组件元素为 JSON 节点"""
    testclass = elem.get("testclass", elem.tag)
    if testclass == "HTTPSampler":
        testclass = "HTTPSamplerProxy"

    node = {
        "id": str(uuid.uuid4()),
        "testclass": testclass,
        "testname": elem.get("testname", ""),
        "enabled": elem.get("enabled", "true").lower() == "true",
        "properties": {},
        "children": [],
    }

    raw = []
    for child in elem:
        prop_name, prop_value = _parse_property(child)
        if prop_name:
            node["properties"][prop_name] = prop_value
        raw.append([prop_name, _raw_xml(child)])

    _post_process_node(node, elem)
    node["properties"][SOURCE_KEY] = {
        "guiclass": elem.get("guiclass", ""),
        "props": raw,
        "snapshot": copy.deepcopy(node["properties"]),
    }
    return node


def _post_process_node(node: Dict[str, Any], elem: ET.Element) -> None:
    """解析后处理：提取简化编辑字段"""
    testclass = node["testclass"]
    props = node["properties"]

    if testclass in THREAD_GROUP_CLASSES:
        main_ctrl = props.pop("ThreadGroup.main_controller", None)
        if isinstance(main_ctrl, dict):
            if main_ctrl.get("LoopController.continue_forever"):
                props["loops"] = -1
            else:
                loops = main_ctrl.get("LoopController.loops", 1)
                try:
                    props["loops"] = int(loops)
                except (TypeError, ValueError):
                    props["loops"] = loops

    if testclass in HTTP_SAMPLER_CLASSES:
        _extract_http_arguments(props)

    if testclass == "ResultCollector":
        guiclass = elem.get("guiclass", "")
        if guiclass in RESULT_COLLECTOR_GUICLASS:
            props["_listener_type"] = guiclass


def _extract_http_arguments(props: Dict[str, Any]) -> None:
    args = props.get("HTTPsampler.Arguments")
    if not isinstance(args, dict) or args.get("_elementType") != "Arguments":
        return

    arguments = args.get("Arguments.arguments", [])
    if not isinstance(arguments, list):
        del props["HTTPsampler.Arguments"]
        return

    if len(arguments) == 1:
        arg = arguments[0]
        if isinstance(arg, dict) and not arg.get("Argument.name"):
            props["__body_data"] = arg.get("Argument.value", "")
            props["HTTPSampler.postBodyRaw"] = props.get("HTTPSampler.postBodyRaw", True)
            del props["HTTPsampler.Arguments"]
            return

    if arguments:
        props["__http_params"] = [
            _normalize_http_arg(a) for a in arguments if isinstance(a, dict)
        ]
    del props["HTTPsampler.Arguments"]


def _normalize_http_arg(arg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "_elementType": "HTTPArgument",
        "_name": arg.get("_name", arg.get("Argument.name", "")),
        "Argument.name": arg.get("Argument.name", ""),
        "Argument.value": arg.get("Argument.value", ""),
        "Argument.metadata": arg.get("Argument.metadata", "="),
        "HTTPArgument.always_encode": arg.get("HTTPArgument.always_encode", False),
        "HTTPArgument.use_equals": arg.get("HTTPArgument.use_equals", True),
        "HTTPArgument.content_type": arg.get("HTTPArgument.content_type", ""),
    }


def _parse_property(elem: ET.Element) -> tuple:
    """解析单个属性元素，返回 (name, value)"""
    tag = elem.tag
    name = elem.get("name", "")

    if tag == "stringProp":
        return name, elem.text or ""

    if tag in ("intProp", "longProp"):
        try:
            return name, int(elem.text or "0")
        except ValueError:
            return name, elem.text or ""

    if tag == "doubleProp":
        name = name or elem.findtext("name", "")
        text = elem.findtext("value")
        if text is None:
            text = elem.text or "0"
        try:
            return name, float(text)
        except ValueError:
            return name, text

    if tag == "boolProp":
        return name, elem.text == "true"

    if tag == "collectionProp":
        items = []
        for child in elem:
            _, value = _parse_property(child)
            items.append(value)
        return name, items

    if tag == "elementProp":
        obj = {
            "_elementType": elem.get("elementType", ""),
            "_testclass": elem.get("testclass", ""),
            "_testname": elem.get("testname", ""),
            "_enabled": elem.get("enabled", "true").lower() == "true",
            "_name": elem.get("name", ""),
        }
        for child in elem:
            prop_name, prop_value = _parse_property(child)
            if prop_name:
                obj[prop_name] = prop_value
        return name, obj

    if tag == "objProp":
        return name, elem.text or ""

    return name, None
