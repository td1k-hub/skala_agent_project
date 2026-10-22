"""Shared sanitized error classification for HTTP and persisted telemetry."""
import asyncio
import httpx
from .gateway import UpstreamError
from .llm import ChatReviewRejected, InvalidModelOutput, ModelUnavailable, ProviderError, ProviderConfigurationError, ProviderAuthenticationError, ProviderModelUnavailable
from .chat_store import ChatProblem


def classify_error(exc):
    # Walk wrapped MCP/AnyIO errors without disclosing provider payloads.
    nested = getattr(exc, "exceptions", None)
    if nested:
        return classify_error(nested[0])
    if isinstance(exc, ChatProblem):
        return exc.status_code, exc.code, exc.message, exc.code == "CHAT_BUSY"
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return 504, "UPSTREAM_TIMEOUT", "분석 응답 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.", True
    if isinstance(exc, ChatReviewRejected):
        descriptions = {"unsupported_claim": "설명에 출처로 확인되지 않은 내용이 있습니다.",
                        "conflicting_status": "설명과 저장된 판정이 일치하지 않습니다.",
                        "insufficient_sources": "설명을 뒷받침할 자료가 부족합니다."}
        return 502, "INVALID_MODEL_OUTPUT", "AI 답변을 수정한 뒤에도 출처 검수를 통과하지 못했습니다. " + descriptions.get(exc.reason, "답변 근거를 확인해 주세요."), False
    if isinstance(exc, InvalidModelOutput):
        return 502, "INVALID_MODEL_OUTPUT", "AI 응답의 형식 또는 근거를 검증하지 못했습니다. 담당자가 입력과 근거를 확인해 주세요.", False
    if isinstance(exc, ModelUnavailable):
        return 503, "OLLAMA_MODEL_UNAVAILABLE", "Ollama 서버 주소와 설치된 모델 이름을 확인해 주세요.", False
    if isinstance(exc, ProviderConfigurationError):
        return 503, "OPENAI_KEY_MISSING", "백엔드 .env에 OPENAI_API_KEY를 설정하고 서버를 다시 시작해 주세요.", False
    if isinstance(exc, ProviderAuthenticationError):
        return 503, "OPENAI_AUTH_FAILED", "OpenAI API 키와 프로젝트 권한을 확인해 주세요.", False
    if isinstance(exc, ProviderModelUnavailable):
        return 503, "OPENAI_MODEL_UNAVAILABLE", "OpenAI 모델 이름과 계정의 모델 사용 권한을 확인해 주세요.", False
    if getattr(exc, "status_code", None) == 429:
        return 503, "PROVIDER_RATE_LIMIT", "AI 서비스의 사용 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.", True
    if isinstance(exc, (UpstreamError, ProviderError, ConnectionError, OSError, httpx.TransportError)):
        return 503, "UPSTREAM_UNAVAILABLE", "데이터 서비스 연결에 실패했습니다. 잠시 후 다시 시도해 주세요.", True
    return 500, "ANALYSIS_FAILED", "요청을 처리하지 못했습니다. 실행 ID로 관리자에게 문의해 주세요.", False


