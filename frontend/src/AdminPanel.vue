<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { request } from './api.js'

const overview = ref(null), runs = ref([]), detail = ref(null), selected = ref(''), filter = ref('all')
const page = ref(1), pageSize = 6
const loading = ref(false), detailLoading = ref(false), error = ref(''), detailError = ref(''), trace = ref(null), checkingTrace = ref(false), connection = ref(null), checkingConnection = ref(false)
const stateNames = { queued: '대기', running: '실행 중', completed: '완료', failed: '실패', cancelled: '취소' }
const decisionNames = { source_summary: '저장 자료 요약으로 응답', invalid_citation: '출처 연결 오류', conflicting_status: '판정 값 불일치', missing_analysis_citation: '분석 근거 누락', missing_assessment_citations: '상태 추정 근거 누락', completed: '완료', handoff: '다음 역할에 전달', approve: '검수 통과', approved: '검수 통과', rejected_unsupported_claim: '출처로 확인되지 않은 설명', rejected_conflicting_status: '저장된 판정과 불일치', rejected_insufficient_sources: '근거 자료 부족', rejected_supported: '검수 결과 불일치', revise_cause: '원인 재검토 요청', revise_maintenance: '계획 재검토 요청', failed: '실패', cancelled: '취소' }
const agentNames = { input: '설비정보', condition: '상태진단', cause: '고장원인', maintenance: '정비계획', safety: '안전검수', chat_router: '상담 조정', chat_retrieval: '상담 근거 조회', chat_answer: '상담 답변', chat_repair: '상담 답변 수정', chat_review: '상담 검수' }
const visibleRuns = computed(() => runs.value.filter(r => filter.value === 'all' || r.state === filter.value))
const pageCount = computed(() => Math.max(1, Math.ceil(visibleRuns.value.length / pageSize)))
const pagedRuns = computed(() => visibleRuns.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const pageStart = computed(() => visibleRuns.value.length ? (page.value - 1) * pageSize + 1 : 0)
const pageEnd = computed(() => Math.min(page.value * pageSize, visibleRuns.value.length))
watch(filter, () => { page.value = 1 })
watch(pageCount, count => { if (page.value > count) page.value = count })
const summary = computed(() => overview.value?.summary)
let refreshController, detailController, timer
const date = value => value ? new Date(value).toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—'
const seconds = value => value == null ? '—' : `${(value / 1000).toFixed(1)}초`
const message = err => err.name === 'TypeError' ? '관리자 API에 연결하지 못했습니다.' : err.message
const tokenTotal = computed(() => (summary.value?.input_tokens || 0) + (summary.value?.output_tokens || 0))
const traceUrl = computed(() => {
  try { const url = new URL(trace.value?.url); return ['http:', 'https:'].includes(url.protocol) ? url.href : null } catch { return null }
})
async function refresh() {
  if (loading.value) return
  refreshController = new AbortController()
  loading.value = true; error.value = ''
  try {
    const [info, list] = await Promise.all([request('/api/v1/admin/monitoring', { signal: refreshController.signal }), request('/api/v1/admin/runs', { signal: refreshController.signal })])
    overview.value = info; runs.value = list.items
    if (!selected.value && list.items.length) await selectRun(list.items[0].run_id)
    else if (selected.value && !detailLoading.value) await selectRun(selected.value, true)
  } catch (err) { if (err.name !== 'AbortError') error.value = message(err) }
  finally { loading.value = false }
}
async function selectRun(id, preserveTrace = false) {
  detailController?.abort(); detailController = new AbortController()
  const signal = detailController.signal
  selected.value = id; detailLoading.value = true; detailError.value = ''
  if (!preserveTrace) { detail.value = null; trace.value = null }
  try {
    const data = await request(`/api/v1/admin/runs/${encodeURIComponent(id)}`, { signal })
    if (!signal.aborted) detail.value = data
  } catch (err) { if (!signal.aborted) detailError.value = message(err) }
  finally { if (!signal.aborted) detailLoading.value = false }
}
async function checkTrace() {
  const id = selected.value
  checkingTrace.value = true
  try {
    const data = await request(`/api/v1/admin/runs/${encodeURIComponent(id)}/trace`)
    if (selected.value === id) trace.value = data
  } catch (err) { if (selected.value === id) trace.value = { state: 'unavailable', message: message(err) } }
  finally { checkingTrace.value = false }
}
async function checkConnection() {
  if (checkingConnection.value) return
  checkingConnection.value = true; connection.value = null
  try { connection.value = await request('/api/v1/admin/langsmith/connection') }
  catch (err) { connection.value = { state: 'unavailable', message: message(err) } }
  finally { checkingConnection.value = false }
}
onMounted(() => { refresh(); timer = setInterval(() => { if (!document.hidden) refresh() }, 10000) })
onBeforeUnmount(() => { clearInterval(timer); refreshController?.abort(); detailController?.abort() })
</script>

<template>
  <section class="admin-panel">
    <div class="admin-toolbar"><span>최근 100건 집계 · 목록 최신 50건 · 10초마다 갱신</span><button class="text-button" :disabled="loading" @click="refresh">{{ loading ? '조회 중…' : '새로고침 ↻' }}</button></div>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <div class="admin-stats"><div class="card"><small>분석·상담 실행</small><strong>{{ summary?.runs ?? '—' }}</strong><span>대기·실행 중 {{ summary?.active ?? 0 }}건</span></div><div class="card"><small>완료 / 실패</small><strong>{{ summary?.completed ?? '—' }} <em>/ {{ summary?.failed ?? '—' }}</em></strong><span>사용자 취소 {{ summary?.cancelled ?? 0 }}건</span></div><div class="card"><small>완료 실행 평균 시간</small><strong>{{ seconds(summary?.average_ms) }}</strong><span>에이전트·MCP·대기 시간 포함</span></div><div class="card"><small>보고된 입출력 토큰</small><strong>{{ tokenTotal.toLocaleString() }}</strong><span>사용량 미보고 호출 {{ summary?.unreported_usage_calls ?? 0 }}건</span></div></div>
    <section class="card tracing-card"><div><h2>LangSmith 추적</h2><p>{{ connection?.state === 'connected' ? '원격 연결 확인됨' : overview?.langsmith.enabled ? '설정됨 · 원격 연결 확인 필요' : '비활성 · 현재는 로컬 실행 기록을 표시합니다' }}</p><small v-if="overview">프로젝트 {{ overview.langsmith.project }}</small></div><span class="mode-tag">{{ connection?.state === 'connected' ? '연결됨' : overview?.langsmith.enabled ? '확인 필요' : '선택 기능 · 꺼짐' }}</span><button class="secondary-button" :disabled="checkingConnection || !overview?.langsmith.enabled" @click="checkConnection">{{ checkingConnection ? '연결 확인 중…' : 'LangSmith 연결 확인' }}</button><p v-if="connection" class="trace-message" role="status">{{ connection.message }}</p><p v-if="overview && !overview.langsmith.enabled" class="trace-instructions">백엔드 .env의 PUMP_GUARD_LANGSMITH_API_KEY에 키를 넣고 PUMP_GUARD_LANGSMITH_TRACING=true로 바꾼 뒤 서버를 재시작하세요. OpenAI 키와는 별개입니다.</p></section>
    <div class="admin-grid"><section class="card"><div class="section-heading"><div><h2>실행 기록</h2></div><label class="filter-label">상태<select v-model="filter"><option value="all">전체</option><option v-for="(label, state) in stateNames" :key="state" :value="state">{{ label }}</option></select></label></div><p v-if="!visibleRuns.length" class="inline-empty">{{ loading ? '실행 기록을 조회하고 있습니다.' : '해당하는 실행 기록이 없습니다. 설비 상태 분석을 실행해 보세요.' }}</p><div class="run-list"><button v-for="run in pagedRuns" :key="run.run_id" :class="['run-row', { selected: selected === run.run_id }]" @click="selectRun(run.run_id)"><span><strong>{{ run.equipment_id }} <span class="reference-tag">{{ run.kind === 'chat' ? '상담' : '분석' }}</span></strong><small>{{ date(run.started_at) }}</small></span><span><span :class="['state-pill', run.state]">{{ stateNames[run.state] }}</span><small>{{ run.model || '규칙 분석' }}</small></span></button></div><nav v-if="visibleRuns.length > pageSize" class="run-pagination" aria-label="실행 기록 페이지"><span>{{ pageStart }}~{{ pageEnd }} / {{ visibleRuns.length }}건</span><div><button type="button" :disabled="page === 1" @click="page--">이전</button><span>{{ page }} / {{ pageCount }}</span><button type="button" :disabled="page === pageCount" @click="page++">다음</button></div></nav></section>
    <section class="card run-detail"><div class="section-heading"><div><h2>에이전트 실행 상세</h2></div><span v-if="detail" class="reference-tag">{{ stateNames[detail.state] }}</span></div><p v-if="detailError" class="alert error" role="alert">{{ detailError }}</p><p v-if="!detail" class="inline-empty">{{ detailLoading ? '실행 정보를 읽고 있습니다.' : '왼쪽에서 실행 기록을 선택하세요.' }}</p><template v-else><dl class="run-facts"><div><dt>실행 ID</dt><dd>{{ detail.run_id }}</dd></div><div><dt>종류 / 설비</dt><dd>{{ detail.kind === 'chat' ? 'AI 상담' : '수치 분석' }} / {{ detail.equipment_id }}</dd></div><div><dt>실행 모드</dt><dd>{{ detail.equipment_id }} / {{ detail.mode === 'mock' ? '규칙 분석' : detail.mode === 'openai' ? 'OpenAI' : 'Ollama' }}</dd></div><div><dt>전체 소요 시간</dt><dd>{{ seconds(detail.metrics.total_ms) }}</dd></div><div v-if="detail.error_code"><dt>오류 코드</dt><dd>{{ detail.error_code }}</dd></div></dl><h3 class="subheading">역할별 도구 선택과 결과 전달</h3><p v-if="!detail.agents.length" class="inline-empty">{{ detail.mode === 'mock' ? '규칙 분석 실행입니다. AI 모델 모드에서 에이전트별 실행을 기록합니다.' : '에이전트 결과를 기다리고 있습니다.' }}</p><ol class="agent-events"><li v-for="(agent, index) in detail.agents" :key="index"><span class="agent-index">{{ index + 1 }}</span><div><strong>{{ agentNames[agent.agent] }} 에이전트 <small>시도 {{ agent.attempt }}</small></strong><p>{{ decisionNames[agent.decision] || agent.decision }} · {{ agent.turns }}턴 · {{ seconds(agent.latency_ms) }}</p><div class="tool-tags"><code v-for="(tool, ti) in agent.tools" :key="`${tool}:${ti}`">{{ tool }}</code></div></div></li></ol><details class="call-details"><summary>LLM 호출별 시간·토큰 · {{ detail.metrics.llm_calls?.length || 0 }}회</summary><div v-for="(call, index) in detail.metrics.llm_calls || []" :key="index" class="call-row"><span>{{ agentNames[call.stage] || call.stage }} #{{ index + 1 }}</span><span>{{ seconds(call.latency_ms) }} · 입력 {{ call.input_tokens ?? '미보고' }} / 출력 {{ call.output_tokens ?? '미보고' }}</span></div></details><div class="trace-actions"><button class="secondary-button" :disabled="checkingTrace || !detail.tracing_enabled" @click="checkTrace">{{ checkingTrace ? '원격 기록 확인 중…' : 'LangSmith 기록 확인' }}</button><a v-if="traceUrl" :href="traceUrl" target="_blank" rel="noopener noreferrer">LangSmith에서 열기 ↗</a></div><p v-if="trace?.remote" class="trace-message">LangSmith 보고: {{ stateNames[trace.remote.state] }} · {{ seconds(trace.remote.latency_ms) }} · 입력 {{ trace.remote.input_tokens ?? '미보고' }} / 출력 {{ trace.remote.output_tokens ?? '미보고' }} 토큰</p><p class="trace-message" role="status">{{ trace?.message || (detail.tracing_enabled ? '원격에 기록된 실행을 확인한 뒤 링크를 제공합니다.' : '이 실행은 LangSmith 추적이 꺼진 상태에서 시작됐습니다.') }}</p></template></section></div>
    <section class="card agent-roster"><h2>역할별 에이전트 구성</h2><div class="roster-grid"><article v-for="agent in overview?.agents || []" :key="agent.id"><strong>{{ agent.name }}</strong><code v-for="tool in agent.tools" :key="tool">{{ tool }}</code></article></div><p class="section-description">AI 모델 모드에서 역할별 도구 사용과 검수 후 재작업을 수행합니다. 검수 통과는 설비 운전 승인을 의미하지 않습니다.</p></section>
  </section>
</template>
