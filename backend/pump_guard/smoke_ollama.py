"""Run a real local Ollama + MCP analysis without starting the HTTP API."""
import asyncio
import json
from pathlib import Path

import httpx

from .config import Settings
from .gateway import Gateway
from .llm import ModelUnavailable
from .main import error_payload
from .models import AnalysisRequest
from .pipeline import Pipeline
from uuid import uuid4


async def check():
    settings = Settings().model_copy(update={"llm_mode": "ollama"})
    run_id = str(uuid4())
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(settings.ollama_base_url.rstrip("/") + "/api/tags")
            response.raise_for_status()
            names = {m["name"] for m in response.json()["models"]}
        if settings.ollama_model not in names:
            raise ModelUnavailable("Configured model is not installed")
        request = AnalysisRequest.model_validate_json((Path(__file__).resolve().parents[1] / "example-request.json").read_text())
        result = await asyncio.wait_for(
            Pipeline(settings).run(request, run_id, Gateway(settings), asyncio.Queue()),
            settings.analysis_timeout_seconds,
        )
    except Exception as exc:
        _, error = error_payload(exc, run_id)
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "result": result.model_dump(mode="json")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check()))
