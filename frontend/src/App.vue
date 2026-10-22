<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { analyze, request } from './api.js'
import AdminPanel from './AdminPanel.vue'
import ChatPanel from './ChatPanel.vue'
import AnalysisTrend from './AnalysisTrend.vue'
import HistoryImport from './HistoryImport.vue'
import { describeEvidence, previewEvidenceIds } from './evidenceText.js'

const metrics = [
  { key: 'vibration_mm_s', label: '진동', unit: 'mm/s', min: 0, max: 100 },
  { key: 'bearing_temperature_c', label: '베어링 온도', unit: '°C', min: -50, max: 300 },
  { key: 'suction_pressure_bar', label: '흡입 압력', unit: 'bar', min: -1, max: 100 },
  { key: 'discharge_pressure_bar', label: '토출 압력', unit: 'bar', min: -1, max: 200 },
  { key: 'flow_m3_h', label: '유량', unit: 'm³/h', min: 0, max: 10000 },
  { key: 'motor_current_a', label: '모터 전류', unit: 'A', min: 0, max: 10000 },
]
const stages = [['input', '설비정보', '설비·이력 도구 조회'], ['condition', '상태진단', '규칙·관측 근거 검토'], ['cause', '고장원인', '매뉴얼 근거 조회'], ['maintenance', '정비계획', '점검 항목 구성'], ['safety', '안전검수', '검수·재작업 요청']]
const values = reactive(Object.fromEntries(metrics.map(m => [m.key, ''])))
const equipment = ref([]), selected = ref(''), health = ref(null), spec = ref(null), analyses = ref([]), inspections = ref([])
const booting = ref(false), detailLoading = ref(false), bootError = ref(''), detailError = ref(''), error = ref(''), notice = ref('')
const running = ref(false), result = ref(null), stageState = reactive({}), activeTab = ref('analysis'), runId = ref('')
const checks = ref(['visual']), outcome = ref('follow_up'), saving = ref(false), saveMessage = ref(''), saveError = ref('')
const chatAnalysisRunId = ref(null), chatBusy = ref(false)
const trendRefreshKey = ref(0)
function openChat(runId = null) { chatAnalysisRunId.value = runId || analyses.value[0]?.run_id || null; activeTab.value = 'chat' }
const inspectionNote = ref('')
const checkNames = { visual: '외관', vibration: '진동', temperature: '온도', leak: '누유', alignment: '정렬' }
const outcomes = { pass: '점검 통과', follow_up: '추가 점검', repair_required: '정비 필요' }
let analysisController, detailController, bootController
let detailVersion = 0
const pageTitle = computed(() => ({ analysis: '설비 상태 분석', history: '점검 기록', chat: 'AI 상담', admin: '관리자 모니터링' })[activeTab.value])
const currentEquipment = computed(() => equipment.value.find(e => e.equipment_id === selected.value))
const completed = computed(() => Object.values(stageState).filter(s => s === 'completed').length)
const resultTone = computed(() => ({ 정상: 'good', 관찰: 'watch', 주의: 'warn', '점검 필요': 'danger' })[result.value?.status] || 'unknown')
const tokens = computed(() => result.value?.metrics?.llm_calls.reduce((sum, c) => sum + (c.input_tokens ?? 0) + (c.output_tokens ?? 0), 0))
const evidenceById = computed(() => new Map((result.value?.evidence || []).map(item => [item.id, item])))
const evidenceFor = id => describeEvidence(evidenceById.value.get(id))
const formatDate = value => new Date(value).toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
const readableError = err => err.name === 'TypeError' ? '백엔드에 연결하지 못했습니다. 8001번 포트의 서버 실행을 확인해 주세요.' : err.message

async function connect() {
  bootController?.abort(); bootController = new AbortController()
  booting.value = true; bootError.value = ''; health.value = null
  const signal = bootController.signal
  const results = await Promise.allSettled([request('/health', { signal }), request('/api/v1/equipment', { signal })])
  if (signal.aborted) return
  if (results[0].status === 'fulfilled') health.value = results[0].value
  if (results[1].status === 'fulfilled') {
    equipment.value = results[1].value.items
    if (!equipment.value.some(e => e.equipment_id === selected.value)) selected.value = equipment.value[0]?.equipment_id || ''
    else await loadDetails(selected.value)
  }
  const failed = results.find(r => r.status === 'rejected')
  if (failed) bootError.value = readableError(failed.reason)
  booting.value = false
}
async function loadDetails(id) {
  const version = ++detailVersion
  detailController?.abort(); detailController = new AbortController()
  spec.value = null; analyses.value = []; inspections.value = []; detailError.value = ''
  result.value = null; error.value = ''; notice.value = ''; saveMessage.value = ''; saveError.value = ''; runId.value = ''
  Object.keys(stageState).forEach(k => delete stageState[k])
  if (!id) return
  detailLoading.value = true
  const signal = detailController.signal
  const all = await Promise.allSettled(['', '/analysis-history?limit=100', '/maintenance-history'].map(suffix => request(`/api/v1/equipment/${id}${suffix}`, { signal })))
  if (version !== detailVersion || signal.aborted) return
  if (all[0].status === 'fulfilled') spec.value = all[0].value
  if (all[1].status === 'fulfilled') analyses.value = all[1].value.items
  if (all[2].status === 'fulfilled') inspections.value = all[2].value.items
  const failed = all.find(r => r.status === 'rejected')
  if (failed) detailError.value = readableError(failed.reason)
  detailLoading.value = false
}
async function startAnalysis() {
  if (running.value || !selected.value) return
  const measurements = {}
  for (const m of metrics) {
    const raw = values[m.key]
    measurements[m.key] = raw === '' || raw == null ? null : Number(raw)
    const n = measurements[m.key]
    if (n !== null && (!Number.isFinite(n) || n < m.min || n > m.max)) { error.value = `${m.label} 입력 범위를 확인해 주세요.`; return }
  }
  error.value = ''; notice.value = ''; result.value = null; runId.value = ''
  Object.keys(stageState).forEach(k => delete stageState[k])
  running.value = true; analysisController = new AbortController()
  try {
    result.value = await analyze({ equipment_id: selected.value, measurements }, {
      signal: analysisController.signal,
      onEvent(event, data) {
        if (event === 'started') runId.value = data.run_id
        if (event === 'stage') stageState[data.stage] = data.state
      },
    })
    try { await refreshHistory() }
    catch { notice.value = '분석은 저장됐습니다. 점검 기록에서 새로고침해 확인해 주세요.' }
  } catch (err) {
    if (err.name === 'AbortError') notice.value = '분석을 취소했습니다.'
    else error.value = readableError(err)
  } finally { running.value = false }
}
async function saveInspection() {
  if (!selected.value || !checks.value.length || saving.value) return
  saving.value = true; saveMessage.value = ''; saveError.value = ''
  const id = selected.value
  try {
    await request('/api/v1/inspections', { method: 'POST', body: { equipment_id: id, checks: checks.value, outcome: outcome.value, note: inspectionNote.value } })
    saveMessage.value = '점검 결과를 저장했습니다.'
    try {
      const data = await request(`/api/v1/equipment/${id}/maintenance-history`)
      if (id === selected.value) inspections.value = data.items
    } catch { saveError.value = '저장은 완료됐지만 이력을 새로 읽지 못했습니다. 이력 새로고침을 눌러 주세요.' }
  } catch (err) { saveError.value = readableError(err) }
  finally { saving.value = false }
}
async function refreshHistory() {
  const id = selected.value
  saveError.value = ''
  try {
    const [inspected, analyzed] = await Promise.all([
      request(`/api/v1/equipment/${id}/maintenance-history`),
      request(`/api/v1/equipment/${id}/analysis-history?limit=100`),
    ])
    if (id === selected.value) { inspections.value = inspected.items; analyses.value = analyzed.items }
  } catch (err) { saveError.value = readableError(err) }
}
watch(selected, id => { chatAnalysisRunId.value = null; loadDetails(id) })
watch(values, () => {
  if (running.value) return
  result.value = null; runId.value = ''; error.value = ''
  Object.keys(stageState).forEach(k => delete stageState[k])
})
onMounted(connect)
onBeforeUnmount(() => { analysisController?.abort(); detailController?.abort(); bootController?.abort() })
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <a class="brand" href="#"><span class="brand-mark">P<span>↗</span></span><div>PUMP GUARD<small>설비 인사이트 워크스페이스</small></div></a>
      <div class="sidebar-label">WORKSPACE</div>
      <nav aria-label="주 메뉴">
        <button :class="{ active: activeTab === 'analysis' }" @click="activeTab = 'analysis'"><span>◈</span>설비 상태 분석<span class="nav-arrow">↗</span></button>
        <button :class="{ active: activeTab === 'history' }" @click="activeTab = 'history'"><span>≡</span>점검 기록</button>
        <button :class="{ active: activeTab === 'chat' }" @click="openChat()"><span>✳</span>AI 상담</button>
        <button :class="{ active: activeTab === 'admin' }" @click="activeTab = 'admin'"><span>▥</span>관리자 모니터링</button>
      </nav>
      <div class="sidebar-bottom"><span class="tiny-dot"></span>PUMP GUARD<p>설비 분석 · 데이터 · AI 상담</p><span class="version">PUMP GUARD / v0.5</span></div>
    </aside>
    <div class="workspace">
      <header class="topbar"><span>워크스페이스 <span class="slash">/</span> {{ pageTitle }}</span><div class="connection"><span class="tiny-dot" :class="health ? 'connected' : 'offline'"></span>{{ health ? '백엔드 연결됨' : '백엔드 연결 대기' }}<button @click="connect" :disabled="booting || running || saving || chatBusy" aria-label="백엔드 연결 새로고침">↻</button></div></header>
      <main>
        <div class="page-heading"><div><div class="eyebrow">EQUIPMENT INTELLIGENCE</div><h1>{{ pageTitle }}</h1><p>{{ activeTab === 'analysis' ? '측정값에서 근거까지, 펌프의 상태를 한눈에 확인하세요.' : activeTab === 'history' ? '점검 결과를 기록하고 설비별 정비 이력을 확인하세요.' : activeTab === 'chat' ? '설비와 분석 결과에 대해 질문하고 답변 근거를 확인하세요.' : '에이전트 실행과 오류, 토큰 사용량을 확인하세요.' }}</p></div><span class="mode-tag">{{ health?.mode === 'openai' ? `OpenAI · ${health.model}` : health?.mode === 'ollama' ? `Ollama · ${health.model}` : health ? '규칙 분석' : '연결 확인 중' }}</span></div>
        <div v-if="health?.mode === 'openai' && !health.provider_configured" class="alert error" role="alert">OpenAI API 키가 설정되지 않았습니다. 백엔드 .env에 OPENAI_API_KEY를 입력하고 서버를 다시 시작해 주세요.</div>
        <div v-if="bootError" class="alert error" role="alert">{{ bootError }} <button @click="connect" :disabled="booting">다시 연결</button></div>
        <section v-if="activeTab !== 'admin'" class="equipment-bar" aria-label="설비 선택"><div class="equipment-icon">↻</div><label for="equipment"><small>분석 대상 설비</small><select id="equipment" v-model="selected" :disabled="booting || running || saving || chatBusy"><option v-if="!equipment.length" value="">{{ booting ? '설비 불러오는 중…' : '설비를 불러오지 못했습니다' }}</option><option v-for="item in equipment" :key="item.equipment_id" :value="item.equipment_id">{{ item.equipment_id }} · {{ item.service }}</option></select></label><div class="equipment-meta"><span>{{ spec?.model || currentEquipment?.model || '—' }}</span><small>원심펌프</small></div><div class="equipment-meta"><span>{{ spec?.rated_flow_m3_h ?? '—' }} <small>m³/h</small></span><small>정격 유량</small></div><span v-if="detailLoading" class="loading-text">이력 조회 중…</span></section>
        <p v-if="detailError" class="alert error" role="alert">{{ detailError }}</p>
        <div v-if="activeTab === 'analysis'" class="analysis-grid">
          <section class="card input-card"><div class="section-heading"><div><span class="section-number">01</span><h2>운전 데이터</h2></div></div><p class="section-description">현재 측정값을 입력하세요. 빈 항목은 누락값으로 처리합니다.</p>
            <form @submit.prevent="startAnalysis"><fieldset :disabled="running"><div class="fields"><label v-for="m in metrics" :key="m.key" :for="m.key"><span>{{ m.label }}</span><div class="input-unit"><input :id="m.key" v-model="values[m.key]" type="number" step="any" :min="m.min" :max="m.max" placeholder="미입력" /><span>{{ m.unit }}</span></div></label></div></fieldset><div class="input-note">진동·온도를 기준으로 상태를 분석합니다.<br>압력·유량·전류의 위험도 판정은 미구현입니다.</div><button v-if="!running" class="primary-button" type="submit" :disabled="!selected || booting || detailLoading">상태 분석 시작 <span>↗</span></button><button v-else class="cancel-button" type="button" @click="analysisController?.abort()">분석 중… <span>취소 ×</span></button></form>
            <p v-if="notice" class="notice" role="status">{{ notice }}</p>
          </section>
          <div class="results-column">
            <section class="card progress-card"><div class="section-heading"><div><span class="section-number">02</span><h2>{{ health?.mode !== 'mock' ? '에이전트 분석 진행' : '분석 진행' }}</h2></div><span class="progress-count">{{ completed }} / 5</span></div><ol class="stage-list"><li v-for="([key, title, description], index) in stages" :key="key" :class="stageState[key]"><span class="stage-dot">{{ stageState[key] === 'completed' ? '✓' : index + 1 }}</span><strong>{{ title }}</strong><small>{{ description }}</small><span v-if="stageState[key] === 'started' && running" class="stage-status">진행 중</span><span v-else-if="stageState[key] === 'started'" class="stage-status">중단</span></li></ol><div class="progress-track"><span :style="{ width: `${completed * 20}%` }"></span></div></section>
            <div v-if="error" class="alert error" role="alert">{{ error }}<small v-if="runId">실행 ID: {{ runId }}</small></div>
            <section class="card result-card" aria-live="polite"><div class="section-heading"><div><span class="section-number">03</span><h2>분석 결과</h2></div><span v-if="result" class="result-time">{{ formatDate(result.completed_at) }}</span></div>
              <div v-if="!result" class="empty-result"><div class="pump-art" aria-hidden="true"><svg viewBox="0 0 220 130" fill="none"><path d="M22 72H58M139 72H199M109 42V18H151" stroke="currentColor" stroke-width="12"/><circle cx="101" cy="73" r="36" stroke="currentColor" stroke-width="12"/><circle cx="101" cy="73" r="15" stroke="currentColor" stroke-width="3"/><path d="M69 113H139M79 104V113M124 104V113" stroke="currentColor" stroke-width="6"/><path d="M90 73H112M101 62V84" stroke="currentColor" stroke-width="3"/></svg></div><h3>{{ running ? '설비 데이터를 분석하고 있습니다' : '설비의 상태를 확인할 준비가 됐습니다' }}</h3><p>{{ running ? '모델 로딩에 시간이 걸릴 수 있습니다. 위에서 진행 단계를 확인하세요.' : '측정값을 입력하고 분석을 시작하면 판정과 근거가 여기에 표시됩니다.' }}</p><span>측정값 입력 → 근거 조회 → 정비 항목 확인</span></div>
              <template v-else><div class="result-summary" :class="resultTone"><div><small>진동·온도 기준 상태</small><h3>{{ result.status ?? '판정 보류' }}</h3></div><div><strong>{{ result.candidate_causes.length }}개</strong><small>고장 원인 후보 · 확정 진단 아님</small></div></div><p v-if="result.missing_fields.length" class="notice">미입력 항목: {{ result.missing_fields.map(key => metrics.find(m => m.key === key)?.label || key).join(', ') }}</p><div class="finding-grid"><div v-for="f in result.findings" :key="f.metric"><small>{{ metrics.find(m => m.key === f.metric)?.label }}</small><strong>{{ f.value }} <small>{{ metrics.find(m => m.key === f.metric)?.unit }}</small></strong><span>{{ ['정상', '관찰', '주의', '점검 필요'][f.level] }}</span></div></div><h3 class="subheading">고장 원인 후보</h3><div v-if="!result.cause_details.length" class="inline-empty">제시할 수 있는 원인 후보가 없습니다. 설비가 안전하다는 의미는 아닙니다.</div><article v-for="cause in result.cause_details" :key="cause.manual_id" class="cause-item"><strong>{{ cause.fault }}</strong><span class="reference-tag">{{ cause.manual_id }}</span><p class="cause-caption">AI가 참고한 관측 기록 · 원인 확정 근거는 아닙니다</p><ul class="cause-evidence-preview"><li v-for="id in previewEvidenceIds(cause)" :key="id">{{ evidenceFor(id).title }}</li></ul><details class="cause-evidence-more"><summary>참고 기록 {{ cause.evidence_ids.length }}건 전체 보기</summary><ul><li v-for="id in cause.evidence_ids" :key="id"><strong>{{ evidenceFor(id).title }}</strong><span>{{ evidenceFor(id).detail }}</span></li></ul></details></article><h3 class="subheading">권장 점검 항목</h3><ol v-if="result.maintenance_plan.length" class="plan-list"><li v-for="(step, index) in result.maintenance_plan" :key="`${step.manual_id}:${step.guide_index}`"><span>{{ String(index + 1).padStart(2, '0') }}</span><div>{{ step.instruction }}<small>{{ step.manual_id }}</small></div></li></ol><p v-else class="inline-empty">구성된 점검 항목이 없습니다.</p><details class="evidence-details"><summary>분석에 사용한 관측 기록 전체 보기 · {{ result.evidence.length }}건</summary><div v-for="item in result.evidence" :key="item.id" class="evidence-record"><strong>{{ describeEvidence(item).title }}</strong><span>{{ describeEvidence(item).detail }}</span></div></details><button type="button" class="chat-result-link" @click="openChat(result.run_id)">이 결과로 정비 계획 묻기 <span>↗</span></button><div class="run-metrics"><span>{{ result.model || '규칙 분석' }}</span><span>{{ (result.metrics.total_ms / 1000).toFixed(1) }}초</span><span>{{ result.metrics.llm_calls.length }}회 호출 · {{ tokens }} 토큰<span v-if="result.metrics.llm_calls.some(c => c.input_tokens === null || c.output_tokens === null)"> (일부 미보고)</span></span></div><details class="run-details"><summary>실행 정보</summary><code>{{ result.run_id }}</code></details></template>
            </section>
          </div>
          <AnalysisTrend :analyses="analyses" :equipment-id="selected" :refresh-key="trendRefreshKey" />
          <HistoryImport :equipment-id="selected" class="analysis-import" style="grid-column: 1 / -1" @saved="trendRefreshKey++" />
        </div>
        <div v-else-if="activeTab === 'history'" class="history-grid"><section class="card"><div class="section-heading"><div><span class="section-number">01</span><h2>점검 결과 기록</h2></div></div><p class="section-description">현장에서 수행한 점검 결과를 기록하세요. 상태 분석 결과와 입력 측정값은 분석 완료 시 자동 저장됩니다.</p><form @submit.prevent="saveInspection"><fieldset :disabled="saving"><legend>수행한 점검 항목</legend><div class="check-options"><label v-for="(label, key) in checkNames" :key="key"><input v-model="checks" type="checkbox" :value="key">{{ label }}</label></div><label class="outcome-label" for="outcome">점검 결과<select id="outcome" v-model="outcome"><option v-for="(label, key) in outcomes" :key="key" :value="key">{{ label }}</option></select></label><label class="outcome-label" for="inspection-note">점검 메모<input id="inspection-note" v-model="inspectionNote" maxlength="1000" placeholder="관찰한 증상과 후속 확인 사항"></label></fieldset><button class="primary-button" :disabled="!selected || saving || !checks.length || running">{{ saving ? '저장 중…' : '점검 결과 저장' }}<span>↗</span></button></form><p v-if="saveMessage" class="notice" role="status">{{ saveMessage }}</p><p v-if="saveError" class="alert error" role="alert">{{ saveError }}</p></section><section class="card"><div class="section-heading"><div><span class="section-number">02</span><h2>저장된 점검 이력</h2></div><button class="text-button" :disabled="!selected || saving" @click="refreshHistory">새로고침 ↻</button></div><p class="section-description">{{ selected || '설비 미선택' }} · 최근 {{ inspections.length }}건 (최대 100건)</p><p v-if="!inspections.length" class="inline-empty">저장된 점검 이력이 없습니다.</p><div class="table-wrap" v-else><table><thead><tr><th>기록 시각</th><th>점검 항목</th><th>결과</th></tr></thead><tbody><tr v-for="item in inspections" :key="item.id"><td>{{ formatDate(item.created_at) }}</td><td>{{ item.checks.map(c => checkNames[c]).join(' · ') }}<small v-if="item.note" class="inspection-note-text">{{ item.note }}</small></td><td><span class="history-outcome">{{ outcomes[item.outcome] }}</span></td></tr></tbody></table></div></section></div>
        <ChatPanel v-else-if="activeTab === 'chat'" :key="`${selected}:${chatAnalysisRunId || 'general'}`" :equipment-id="selected" :analysis-run-id="chatAnalysisRunId" @busy="chatBusy = $event" />
        <AdminPanel v-else />
        <section v-if="activeTab === 'history'" class="card operating-card"><div class="section-heading"><div><h2>저장된 분석·측정 기록</h2></div><small>최근 {{ analyses.length }}건</small></div><p v-if="!analyses.length" class="inline-empty">저장된 분석이 없습니다. 현재 측정값을 분석하면 여기에 기록됩니다.</p><div v-else class="table-wrap"><table><thead><tr><th>분석 시각</th><th>입력 측정값</th><th>진동·온도 판정</th><th>상담</th></tr></thead><tbody><tr v-for="item in analyses" :key="item.run_id"><td>{{ formatDate(item.created_at) }}</td><td><span v-for="m in metrics" :key="m.key" v-show="item.measurements[m.key] !== null">{{ m.label }} {{ item.measurements[m.key] }} {{ m.unit }} · </span></td><td>{{ item.status || '판정 보류' }}</td><td><button class="text-button" @click="openChat(item.run_id)">이 결과에 질문 ↗</button></td></tr></tbody></table></div></section>
        <footer>PUMP GUARD <span>근거를 바탕으로, 신중한 정비 판단을 위해.</span><span>PUMP GUARD · EQUIPMENT INTELLIGENCE</span></footer>
      </main>
    </div>
  </div>
</template>
