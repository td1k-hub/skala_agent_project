import asyncio
from contextlib import suppress
from uuid import uuid4
from anyio import CancelScope

from fastapi import Query, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .chat import ChatService
from .chat_models import ChatMessageRequest, ChatSessionRequest, ChatResult
from .chat_store import ChatProblem
from .data import PUMPS
from .research_data import read_dataset


def register_chat_routes(app, settings, encode_event, error_payload):
    service = ChatService(settings)
    app.state.chat = service

    @app.exception_handler(ChatProblem)
    async def chat_problem(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code)

    @app.post("/api/v1/chat/sessions", status_code=201)
    async def create_session(body: ChatSessionRequest):
        if body.equipment_id == "UCI-447":
            if body.analysis_run_id is not None or body.research_cycle_id is None or read_dataset(body.research_cycle_id, settings.research_database_path) is None:
                raise ChatProblem("INVALID_RESEARCH_CONTEXT", "상담할 실험 주기를 선택해 주세요.", 422)
        elif body.research_cycle_id is not None:
            raise ChatProblem("EQUIPMENT_MISMATCH", "공개 실험 데이터는 UCI-447에만 연결할 수 있습니다.", 422)
        elif body.equipment_id not in PUMPS:
            raise ChatProblem("EQUIPMENT_NOT_FOUND", "등록된 설비가 아닙니다.", 404)
        return service.store.create(body.equipment_id, body.analysis_run_id, body.research_cycle_id)

    @app.get("/api/v1/chat/sessions")
    async def list_sessions(equipment_id: str = Query(pattern=r"^(P-[0-9]{3}|UCI-447)$")):
        return {"items": service.store.list(equipment_id)}

    @app.get("/api/v1/chat/sessions/{session_id}")
    async def get_session(session_id: str):
        return service.store.get(session_id)

    @app.delete("/api/v1/chat/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str):
        service.store.delete(session_id)
        return Response(status_code=204)

    @app.post("/api/v1/chat/sessions/{session_id}/messages", response_model=ChatResult)
    async def chat_message(session_id: str, body: ChatMessageRequest):
        service.store.get(session_id)
        run_id = str(uuid4())
        try:
            return await asyncio.wait_for(service.run(session_id, body.message, run_id, app.state.gateway, app.state.pipeline, asyncio.Queue(maxsize=32)), settings.analysis_timeout_seconds)
        except Exception as exc:
            status, error = error_payload(exc, run_id)
            # A busy request never created a telemetry run; UPDATE is then a no-op.
            app.state.pipeline.store.finish(run_id, "failed", error_code=error["code"])
            return JSONResponse({"error": error}, status_code=status)

    @app.post("/api/v1/chat/sessions/{session_id}/messages/stream")
    async def chat_stream(session_id: str, body: ChatMessageRequest):
        service.store.get(session_id)
        run_id = str(uuid4())

        async def events():
            queue = asyncio.Queue(maxsize=32)
            async def worker():
                try:
                    result = await asyncio.wait_for(service.run(session_id, body.message, run_id, app.state.gateway, app.state.pipeline, queue), settings.analysis_timeout_seconds)
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
                yield encode_event("started", {"run_id": run_id, "session_id": session_id, "mode": settings.llm_mode}, run_id, sequence)
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
                task.cancel()
                # StreamingResponse cancels its AnyIO scope on disconnect. Shield
                # the join so repeated cancellation cannot skip lease cleanup.
                with CancelScope(shield=True):
                    with suppress(asyncio.CancelledError):
                        await task
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "X-Run-ID": run_id})
