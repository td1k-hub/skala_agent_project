import asyncio
import json
import logging
from contextlib import suppress, asynccontextmanager
from uuid import uuid4
from pathlib import Path
from anyio import CancelScope

from fastapi import FastAPI, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .data import PUMPS
from .gateway import Gateway
from .models import AnalysisRequest, AnalysisResult, InspectionRequest
from .pipeline import Pipeline, run_pipeline
from .errors import classify_error
from .agents import ROLES
from .chat_api import register_chat_routes
from .chat_store import ConversationStore
from .research_data import read_dataset
from .lifecycle import register_lifecycle_routes

logger = logging.getLogger("pump_guard")


class DashboardFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path in {".", "", "index.html"}:
            response.headers["Cache-Control"] = "no-store"
        return response


class BodyLimit:
    """Enforce actual bytes too, including bodies without Content-Length."""
    def __init__(self, app, max_bytes):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        messages, total = [], 0
        limit = 262144 if scope.get("path") == "/api/v1/lifecycle/readings" else self.max_bytes
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            total += len(message.get("body", b""))
            if total > limit:
                response = JSONResponse({"error": {"code": "BODY_TOO_LARGE", "message": "입력 데이터가 허용 크기를 초과했습니다."}}, status_code=413)
                return await response(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay():
            return messages.pop(0) if messages else await receive()
        await self.app(scope, replay, send)


def error_payload(exc, run_id):
    status, code, message, retryable = classify_error(exc)
    # No raw exceptions, sensor payloads, prompts, secrets or MCP output in logs.
    logger.warning("request_failed run_id=%s code=%s", run_id, code)
    return status, {"run_id": run_id, "code": code, "message": message, "retryable": retryable}


def encode_event(event, data, run_id, sequence):
    return f"id: {run_id}:{sequence}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=False, allow_nan=False)}\n\n"


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    @asynccontextmanager
    async def lifespan(app):
        yield
        client = app.state.pipeline.trace_client
        if client is not None:
            await asyncio.to_thread(client.close, timeout=3)

    app = FastAPI(title="PUMP GUARD", version="0.5.0", lifespan=lifespan,
                  description="Role agents + MCP + OpenAI + streaming + local/LangSmith monitoring")
    app.state.gateway = Gateway(settings)
    app.state.pipeline = Pipeline(settings)
    app.add_middleware(BodyLimit, max_bytes=settings.max_body_bytes)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type"], allow_credentials=False)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Do not echo rejected input (which can include PII or injected prompts).
        return JSONResponse({"error": {"code": "INVALID_INPUT", "message": "설비 ID, 수치 범위 및 허용 필드를 확인해 주세요.", "fields": [{"loc": list(err["loc"]), "type": err["type"]} for err in exc.errors()]}}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        status, error = error_payload(exc, str(uuid4()))
        return JSONResponse({"error": error}, status_code=status)

    def unknown_equipment(equipment_id):
        if equipment_id not in PUMPS:
            return JSONResponse({"error": {"code": "EQUIPMENT_NOT_FOUND", "message": "등록된 모의 설비가 아닙니다."}}, status_code=404)

    async def call(server, tool, arguments=None):
        try:
            return await asyncio.wait_for(app.state.gateway.call(server, tool, arguments), settings.analysis_timeout_seconds)
        except Exception as exc:
            status, error = error_payload(exc, str(uuid4()))
            return JSONResponse({"error": error}, status_code=status)

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": settings.llm_mode, "model": settings.active_model,
                "provider_configured": bool(settings.openai_api_key and settings.openai_api_key.get_secret_value().strip()) if settings.llm_mode == "openai" else True,
                "version": "0.5.0", "architecture": "multi_agent" if settings.llm_mode != "mock" else "workflow"}

    @app.get("/api/v1/equipment")
    async def equipment():
        return await call("equipment", "list_equipment")

    @app.get("/api/v1/equipment/{equipment_id}")
    async def detail(equipment_id: str):
        return unknown_equipment(equipment_id) or await call("equipment", "get_equipment", {"equipment_id": equipment_id})

    @app.get("/api/v1/equipment/{equipment_id}/operating-history")
    async def operating_history(equipment_id: str):
        return unknown_equipment(equipment_id) or await call("equipment", "get_operating_history", {"equipment_id": equipment_id})

    @app.get("/api/v1/equipment/{equipment_id}/maintenance-history")
    async def maintenance_history(equipment_id: str):
        return unknown_equipment(equipment_id) or await call("equipment", "get_maintenance_history", {"equipment_id": equipment_id})

    @app.get("/api/v1/equipment/{equipment_id}/analysis-history")
    async def analysis_history(equipment_id: str, limit: int = Query(default=30, ge=1, le=100)):
        if response := unknown_equipment(equipment_id):
            return response
        return {"equipment_id": equipment_id, "items": ConversationStore(settings.database_path).list_analyses(equipment_id, limit)}

    @app.post("/api/v1/inspections", status_code=201)
    async def inspection(body: InspectionRequest):
        if body.equipment_id == "UCI-447":
            if body.research_cycle_id is None or read_dataset(body.research_cycle_id, database=settings.research_database_path) is None:
                return JSONResponse({"error": {"code": "INVALID_CYCLE", "message": "유효한 실험 주기를 연결해 주세요."}}, status_code=422)
            return await call("equipment", "save_inspection", {"record": body.model_dump()})
        if body.research_cycle_id is not None:
            return JSONResponse({"error": {"code": "EQUIPMENT_MISMATCH", "message": "공개 실험 주기는 UCI-447에만 연결할 수 있습니다."}}, status_code=422)
        return unknown_equipment(body.equipment_id) or await call("equipment", "save_inspection", {"record": body.model_dump()})

    @app.post("/api/v1/analyses", response_model=AnalysisResult)
    async def analysis(body: AnalysisRequest):
        if response := unknown_equipment(body.equipment_id):
            return response
        run_id = str(uuid4())
        try:
            return await asyncio.wait_for(run_pipeline(body, run_id, app.state.gateway, asyncio.Queue(maxsize=32), app.state.pipeline), settings.analysis_timeout_seconds)
        except Exception as exc:
            status, error = error_payload(exc, run_id)
            app.state.pipeline.store.finish(run_id, "failed", error_code=error["code"])
            return JSONResponse({"error": error}, status_code=status)

    @app.post("/api/v1/analyses/stream", response_class=StreamingResponse, responses={200: {"content": {"text/event-stream": {}}}})
    async def stream(body: AnalysisRequest):
        if response := unknown_equipment(body.equipment_id):
            return response
        run_id = str(uuid4())

        async def events():
            queue = asyncio.Queue(maxsize=32)

            async def worker():
                try:
                    result = await asyncio.wait_for(run_pipeline(body, run_id, app.state.gateway, queue, app.state.pipeline), settings.analysis_timeout_seconds)
                    await queue.put(("result", result.model_dump(mode="json")))
                except Exception as exc:
                    _, error = error_payload(exc, run_id)
                    app.state.pipeline.store.finish(run_id, "failed", error_code=error["code"])
                    await queue.put(("error", error))
                finally:
                    await queue.put(("done", None))

            task = asyncio.create_task(worker())
            sequence = 0
            try:
                yield encode_event("started", {"run_id": run_id, "mode": settings.llm_mode}, run_id, sequence)
                while True:
                    try:
                        event, data = await asyncio.wait_for(queue.get(), settings.heartbeat_seconds)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if event == "done":
                        break
                    sequence += 1
                    yield encode_event(event, data, run_id, sequence)
            finally:
                # ASGI disconnect cancellation closes the task, SDK sessions and children.
                task.cancel()
                with CancelScope(shield=True):
                    with suppress(asyncio.CancelledError):
                        await task

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "X-Run-ID": run_id})

    @app.get("/api/v1/admin/monitoring")
    async def monitoring():
        return {"summary": app.state.pipeline.store.summary(),
                "langsmith": {"enabled": settings.langsmith_tracing, "project": settings.langsmith_project,
                              "endpoint": settings.langsmith_endpoint,
                              "state": "configured_unverified" if settings.langsmith_tracing else "disabled"},
                "agents": [{"id": role, "name": data[0], "tools": sorted(data[3])} for role, data in ROLES.items()]}

    @app.get("/api/v1/admin/runs")
    async def runs(limit: int = Query(default=50, ge=1, le=100)):
        return {"items": app.state.pipeline.store.list(limit)}

    @app.get("/api/v1/admin/langsmith/connection")
    async def langsmith_connection():
        """Verify remote credentials without exposing the key or trace contents."""
        client = app.state.pipeline.trace_client
        if not settings.langsmith_tracing or client is None:
            return {"state": "disabled", "message": "LangSmith 추적이 꺼져 있습니다. 키와 추적 설정을 확인해 주세요."}
        try:
            await asyncio.wait_for(asyncio.to_thread(lambda: next(client.list_projects(limit=1), None)), timeout=8)
            return {"state": "connected", "message": "LangSmith 연결을 확인했습니다. 새 분석이나 상담부터 추적됩니다."}
        except Exception:
            return {"state": "unavailable", "message": "LangSmith에 연결하지 못했습니다. 키·워크스페이스 ID·리전 endpoint·네트워크를 확인해 주세요."}

    @app.get("/api/v1/admin/runs/{run_id}")
    async def run_detail(run_id: str):
        item = app.state.pipeline.store.detail(run_id)
        return item or JSONResponse({"error": {"code": "RUN_NOT_FOUND", "message": "실행 기록을 찾을 수 없습니다."}}, status_code=404)

    @app.get("/api/v1/admin/runs/{run_id}/trace")
    async def run_trace(run_id: str):
        item = app.state.pipeline.store.detail(run_id)
        if item is None:
            return JSONResponse({"error": {"code": "RUN_NOT_FOUND", "message": "실행 기록을 찾을 수 없습니다."}}, status_code=404)
        client = app.state.pipeline.trace_client
        if client is None or not item["tracing_enabled"]:
            return {"state": "disabled", "url": None, "message": "이 실행의 LangSmith 추적이 비활성화되어 있습니다."}
        try:
            def lookup():
                run = client.read_run(item["trace_id"])
                start, end = getattr(run, "start_time", None), getattr(run, "end_time", None)
                remote = {"state": "failed" if getattr(run, "error", None) else "completed" if end else "running",
                          "latency_ms": max(0, (end - start).total_seconds() * 1000) if start and end else None,
                          "input_tokens": getattr(run, "prompt_tokens", None),
                          "output_tokens": getattr(run, "completion_tokens", None)}
                return client.get_run_url(run=run), remote
            url, remote = await asyncio.wait_for(asyncio.to_thread(lookup), timeout=8)
            return {"state": "available", "url": url, "remote": remote, "message": "LangSmith에서 실행 기록을 확인했습니다."}
        except Exception:
            return {"state": "unavailable", "url": None, "message": "LangSmith 기록을 아직 확인하지 못했습니다. 전송 지연·키·프로젝트·네트워크를 확인해 주세요."}

    @app.get("/api/v1/research/uci447")
    def research_metadata():
        return read_dataset(database=settings.research_database_path) or JSONResponse({"error": {"code": "DATASET_NOT_IMPORTED", "message": "공개 데이터를 먼저 가져와 주세요."}}, status_code=404)

    @app.get("/api/v1/research/uci447/cycles/{cycle_id}")
    def research_cycle(cycle_id: int):
        if cycle_id < 1 or cycle_id > 2205:
            return JSONResponse({"error": {"code": "CYCLE_NOT_FOUND", "message": "1~2205 범위의 실험 주기를 선택해 주세요."}}, status_code=404)
        return read_dataset(cycle_id, database=settings.research_database_path) or JSONResponse({"error": {"code": "CYCLE_NOT_FOUND", "message": "실험 데이터를 찾을 수 없습니다."}}, status_code=404)

    register_chat_routes(app, settings, encode_event, error_payload)
    register_lifecycle_routes(app, settings)

    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", DashboardFiles(directory=frontend_dist, html=True), name="dashboard")

    return app


app = create_app()
