# PUMP GUARD · 설비 분석·AI 상담·관리자 모니터링

FastAPI + LangGraph 5단계 파이프라인 + OpenAI Responses API + MCP 서버 2개 + REST/SSE + Vue 3 대시보드를 연결한 로컬 개발용 앱입니다. Python **3.11 이상**이 필요합니다. 프론트엔드 빌드에는 Node **20.19 이상(20.x) 또는 22.12 이상**이 필요합니다. 역할별 에이전트와 관리자 모니터링 화면을 제공합니다. 설비와 분석 결과를 연결하는 AI 상담 화면을 제공합니다.

설비·운전 이력·정비지식은 합성 예제이며 위험 판정은 미승인 데모 임계값입니다. OpenAI 모델을 사용해도 `decision_supported: false`입니다.

별도 **공개 실측 데이터** 메뉴에는 UCI 447 유압 시험장치 측정 자료를 가져왔습니다. 2,205개 실험 주기와 17개 센서의 조회·그래프·60초 기록 재생을 제공합니다. 이 자료는 기존 합성 설비 분석과 별도이며, 이상 탐지 모델을 학습하거나 성능 평가에 사용한 상태는 아닙니다.

## 공개 실측 자료

- 원본: [UCI Condition monitoring of hydraulic systems](https://archive.ics.uci.edu/dataset/447/condition+monitoring+of+hydraulic+systems), Helwig·Pignanelli·Schütze (2015), ZeMA, DOI `10.24432/C5CW21`, CC BY 4.0.
- 원본 파일: `research/downloads/uci447/` (약 531 MiB), 가공 DB: `research/processed/uci447.sqlite3`. 모두 버전 관리에서 제외합니다.
- 가져오기: 센서별 2,205행, 주기당 60초 × 수집 주파수, 유한 수치, 5개 상태 라벨의 허용값을 검사합니다. SHA-256을 DB 메타데이터에 보관합니다. 가공 DB는 검증 완료 후 교체합니다.
- 조회 API: `GET /api/v1/research/uci447`, `GET /api/v1/research/uci447/cycles/1` (주기 1~2205).
- 각 센서의 원본 전체 샘플로 주기 평균·최솟값·최댓값을 계산합니다. 그래프와 재생값은 1초 구간 평균입니다. 실험 주기를 연속 현장 운전 이력으로 연결하거나 관측 시각을 만들지 않습니다.
- 상태 표시는 `profile.txt`의 실험 라벨이며 모델 예측이 아닙니다. TS 온도는 베어링 온도가 아니며, EPS1 전력(W)은 전류(A)가 아닙니다. PS1~6의 흡입·토출 위치도 임의로 지정하지 않습니다.
- 유압 시험장치 자료이므로 화학공단 원심펌프의 현장 성능·안전성을 검증한 근거가 아닙니다. AI 상담은 실측 자료를 읽기 전용 MCP로 조회합니다. 기존 수치 분석 에이전트의 임계값·입력과는 별도입니다.

다시 가져올 때는 압축을 푼 원본 폴더를 지정합니다(추가 패키지 불필요).

```bash
cd backend
.venv/bin/python -m pump_guard.research_data '/path/to/condition+monitoring+of+hydraulic+systems'
```

## 설치·실행

### 현재 컴퓨터에서 바로 실행

가상환경·의존성·프론트엔드 빌드를 준비해 두었습니다. `backend/.env`에 `OPENAI_API_KEY`를 입력한 뒤 실행하세요. 키는 브라우저나 저장소에 넣지 않습니다.

```bash
cd backend
source .venv/bin/activate
uvicorn pump_guard.main:app --host 127.0.0.1 --port 8001 --reload
```

대시보드: **http://127.0.0.1:8001/** · API 문서: http://127.0.0.1:8001/docs

설비 선택 → 예제값 입력 또는 직접 측정값 입력 → 상태 분석 시작 순서로 사용합니다. 5단계 진행, 취소, 판정·원인 후보·근거·정비 항목을 표시합니다. `점검 기록` 메뉴에서는 수행한 점검 항목과 결과를 저장·조회합니다. 예제값과 이력은 실제 측정 데이터가 아닙니다.

별도 정비 계획 페이지는 없습니다. **설비 상태 분석 → 분석 시작 → 이 결과로 정비 계획 묻기** 순서로 챗봇에 질문합니다. 챗봇은 연결된 분석 결과의 점검 항목, 저장된 점검 기록, 날짜별 실측 이력(있는 경우)을 함께 조회합니다. 날짜별 기록은 설비 상태 분석 화면 아래의 접이식 **실측 이력 등록** 영역에서 `observed_at,value` CSV로 추가할 수 있습니다. 시각에는 `2026-10-01T09:00:00+09:00`처럼 시간대가 필요합니다. 합성 운전 이력과 UCI 실험 주기는 현장 설비의 수명 이력으로 사용하지 않습니다. 설비별 한계값·운전 시간·고장/교체 기록 없이 잔여 수명이나 교체일을 계산하지 않습니다.

분석 결과 아래의 **이전 수치와 변화**에서 최근 분석 입력 100건과 등록 실측 최대 2,000건을 출처별로 전환해 봅니다. 진동·베어링 온도·흡입/토출 압력·유량·전류의 관측 시각, 첫 기록 대비 변화 및 관측 범위를 보여줍니다. 등록 실측에 한해서 담당자가 확인한 한계값을 넣으면 최소 6건·14일 이상의 이력과 일관된 변화가 있을 때 제한적 도달 추정을 표시합니다. 이 날짜는 고장일이나 교체일이 아닙니다. 상담의 변화·정비 계획 질문에는 등록 실측 요약을 별도 근거로 조회하며, 관련 없는 명백한 질문은 모델 호출 전에 안내문으로 돌려 토큰 사용을 줄입니다.

AI 상담에서 정상 압력·유량 범위를 물으면 설비별 허용 범위가 등록되어 있는지 먼저 확인합니다. 현재 합성 설비에 있는 정격 유량은 정상 범위와 구분해서 답하며, 등록되지 않은 압력 허용 범위는 임의의 숫자로 제시하지 않습니다.

### 처음 설치하거나 프론트엔드를 수정한 경우

먼저 프론트엔드를 설치·빌드합니다.

```bash
cd frontend
npm ci
npm run build
```

빌드된 `frontend/dist`가 있으면 FastAPI가 `/`에서 제공합니다. 첫 빌드 후에는 백엔드를 다시 시작하세요. 소스 파일을 수정하면 다시 빌드해야 합니다. `npm run preview`는 API를 프록시하지 않으므로 전체 앱 확인에는 FastAPI 주소를 사용하세요.

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
python -m pip install --no-deps -e .
cp .env.example .env  # 처음 설치할 때만 실행
```

기본 OpenAI 모델은 `gpt-5.4-mini`입니다. `backend/.env`에서 `PUMP_GUARD_OPENAI_MODEL`을 변경할 수 있습니다. 이 앱은 OpenAI Responses API의 구조화 출력을 사용하며, API 키는 서버에서만 읽습니다. 설정 예:

OpenAI 모드에서는 분석 측정값, 조회된 설비·점검 자료와 상담 질문·최근 대화가 모델 요청에 포함되어 외부 API로 전송됩니다. 요청에 `store: false`를 설정합니다.

```dotenv
PUMP_GUARD_LLM_MODE=openai
OPENAI_API_KEY=발급받은_키
PUMP_GUARD_OPENAI_MODEL=gpt-5.4-mini
```

기존 `.env`가 있으면 파일을 덮어쓰지 말고 위 항목만 추가·수정하세요. 한 번의 작은 모델 호출로 연결을 확인할 수 있습니다. 이 확인에도 API 사용량이 발생합니다.

```bash
cd backend
source .venv/bin/activate
python -m pump_guard.smoke_openai
uvicorn pump_guard.main:app --host 127.0.0.1 --port 8001 --reload
```

- Swagger: <http://127.0.0.1:8001/docs>
- 생존 확인: <http://127.0.0.1:8001/health> (`provider_configured`는 키 설정 여부이며 API 연결 확인은 `smoke_openai`를 사용)
- `PUMP_GUARD_LANGSMITH_API_KEY`는 실행 추적용 별도 키입니다.

```bash
curl -N -X POST http://127.0.0.1:8001/api/v1/analyses/stream \
  -H 'Content-Type: application/json' \
  --data-binary @example-request.json
```

외부 연결 없이 실행하려면 `.env`에서 `PUMP_GUARD_LLM_MODE=mock`으로 변경합니다. `.env`가 없을 때 코드 기본값도 `mock`입니다. 두 모드 모두 LangGraph를 사용합니다. Mock은 결정적 순차 워크플로이며, OpenAI 모드에서 역할별 LLM 에이전트가 실행됩니다. 기존 Ollama 설정은 선택적으로 유지합니다.

프론트엔드 개발 중에는 백엔드를 실행한 상태에서 별도 터미널로 `cd frontend && npm run dev`를 실행하고 http://127.0.0.1:5173/ 에 접속합니다. Vite가 `/api`와 `/health`를 8001번 포트로 전달합니다. 다른 포트는 `PUMP_GUARD_API_TARGET=http://127.0.0.1:8000 npm run dev`처럼 지정합니다. Vue가 API 응답과 생성 텍스트를 일반 텍스트로 표시합니다. 입력값이나 설비를 바꾸면 이전 분석 결과를 비웁니다.

## 분석 흐름

| 노드 | 동작 |
| --- | --- |
| `input` | 설비정보 에이전트가 사양·운전 이력·점검 이력 MCP 도구를 선택해 조회 |
| `condition` | 상태진단 에이전트가 규칙 평가·이력 비교 도구를 호출하고 근거 전달 |
| `cause` | 고장원인 에이전트가 MCP 매뉴얼을 검색하고 관측 근거와 후보 연결 |
| `maintenance` | 정비계획 에이전트가 선택된 매뉴얼 원문을 조회하고 점검 항목 구성 |
| `safety` | 안전검수 에이전트가 제안 검증 도구를 호출하고 통과·재작업·거부 결정 |

OpenAI 모드에는 5개 역할별 지침·출력 스키마·허용 도구·관측 기록을 가진 에이전트가 있습니다. 각 에이전트가 `도구 선택 → 도구 관측 → 구조화 결과 전달`을 반복합니다. LangGraph는 공유 상태와 역할 간 전달을 관리하며, 안전검수 결과에 따라 원인 또는 정비계획 역할로 돌아갑니다. 설정한 OpenAI 모델을 5개 역할이 공유합니다.

에이전트당 기본 4턴, 검수 재작업은 전체 기본 1회로 제한합니다. 필수 도구를 조회하지 않은 완료, 역할 밖 도구, 잘못된 근거·매뉴얼·계획 참조, 위험도 변경, 반복 한도 초과는 실패로 처리합니다. 모델이 생성한 자유 문장을 정비 지시로 사용하지 않으며 최종 고장명·정비 항목은 MCP 원문에서 구성합니다. 근거 참조 검증은 인과관계 검증을 대신하지 않습니다.

최종 코드 검증에서 Rule Engine을 다시 계산하므로 LLM이 판정을 낮출 수 없습니다. 정상·누락 입력도 역할별 검토를 거치지만, 원인 후보는 비어 있을 수 있습니다. 검수 통과는 설비 운전 승인이 아닙니다. 전체 분석 제한 300초 안에 각 역할의 여러 호출이 수행되므로 단일 워크플로보다 느릴 수 있습니다.

`llm.py`는 LangChain `BaseChatModel`을 구현한 비동기 OpenAI Responses 어댑터를 제공합니다. `text.format`에 엄격한 JSON Schema를 보내고 반환값을 Pydantic으로 다시 검증합니다. 요청에는 `store: false`를 사용하고 API 키는 서버 메모리에서만 요청 헤더에 넣습니다. LangChain Runnable로 도구 호출도 추적합니다. 자동 JSON 복구나 Mock 결과로의 전환은 없습니다. SSE는 단계 단위이며 토큰 스트리밍은 아닙니다.

## MCP 수명주기·동시성

- 분석 한 건마다 equipment·knowledge stdio MCP 세션을 열고 5단계 전체에서 재사용한 뒤 종료합니다.
- SDK task group의 생성·종료는 같은 상위 작업에서 수행합니다. 세션을 그래프 노드에서 열고 다른 노드에서 닫지 않습니다.
- 분석 간 세션·State를 공유하지 않습니다. 같은 서버의 도구 호출은 직렬화합니다.
- 프로세스당 분석 2건, LLM 호출 1건이 기본 상한입니다. 대기 시간도 전체 분석 제한 시간에 포함됩니다.
- 완료·오류·시간 초과·클라이언트 연결 해제 시 세션과 하위 작업을 정리합니다.
- 설비 조회·점검 저장 REST API는 도구 호출 단위 세션을 사용합니다. 분석 동시성 상한은 이 조회 API에는 적용되지 않습니다.

`.env.example` 기준 전체 분석 제한은 300초, 개별 OpenAI HTTP 제한은 120초, heartbeat는 10초입니다. 모델 로딩과 동시 요청 대기에 따라 전체 제한에 도달할 수 있습니다. 여러 Uvicorn worker 사이에는 상한이 공유되지 않습니다.

## API·SSE 계약

| Method | 경로 | 기능 |
| --- | --- | --- |
| GET | `/health` | 프로세스 상태·설정 모드·모델 |
| GET | `/api/v1/equipment` | 모의 설비 목록 (`P-101`, `P-102`) |
| GET | `/api/v1/equipment/{id}` | 설비 사양 |
| GET | `/api/v1/equipment/{id}/operating-history` | 합성 관측값 6개 |
| GET | `/api/v1/equipment/{id}/maintenance-history` | 저장한 점검 최신 100건 |
| POST | `/api/v1/inspections` | 점검 SQLite 저장 |
| POST | `/api/v1/analyses` | 최종 JSON |
| POST | `/api/v1/analyses/stream` | 단계 진행·최종 결과 SSE |

입력은 허용된 설비 ID·측정 수치·점검 항목으로 제한합니다. 자유 입력·추가 필드·문자열 수치·불리언 수치·비유한값·범위 밖 수치는 거부하며 본문은 기본 16 KiB로 제한합니다. 입력 오류에 원래 값을 재출력하지 않습니다. 누락 수치가 하나라도 있으면 `status: null`과 `missing_fields`를 반환합니다.

기존 `candidate_causes`, `checklist`, `status` 필드를 유지하고 `model`, `evidence`, `cause_details`, `maintenance_plan`, `metrics`, `architecture`, `agent_reports`를 추가했습니다. `metrics`에는 전체 시간, 5개 노드별 시간, LLM 호출별 시간·입출력 토큰 수가 들어갑니다. 토큰 수는 모델 공급자의 보고값이며 누락되면 `null`입니다. 비용·고장 확률을 추정하지 않습니다.

일반적인 SSE 순서는 `started` → 각 역할 `started/completed` 10개 → `result`입니다. 검수 재작업이 있으면 해당 역할의 단계 이벤트가 추가되므로 이벤트 수를 고정하지 않습니다. 단계 데이터에는 역할과 시도 횟수·완료 결정이 포함됩니다. 실패하면 `result` 대신 `error` 후 종료합니다. 진행이 없으면 `: heartbeat` 주석을 보냅니다. POST이므로 Vue에서는 `fetch`·`ReadableStream`·`AbortController`를 사용합니다. UTF-8 chunk를 버퍼링하고 빈 줄 기준으로 이벤트를 파싱해야 합니다. 자동 재접속·재생은 없습니다. `result` 없이 연결이 끝나면 실패로 취급합니다.

프록시는 응답 버퍼링을 끄고 read timeout을 분석 제한보다 길게 설정해야 합니다. API는 `X-Accel-Buffering: no`, `Cache-Control: no-cache, no-transform`을 보냅니다.

| 코드 | JSON API HTTP | 의미 |
| --- | --- | --- |
| `INVALID_INPUT` / `EQUIPMENT_NOT_FOUND` / `BODY_TOO_LARGE` | 422 / 404 / 413 | 입력·설비·본문 제한 |
| `OPENAI_KEY_MISSING` / `OPENAI_AUTH_FAILED` / `OPENAI_MODEL_UNAVAILABLE` | 503 | 키 미설정·인증 실패·모델 접근 실패 |
| `OLLAMA_MODEL_UNAVAILABLE` | 503 | 선택적 Ollama 모드의 모델 또는 endpoint 없음 |
| `UPSTREAM_UNAVAILABLE` / `PROVIDER_RATE_LIMIT` | 503 | 연결·공급자 오류 / 429 |
| `UPSTREAM_TIMEOUT` | 504 | 개별 또는 전체 시간 초과 |
| `INVALID_MODEL_OUTPUT` | 502 | 스키마·근거 검증 실패 또는 잘린 응답 |
| `ANALYSIS_FAILED` | 500 | 그 외 오류 |

스트림 시작 이후에는 HTTP 200 안의 `error` 이벤트로 전달합니다. API와 애플리케이션 오류 로그에 원문 예외·프롬프트·키를 출력하지 않습니다.

## LangSmith

기본은 꺼져 있으며 OpenAI API 실행에 필요하지 않습니다. 활성화하려면 `.env`에 다음을 설정합니다.

```dotenv
PUMP_GUARD_LANGSMITH_TRACING=true
PUMP_GUARD_LANGSMITH_API_KEY=your-key
PUMP_GUARD_LANGSMITH_PROJECT=pump-guard
PUMP_GUARD_LANGSMITH_ENDPOINT=https://api.smith.langchain.com
# 키가 여러 워크스페이스에 속한 경우에만 입력
PUMP_GUARD_LANGSMITH_WORKSPACE_ID=
```

`backend/.env`에 이미 빈 키 입력란이 있습니다. 값을 넣고 추적을 `true`로 바꾼 다음 백엔드를 재시작하세요. 관리자 모니터링의 **LangSmith 연결 확인**은 원격 API 인증을 시험하고, 새 분석·상담 실행을 선택한 뒤 **LangSmith 기록 확인**은 해당 실행이 실제 업로드됐는지 검증합니다. 키가 여러 워크스페이스에 속하면 워크스페이스 ID를 입력하고, US 외 리전이면 해당 리전의 API endpoint를 사용하세요. 명시적인 tracing context와 입력·출력 비공개 클라이언트를 사용하므로 그래프 State·센서값·프롬프트·완성 응답은 추적 입력·출력에서 가립니다. 일반 `LANGSMITH_TRACING` 환경변수만으로는 이 앱의 추적을 켜지 않습니다.

## AI 상담

왼쪽 **AI 상담** 메뉴에서 설비 사양·운전/점검 이력·정비 매뉴얼을 질문할 수 있습니다. 분석을 완료한 뒤 **이 결과에 질문하기**를 누르면 해당 분석의 서버 저장 스냅샷을 연결합니다. 과거 버전에서 실행해 스냅샷이 없는 분석은 새로 실행해야 합니다.

- “이 설비의 사양을 알려줘”
- “최근 점검 이력이 있어?”
- “캐비테이션 매뉴얼의 점검 항목을 알려줘”
- 분석 연결 후 “이 결과와 점검 기록으로 정비 계획을 세워줘”, “왜 주의로 나왔어?”, “고장 후보를 고른 근거가 뭐야?”

OpenAI를 사용하는 상담 조정 → 필요한 읽기 전용 도구 조회 → 출처를 붙인 답변 → 별도 답변 검수의 LangGraph를 실행합니다. 매 질문마다 수치 분석용 에이전트 5개를 모두 실행하지 않습니다. OpenAI 어댑터·MCP·코드 판정·실행 추적과 동시성 제한을 공유합니다. LangSmith 키는 필요하지 않습니다.

POST SSE로 4단계 진행을 표시합니다. AI의 출처 연결이나 검수가 실패하면 한 번 수정·재검수하고, 계속 실패할 때는 조회한 원자료에서 서버가 직접 만든 문장만 `저장 자료 요약`으로 표시합니다. 이 요약은 AI 추론 결과가 아니며 추정 상태·고장 확률을 새로 만들지 않습니다. 내부 `source_ids`는 답변 본문에서 제거하고 출처 카드는 유지합니다. 분석 미연결·근거 부족은 추가 정보를 요청합니다. 인용 존재 검증과 LLM 검수는 사실성·인과관계 보증을 대신하지 않습니다.

완료된 질문·답변과 인용 근거는 SQLite에 저장합니다. 대화 목록에서 다시 열거나 삭제할 수 있습니다. 취소·오류 전에 완료되지 않은 대화 쌍은 저장하지 않습니다. 서버에서 이미 완료된 답변은 클라이언트 취소 이후에도 남을 수 있습니다. 세션별 동시 답변은 DB lease로 막고 취소·시간 초과 시 해제합니다. 강제 종료 시 lease는 분석 제한 시간+30초 후 만료됩니다.

대화는 생성 당시 설비·분석 또는 UCI 실험 주기에 고정됩니다. 서버의 검증된 분석을 조회하며 클라이언트에서 임의의 분석 본문·system/tool 역할을 전달할 수 없습니다. 수치 입력이나 점검 저장은 상담이 자동 수행하지 않습니다. 질문은 2,000자, 모델의 이전 대화 맥락은 최근 6개 메시지 각 최대 500자, UI 복원은 최근 100개 메시지, 목록은 설비별 최근 30개 대화입니다. 점검 조회 근거는 최신 10건으로 제한합니다.

| Method | 경로 | 기능 |
| --- | --- | --- |
| POST | `/api/v1/chat/sessions` | 설비·선택적 분석 ID 또는 UCI 실험 주기로 대화 생성 |
| GET | `/api/v1/chat/sessions?equipment_id=P-101` | 설비별 대화 목록 |
| GET | `/api/v1/chat/sessions/{id}` | 저장된 대화·연결 분석 조회 |
| DELETE | `/api/v1/chat/sessions/{id}` | 대화와 메시지 삭제 |
| POST | `/api/v1/chat/sessions/{id}/messages` | `{"message":"질문"}`의 최종 JSON 답변 |
| POST | `/api/v1/chat/sessions/{id}/messages/stream` | 같은 입력의 단계·최종 답변 SSE |

### 실측 기록과 점검 이력으로 상태 추정

`공개 실측 데이터` → 실험 주기 선택 → `실측 데이터로 AI 상담` 순서로 상담을 엽니다. 점검 항목·결과·메모는 같은 화면에서 먼저 저장할 수 있습니다. 질문 예: “최근 점검 기록과 실측 데이터를 보고 이 장비의 상태를 추측해줘.”

- 대상은 `UCI-447`로 고정합니다. 세션 생성 입력 예: `{"equipment_id":"UCI-447","research_cycle_id":2205}`. 다른 설비에 실험 주기를 연결하거나 존재하지 않는 주기를 지정하면 거부합니다.
- `get_measured_history` MCP 도구는 선택 주기까지 최대 5개 실험의 17개 센서 평균과 원본 상태 라벨을 조회합니다. 주기는 서버가 세션에서 결정하므로 모델이 바꾸지 못합니다.
- 점검 저장 API에는 선택적 `note`(최대 1,000자)를 추가했습니다. UCI 기록은 `research_cycle_id`가 필수입니다. P-101/P-102에는 실험 주기를 지정할 수 없습니다.
- 상태 추정 의도에서는 점검 기록과 실측/운전 기록을 모두 조회하고, 답변이 두 종류의 근거를 인용하는지 서버에서 검사합니다. OpenAI 답변·검수 역할은 관측 사실, 잠정 해석, 추가 확인 항목을 구분합니다. 기록이 없으면 근거 부족으로 설명합니다.
- UCI 점검 근거는 서버가 조회한 최신 100건 중 선택 주기 이하에 연결된 최근 10건입니다. 입력 시각은 실험 시각이 아니며 원본 측정값·라벨을 변경하지 않습니다. 실험 주기 간 수치 차이를 현장 열화 추세로 간주하지 않습니다.
- P-101/P-102의 점검 기록도 상태 추정에 사용할 수 있지만, UCI 측정값을 해당 설비의 현재 측정값으로 대입하지 않습니다. `analysis_status`는 기존 분석 판정만 유지하며 새로운 확정 판정·고장 확률을 만들지 않습니다.
- 가공 데이터 경로는 `PUMP_GUARD_RESEARCH_DATABASE_PATH`로 변경할 수 있으며 MCP 자식 프로세스에도 전달합니다. 기본값은 프로젝트의 `research/processed/uci447.sqlite3`입니다.

흡입·토출 압력, 유량, 전류는 저장된 수치 분석의 위험도 판정에 포함되지 않습니다. 압력 관련 상담은 이를 밝히고 설비별 기준 자료가 필요하다고 설명합니다.

현재 인증·사용자별 대화 격리·자동 보관 만료는 없습니다. 로컬 개발용으로 사용합니다. 대화 삭제는 상담 메시지를 지우며 분석 스냅샷이나 관리자 실행 메타데이터는 별도로 남습니다.

## 관리자 모니터링

왼쪽 `관리자 모니터링` 메뉴에서 분석·상담 구분, 최근 실행 목록·성공/실패/취소·평균 시간·보고된 토큰 수와 역할별 도구·턴·재작업 기록을 확인합니다. 집계는 최근 100건, 목록은 최신 50건입니다. SQLite에 메타데이터를 저장하며 관리자 API에 센서 본문·프롬프트·원문 예외·키를 기록하지 않습니다. 브라우저가 열린 동안 10초마다 갱신합니다.

- `GET /api/v1/admin/monitoring`: 최근 집계·LangSmith 설정 상태·역할 구성
- `GET /api/v1/admin/runs?limit=50`: 최근 실행 목록 (최대 100)
- `GET /api/v1/admin/runs/{run_id}`: 역할별 활동과 호출 측정값
- `GET /api/v1/admin/runs/{run_id}/trace`: LangSmith 실제 실행 조회 후 링크 반환

LangSmith가 꺼져 있으면 로컬 기록만 표시합니다. 설정만으로 연결 성공을 표시하지 않으며, `LangSmith 기록 확인` 버튼이 SDK `read_run`으로 원격 실행을 확인한 경우에만 링크를 제공합니다. 과거 추적 비활성 실행이 설정 변경 후 자동 업로드되지는 않습니다. 키·프로젝트·지역 endpoint를 설정하고 새 분석을 실행하세요. 서버 종료 시 추적 클라이언트를 정리합니다.

현재 관리자 기능은 로컬 개발용이며 로그인·권한 분리는 아직 없습니다. 로컬 기록의 자동 삭제·프로세스 강제 종료 후 중단 상태 복구는 아직 없습니다. 최종 분석 스냅샷과 대화 본문은 관리자 메타데이터와 별도 테이블에 저장합니다. LangSmith 추적에는 기본적으로 입력·출력을 가립니다.

## 검증

가져온 UCI 실측 자료와 임시 분석·점검 기록으로 상담 경로를 반복 점검할 수 있습니다. 기본 오프라인 평가는 기존 Ollama 모의 전송에 의도적으로 잘못된 형식의 답변을 주어 출처 연결 오류 시 `저장 자료 요약`으로 마무리되는지 확인합니다. 생성된 보고서는 [chat_validation.json](research/reports/chat_validation.json)입니다. 이 수치는 모델 진단 정확도가 아닙니다.

```bash
cd backend
.venv/bin/python -m pump_guard.evaluate_chat
```

실제 OpenAI 답변 경로는 키를 설정한 뒤 별도 실행으로 확인할 수 있습니다. API 사용량이 발생하며, 생성 결과가 현장 진단 정확도를 검증하는 것은 아닙니다. 선택적으로 유지한 Ollama 경로도 확인할 수 있습니다.

```bash
.venv/bin/python -m pump_guard.evaluate_chat --live-openai
.venv/bin/python -m pump_guard.evaluate_chat --live-ollama
```


```bash
cd backend
source .venv/bin/activate
pytest -q
python -m pip check
python -m pump_guard.smoke_openai  # 실제 OpenAI 연결 확인: 키가 있어야 하며 API 사용량 발생
```

기존 회귀 테스트와 에이전트·관리자 테스트를 실행합니다. 실제 LangGraph·LangChain·stdio MCP를 사용하며 OpenAI Responses 및 기존 Ollama HTTP는 `httpx.MockTransport`로 대체합니다. 역할별 도구 선택·금지 도구·판정 변경 방지·검수 재작업·반복 한도·메타데이터 영속성·시간 초과 기록·LangSmith 원격 조회 분기를 검증합니다. 원격 LangSmith 테스트는 SDK 클라이언트 대역을 사용합니다.

백엔드·프론트엔드 테스트와 Vite 빌드로 회귀 검증합니다. 상담의 서버 스냅샷·대화 격리·저장/삭제·위조 출처·판정 변경·금지 도구·동시 질문·취소/연결 해제·DB 이행을 확인했습니다. 관리자·상담 화면의 실제 브라우저 동작과 실제 모델의 도구 선택·답변 품질은 별도 검증이 필요합니다.

이 환경에는 OpenAI API 키가 없어 **실제 모델 추론 품질·속도는 미검증**입니다. OpenAI Responses 호출은 MockTransport로 검증했습니다. 네트워크 다운로드도 제한되어 로컬 pip 캐시로 설치했습니다. `requirements.lock.txt`는 이 Python 3.11 환경의 고정 버전 목록이며 OS 전체를 위한 해시 잠금 파일은 아닙니다.

## 현재 한계·다음 단계

진동 `2.0 / 3.5 / 5.0 mm/s`, 베어링 온도 `70 / 80 / 90 °C`는 임의 데모 임계값입니다. 압력·유량·전류는 형식·범위 검사만 수행합니다. 이력 비교는 합성 관측값의 수치 차이이며 검증된 추세 진단이 아닙니다. 인증·인가, 사용자별 격리, 호출량 제한, 영속 큐, 운영 배포 구성은 아직 없습니다.

Vue 대시보드·POST SSE 화면·점검 저장 화면은 구현했습니다. 프론트엔드의 `npm test`는 UTF-8 분할, CR/LF, heartbeat, 스트림 오류·중도 종료·취소를 검증합니다. `npm run build`로 프로덕션 빌드를 확인합니다. 이 실행 환경은 로컬 포트 접근뿐 아니라 포트 바인딩도 차단되어 실제 브라우저에서 화면과 전체 흐름을 확인하지 못했습니다.

다음은 키 입력 후 실제 OpenAI 역할별 실행 품질 확인, LangSmith 키 설정 후 원격 기록 확인, 브라우저에서 분석·재작업·관리자 화면 확인입니다. AI 상담 구현 범위와 후속 설계는 [CHATBOT_PLAN.md](CHATBOT_PLAN.md)를 참고합니다. 실제 운전 적용에는 승인된 설비별 판정 기준과 실제 이력·정비지식 연결이 별도로 필요합니다.

구현 참고: [OpenAI Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create), [OpenAI 구조화 출력](https://developers.openai.com/api/docs/guides/structured-outputs), [LangGraph API](https://reference.langchain.com/python/langgraph/graph), [LangSmith 추적](https://docs.langchain.com/langsmith/trace-with-langgraph).

프론트엔드 참고: [Vue 도구 구성](https://vuejs.org/guide/scaling-up/tooling.html), [Vite 개발 서버 설정](https://vite.dev/config/server-options), [ReadableStream](https://developer.mozilla.org/en-US/docs/Web/API/ReadableStream).
