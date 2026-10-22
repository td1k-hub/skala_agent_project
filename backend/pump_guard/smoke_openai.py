"""Verify OpenAI Responses configuration with one small structured request."""
import asyncio
import json
from uuid import uuid4

from .config import Settings
from .errors import classify_error
from .llm import OpenAIAdapter
from .chat_models import ChatReview


async def check():
    settings = Settings()
    if settings.llm_mode != "openai":
        print(json.dumps({"ok": False, "code": "OPENAI_MODE_REQUIRED", "message": "PUMP_GUARD_LLM_MODE=openai로 설정해 주세요."}, ensure_ascii=False))
        return 1
    try:
        result, metrics = await OpenAIAdapter(settings).generate(
            "connection_check", ChatReview, {"expected": "approve"},
            {"run_id": uuid4()},
            system_prompt="Return JSON with verdict=approve and reason=supported. This is a connection check.",
        )
        if result.verdict != "approve" or result.reason != "supported":
            raise ValueError("Unexpected verification response")
    except Exception as exc:
        _, code, message, _ = classify_error(exc)
        print(json.dumps({"ok": False, "code": code, "message": message}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "model": settings.openai_model,
                      "input_tokens": metrics.input_tokens, "output_tokens": metrics.output_tokens}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check()))
