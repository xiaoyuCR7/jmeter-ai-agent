"""This machine: local access, tool confirmations, system info, paths and the JMeter registry."""
import os
import shutil
import asyncio

from fastapi import APIRouter, HTTPException, Response
from app.core import local_access
from app.core.config import settings
from app.core.config import load_paths_config, save_paths_config
from app.core.text import current_lang, tr
from app.services.jmeter.registry_en import localize
from app.services.jmeter.executor import get_executor
from app.services.jmeter.component_registry import get_all_components, get_allowed_children
from app.services.jmeter.function_registry import get_all_functions

router = APIRouter()


# ===== Local access =====

@router.post("/session")
async def create_session(request: dict, response: Response):
    """Trade the access token from the start link for a cookie that scripts on the page cannot read."""
    token = request.get("token")
    if not isinstance(token, str) or not local_access.token_matches(token):
        raise HTTPException(401, "Invalid access token")
    response.set_cookie(local_access.COOKIE_NAME, token, max_age=local_access.COOKIE_MAX_AGE,
                        httponly=True, samesite="strict", path="/")
    return {"accepted": True}


@router.get("/session/token")
async def session_token():
    """For the MCP setup page: already signed-in callers only, as this path is not exempt."""
    return {"token": local_access.get_token()}


# ===== Agent tool confirmations =====

@router.get("/confirmations")
async def pending_confirmations():
    from app.services.confirmations import confirmations
    return confirmations.list_pending()


@router.post("/confirmations/{confirmation_id}")
async def decide_confirmation(confirmation_id: str, decision: dict):
    from app.services.confirmations import confirmations
    approved = decision.get("approved")
    if not isinstance(approved, bool):
        raise HTTPException(422, "approved must be a JSON boolean")
    try:
        confirmations.decide(confirmation_id, approved)
    except KeyError:
        raise HTTPException(409, "Confirmation expired or already consumed") from None
    return {"accepted": True}


# ===== Report storage =====

@router.get("/system/storage")
async def report_storage():
    """How much disk the reports hold, and how much of it pruning old runs could free."""
    from app.services import report_retention
    return await asyncio.to_thread(report_retention.usage)


@router.post("/system/storage/prune")
async def prune_reports(request: dict):
    """Remove the HTML dashboard (and optionally the raw results) of runs older than ``days``."""
    from app.services import report_retention
    days, include = request.get("days"), request.get("include_results", False)
    if type(days) is not int or days < 1 or not isinstance(include, bool):
        raise HTTPException(422, "days must be a whole number of at least 1; include_results a JSON boolean")
    return await report_retention.prune(days, include)


def _local_jmeter():
    """Folder and path of the JMeter this app will really run, which may differ from JMETER_HOME."""
    try:
        path = get_executor().jmeter_path
    except FileNotFoundError:
        return None, None
    path = shutil.which(path) or path  # found on PATH: only the bare name is known
    return os.path.dirname(path), path


@router.get("/system/info")
async def system_info():
    """获取系统信息"""
    import platform
    in_container = settings.JMETER_EXECUTION_MODE == 'container'
    jmeter_home, jmeter_path = (None, None) if in_container else _local_jmeter()

    return {
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "execution_mode": settings.JMETER_EXECUTION_MODE,
        "jmeter_image": settings.JMETER_CONTAINER_IMAGE if in_container else None,
        "jmeter_home": jmeter_home,
        "jmeter_executable": jmeter_path,
        "test_plans_dir": settings.TEST_PLANS_DIR,
        "reports_dir": settings.REPORTS_DIR,
        "test_plans_dir_default": settings.TEST_PLANS_DIR_DEFAULT,
        "reports_dir_default": settings.REPORTS_DIR_DEFAULT,
    }


@router.get("/system/resources")
async def get_system_resources():
    """获取当前系统资源使用情况(CPU/内存)"""
    try:
        from app.services.jmeter.resource_monitor import get_resource_monitor
        monitor = get_resource_monitor()
        snap = await asyncio.to_thread(monitor.snapshot)
        return snap.to_dict()
    except Exception:
        import psutil
        cpu = await asyncio.to_thread(psutil.cpu_percent, interval=0.3)
        mem = psutil.virtual_memory()
        return {
            "cpu_percent": round(cpu, 1),
            "memory_percent": round(mem.percent, 1),
            "memory_used_gb": round(mem.used / (1024**3), 2),
            "memory_total_gb": round(mem.total / (1024**3), 2),
            "is_healthy": (cpu <= settings.RESOURCE_CPU_THRESHOLD
                           and mem.percent <= settings.RESOURCE_MEMORY_THRESHOLD),
        }


@router.get("/system/paths")
async def get_paths_config():
    """获取路径配置"""
    config = load_paths_config()
    return {
        "test_plans_dir": config.get("test_plans_dir", settings.TEST_PLANS_DIR_DEFAULT),
        "reports_dir": config.get("reports_dir", settings.REPORTS_DIR_DEFAULT),
        "test_plans_dir_default": settings.TEST_PLANS_DIR_DEFAULT,
        "reports_dir_default": settings.REPORTS_DIR_DEFAULT,
    }


@router.put("/system/paths")
async def update_paths_config(request: dict):
    """更新路径配置"""
    test_plans_dir = request.get("test_plans_dir")
    reports_dir = request.get("reports_dir")
    
    if not test_plans_dir or not reports_dir:
        raise HTTPException(status_code=400, detail=tr("test_plans_dir 和 reports_dir 都是必填项", "Both test_plans_dir and reports_dir are required"))
    
    if not os.path.isabs(test_plans_dir):
        raise HTTPException(status_code=400, detail=tr("test_plans_dir 必须是绝对路径", "test_plans_dir must be an absolute path"))
    
    if not os.path.isabs(reports_dir):
        raise HTTPException(status_code=400, detail=tr("reports_dir 必须是绝对路径", "reports_dir must be an absolute path"))
    
    try:
        os.makedirs(test_plans_dir, exist_ok=True)
        os.makedirs(reports_dir, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=tr(f"无法创建目录: {str(e)}", f"Could not create the folder: {str(e)}")) from e
    
    save_paths_config({
        "test_plans_dir": test_plans_dir,
        "reports_dir": reports_dir
    })
    
    return {"success": True, "message": tr("路径配置已更新", "Folders updated")}


# ===== JMeter Component & Function Registry =====
@router.get("/jmeter/components")
async def get_components():
    """获取支持的 JMeter 组件列表"""
    return localize(get_all_components(), current_lang())


@router.get("/jmeter/functions")
async def get_functions():
    """获取支持的 JMeter 函数列表"""
    return localize(get_all_functions(), current_lang())


@router.get("/jmeter/components/{parent_testclass}/children")
async def get_component_children(parent_testclass: str):
    """获取某组件下允许添加的子组件"""
    return localize(get_allowed_children(parent_testclass), current_lang())
