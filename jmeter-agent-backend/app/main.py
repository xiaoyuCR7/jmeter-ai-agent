import warnings

warnings.filterwarnings(
    "ignore",
    message=r"Field \"model_name\" in .+ has conflict with protected namespace \"model_\".",
    category=UserWarning,
)

import asyncio
import logging
import os
from contextlib import asynccontextmanager, AsyncExitStack

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import routes, shared
from app.api.mcp_routes import init_mcp_server
from app.core import local_access
from app.core.config import settings, validate_deployment_security
from app.core.errors import ServiceError
from app.core.logging_setup import configure_logging
from app.core.text import RequestLanguageMiddleware
from app.middleware.local_guard import (
    LocalAccessMiddleware, OriginGuardMiddleware, ReportSandboxMiddleware, TrustedHostMiddleware,
)
from app.models.db import init_db
from app.services import experiments, notifications, post_run, report_retention
from app.services.ai.adapters.factory import AIAdapterFactory
from app.services.ai.memory import fact_ledger, hypothesis_ledger
from app.services.execution_lifecycle import execution_lifecycle
from app.services.jmeter import environment as run_environment, resource_monitor
from app.services.storage import store

logger = logging.getLogger(__name__)

frontend_dist = os.path.join(os.path.dirname(settings.BASE_DIR), "jmeter-agent-frontend", "dist")


async def _stop_monitoring():
    if resource_monitor._monitor is not None:
        await resource_monitor._monitor.stop_monitoring()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_deployment_security()
    configure_logging()
    # On the console only: this link is a credential and must stay out of log files.
    print(f'Open JMeter Agent: {local_access.access_url()}', flush=True)
    if os.path.exists(frontend_dist):
        print(f'Built UI: {local_access.access_url(f"http://localhost:{settings.PORT}")}', flush=True)
    await init_db()
    await store.sync_from_dbstore()
    await shared.ensure_default_project()

    async with AsyncExitStack() as cleanup:
        # LIFO cleanup remains guaranteed if startup, serving, or one cleanup fails.
        cleanup.push_async_callback(AIAdapterFactory.close_all)
        # Pushed early so they close late. An open connection's thread would keep the process from exiting.
        cleanup.push_async_callback(fact_ledger.close)
        cleanup.push_async_callback(hypothesis_ledger.close)
        cleanup.push_async_callback(_stop_monitoring)
        execution_lifecycle.start()
        notifications.register(execution_lifecycle)
        execution_lifecycle.set_post_run(post_run.finish_run)
        cleanup.callback(execution_lifecycle.set_post_run, None)
        # Runs after the lifecycle shutdown below, so the cancellations it causes are still sent.
        cleanup.push_async_callback(notifications.drain, 10.0)
        cleanup.callback(notifications.unregister, execution_lifecycle)
        cleanup.push_async_callback(execution_lifecycle.shutdown)
        cleanup.push_async_callback(post_run.stop_reanalyses)
        cleanup.push_async_callback(experiments.stop_searches)
        await post_run.recover_interrupted()
        await asyncio.to_thread(experiments.recover_searches)
        # A run cut off by a restart never masked the secrets its copy of the plan carried.
        for execution in list(store.executions.values()):
            await asyncio.to_thread(run_environment.scrub, execution.jmx_path)
        # Older runs join project history in the background; reading their plans can take a while.
        backfill = asyncio.create_task(post_run.backfill_memory())

        async def stop_backfill():
            backfill.cancel()
            await asyncio.gather(backfill, return_exceptions=True)
        cleanup.push_async_callback(stop_backfill)

        pruning = asyncio.create_task(report_retention.keep_pruned())

        async def stop_pruning():
            pruning.cancel()
            await asyncio.gather(pruning, return_exceptions=True)
        cleanup.push_async_callback(stop_pruning)

        await init_mcp_server()

        try:
            from app.services.jmeter.executor import get_executor, remove_orphan_containers
            await asyncio.to_thread(remove_orphan_containers)
            executor = get_executor()
            if executor.jmeter_path:
                logger.info("JMeter executable found at: %s", executor.jmeter_path)
        except Exception as e:
            logger.warning("JMeter health check failed on startup: %s", e)

        yield


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI驱动的JMeter性能测试平台 - 支持多AI模型、自动生成测试计划、智能分析报告",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


@app.exception_handler(ServiceError)
async def service_error(_request: Request, error: ServiceError):
    return JSONResponse(status_code=error.status_code, content={"detail": error.detail})


# Local single-user tool: no accounts. The access token proves a caller is the user; Origin
# checks still stop other websites in the same browser from driving the local API.
app.add_middleware(RequestLanguageMiddleware)  # Innermost: refusals by the guards below stay in English.
app.add_middleware(LocalAccessMiddleware)
app.add_middleware(ReportSandboxMiddleware)
app.add_middleware(OriginGuardMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Validate Host before deriving same-origin from it (DNS rebinding).
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.ALLOWED_HOSTS)


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


app.include_router(routes.router, prefix="/api/v1")


class ReportFiles(StaticFiles):
    """Follow the reports folder, which can be changed in settings while the app runs."""
    def lookup_path(self, path):
        self.all_directories = [settings.REPORTS_DIR]
        return super().lookup_path(path)


os.makedirs(settings.REPORTS_DIR, exist_ok=True)
app.mount("/reports", ReportFiles(directory=settings.REPORTS_DIR), name="reports")

# A page the browser cached before an upgrade asks for a script the new build no longer has. Instead of
# a blank page, that script reloads the page once; a reload always checks the page with the server.
# A running app that asks for a replaced page chunk gets an import error instead, which its router turns
# into opening that page in the new build (main.ts).
_RELOAD_ONCE = (
    "if(window.__jmeterAgentBooted)throw new Error('Failed to fetch dynamically imported module: replaced by an upgrade');"
    "try{if(sessionStorage.getItem('jmeter-agent-stale')!==location.pathname){"
    "sessionStorage.setItem('jmeter-agent-stale',location.pathname);location.reload()}}"
    "catch(e){}"
)


class UIAssets(StaticFiles):
    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as error:
            if error.status_code != 404 or not path.endswith('.js'):
                raise
            return Response(_RELOAD_ONCE, media_type='text/javascript', headers={'Cache-Control': 'no-store'})


if os.path.exists(frontend_dist):
    app.mount("/assets", UIAssets(directory=os.path.join(frontend_dist, "assets")), name="assets")


def _app_page(index_path: str) -> FileResponse:
    # Checked with the server every time: an index.html the browser kept from before an upgrade asked
    # for chunks the new build no longer has, and the app opened blank.
    return FileResponse(index_path, headers={"Cache-Control": "no-cache"})


@app.get("/")
async def root():
    index_path = os.path.join(frontend_dist, "index.html")
    if os.path.exists(index_path):
        return _app_page(index_path)
    return {"name": settings.APP_NAME, "version": settings.APP_VERSION, "docs": "/api/docs"}


@app.get("/{path:path}")
async def catch_all(path: str):
    """A file at the top of the built UI (logo.svg) as itself; any other page route gets the app."""
    if path.startswith(("api/", "reports/")) or path == "health":
        raise HTTPException(status_code=404, detail="Not found")
    if path and "/" not in path and "\\" not in path:
        candidate = os.path.join(frontend_dist, path)
        if os.path.isfile(candidate):
            return FileResponse(candidate)
    index_path = os.path.join(frontend_dist, "index.html")
    if os.path.exists(index_path):
        return _app_page(index_path)
    raise HTTPException(status_code=404, detail="Not found")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=settings.DEBUG)
