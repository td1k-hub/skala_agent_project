<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { askChat, request } from './api.js'
import { displayChatText } from './chatText.js'

const props = defineProps({ equipmentId: { type: String, required: true }, analysisRunId: { type: String, default: null }, researchCycleId: { type: Number, default: null } })
const emit = defineEmits(['busy'])
const sessions = ref([]), session = ref(null), selectedId = ref(''), question = ref(''), pending = ref('')
const loading = ref(false), sending = ref(false), error = ref(''), status = ref(''), streamStage = ref(''), messagesEnd = ref(null)
let controller, loadController, version = 0
const measureLabels = { vibration_mm_s: ['진동', 'mm/s'], bearing_temperature_c: ['베어링 온도', '°C'], suction_pressure_bar: ['흡입 압력', 'bar'], discharge_pressure_bar: ['토출 압력', 'bar'], flow_m3_h: ['유량', 'm³/h'], motor_current_a: ['모터 전류', 'A'] }
const checkLabels = { visual: '외관', vibration: '진동', temperature: '온도', leak: '누유', alignment: '정렬' }
const outcomes = { pass: '통과', follow_up: '추가 점검', repair_required: '정비 필요' }
const analysis = computed(() => session.value?.analysis)
const messages = computed(() => session.value?.messages || [])
const suggestions = computed(() => analysis.value ? ['이번 측정값이 이전 분석 기록과 비교해 어떻게 변했어?', '이 추세라면 정비는 언제 검토해야 해?', '현재 결과와 점검 기록을 바탕으로 정비 계획을 세워줘'] : ['저장된 분석 기록에서 진동과 온도는 어떻게 변했어?', '최근 점검 기록을 보고 현재 상태를 추측해줘', '정비 시기를 판단하려면 어떤 정보가 더 필요해?'])
const sourceTitle = source => source?.title?.replace(' · 합성 매뉴얼', '').replace('최근 합성 운전 이력', '최근 운전 이력')
const storageKey = () => `pump-guard-chat:${props.equipmentId}`
const date = value => new Date(value).toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
const readable = err => err.name === 'TypeError' ? '상담 서버에 연결하지 못했습니다. 백엔드 실행을 확인해 주세요.' : err.message
const labelStatus = computed(() => analysis.value ? analysis.value.result.status || '판정 보류' : '분석 미연결')
function remember(id) { try { sessionStorage.setItem(storageKey(), id) } catch {} }
function remembered() { try { return sessionStorage.getItem(storageKey()) } catch { return null } }
async function scrollBottom() { await nextTick(); messagesEnd.value?.scrollIntoView({ block: 'nearest', behavior: 'auto' }) }
async function listSessions(signal) {
  const data = await request(`/api/v1/chat/sessions?equipment_id=${encodeURIComponent(props.equipmentId)}`, { signal })
  sessions.value = data.items
}
async function initialize() {
  const current = ++version
  loadController?.abort(); loadController = new AbortController()
  const signal = loadController.signal
  loading.value = true; error.value = ''; session.value = null
  if (!props.equipmentId) { loading.value = false; return }
  try {
    await listSessions(signal)
    const existing = props.researchCycleId
      ? sessions.value.find(s => s.research_cycle_id === props.researchCycleId)
      : props.analysisRunId
      ? sessions.value.find(s => s.analysis_run_id === props.analysisRunId)
      : sessions.value.find(s => s.id === remembered())
    let id = existing?.id
    if (!id) {
      const created = await request('/api/v1/chat/sessions', { method: 'POST', body: { equipment_id: props.equipmentId, analysis_run_id: props.analysisRunId, research_cycle_id: props.researchCycleId }, signal })
      id = created.id
      await listSessions(signal)
    }
    const data = await request(`/api/v1/chat/sessions/${id}`, { signal })
    if (current !== version || signal.aborted) return
    session.value = data; selectedId.value = id; remember(id)
    await scrollBottom()
  } catch (err) { if (!signal.aborted) error.value = readable(err) }
  finally { if (current === version && !signal.aborted) loading.value = false }
}
async function selectSession(id) {
  if (!id || sending.value) return
  const current = ++version
  loadController?.abort(); loadController = new AbortController()
  const signal = loadController.signal
  loading.value = true; error.value = ''; status.value = ''; session.value = null
  try {
    const data = await request(`/api/v1/chat/sessions/${id}`, { signal })
    if (current === version && !signal.aborted) { session.value = data; selectedId.value = id; remember(id); await scrollBottom() }
  } catch (err) { if (!signal.aborted) error.value = readable(err) }
  finally { if (current === version && !signal.aborted) loading.value = false }
}
async function newSession(analysisRunId = session.value?.analysis_run_id || null) {
  if (sending.value || loading.value || !props.equipmentId) return
  error.value = ''; status.value = ''; loading.value = true
  try {
    const created = await request('/api/v1/chat/sessions', { method: 'POST', body: { equipment_id: props.equipmentId, analysis_run_id: analysisRunId, research_cycle_id: session.value?.research_cycle_id || props.researchCycleId } })
    await listSessions()
    await selectSession(created.id)
    question.value = ''
  } catch (err) { error.value = readable(err) }
  finally { loading.value = false }
}
async function deleteSession() {
  if (!session.value || sending.value || !window.confirm('이 상담의 저장된 대화를 삭제할까요?')) return
  loading.value = true; error.value = ''
  try {
    await request(`/api/v1/chat/sessions/${session.value.id}`, { method: 'DELETE' })
    session.value = null; selectedId.value = ''
    await listSessions()
    if (sessions.value.length) await selectSession(sessions.value[0].id)
  } catch (err) { error.value = readable(err) }
  finally { loading.value = false }
}
async function send(text = question.value) {
  text = text.trim()
  if (!text || text.length > 2000 || !session.value || sending.value || loading.value) return
  sending.value = true; emit('busy', true); error.value = ''; status.value = ''; pending.value = text; question.value = ''
  streamStage.value = '상담 요청을 보내고 있습니다'
  controller = new AbortController()
  await scrollBottom()
  let committed = null
  try {
    committed = await askChat(session.value.id, { message: text }, {
      signal: controller.signal,
      onEvent(event, data) { if (event === 'stage' && data.state === 'started') streamStage.value = data.message },
    })
    const data = await request(`/api/v1/chat/sessions/${session.value.id}`, { signal: controller.signal })
    session.value = data; pending.value = ''; await listSessions(controller.signal)
  } catch (err) {
    if (committed) {
      if (!session.value.messages.some(m => m.id === committed.message_id)) session.value.messages.push({ id: `user:${committed.message_id}`, role: 'user', content: text, created_at: new Date().toISOString() }, { id: committed.message_id, role: 'assistant', content: committed.answer, response: committed, created_at: new Date().toISOString() })
      pending.value = ''; status.value = '답변은 저장됐습니다. 대화 목록 갱신은 다시 시도해 주세요.'
    } else {
      question.value = text; pending.value = ''
      if (err.name === 'AbortError') status.value = '요청을 취소했습니다. 서버에서 이미 완료한 답변은 대화 기록에 남아 있을 수 있습니다.'
      else error.value = readable(err)
    }
  } finally {
    sending.value = false; emit('busy', false); streamStage.value = ''; await scrollBottom()
  }
}
function onKeydown(event) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send() }
}
function sourceLines(source) {
  try {
    const data = JSON.parse(source.content)
    if (source.kind === 'manual') return [...data.symptoms.map(s => `증상: ${s}`), ...data.guide.map(s => `점검: ${s}`)]
    if (source.kind === 'analysis') return [`저장된 판정: ${data.status || '판정 보류'}`, ...(data.findings || []).map(f => `${measureLabels[f.metric]?.[0] || f.metric}: ${f.value} · 단계 ${f.level}`), ...(data.maintenance_plan || []).map(p => p.instruction)]
    if (source.kind === 'equipment') return [`모델: ${data.model}`, `용도: ${data.service}`, ...(data.rated_flow_m3_h == null ? [] : [`정격 유량: ${data.rated_flow_m3_h} m³/h`])]
    if (source.kind === 'measured_history') return [`실험 주기 ${data.selected_cycle_id}까지의 측정 기록 · 실험 순서 기준`, ...data.items.map(item => `주기 ${item.cycle_id} · VS1 ${item.means.VS1.toFixed(3)} mm/s · TS1 ${item.means.TS1.toFixed(3)} °C · FS1 ${item.means.FS1.toFixed(3)} L/min · 펌프 라벨 ${['누설 없음', '약한 내부 누설', '심한 내부 누설'][item.labels.pump_leakage]}`), `출처: ${data.source_url}`]
    if (source.kind === 'analysis_history') return data.items.length ? data.items.map(item => `${date(item.created_at)} · ${item.status || '판정 보류'} · 진동 ${item.measurements.vibration_mm_s ?? '미입력'} mm/s · 온도 ${item.measurements.bearing_temperature_c ?? '미입력'} °C`) : ['저장된 분석 기록이 없습니다.']
    if (source.kind === 'measurement_history') return data.items.length ? data.items.map(item => `${item.label}: ${item.first_value} → ${item.last_value} ${item.unit} · ${item.count}건 · ${item.observed_days}일`) : ['등록된 날짜별 실측 이력이 없습니다.']
    if (source.kind === 'maintenance_history') return data.items.length ? data.items.map(item => `${date(item.created_at)} · ${item.checks.map(c => checkLabels[c] || c).join(', ')} · ${outcomes[item.outcome] || item.outcome}${item.research_cycle_id ? ` · 실험 주기 ${item.research_cycle_id}` : ''}${item.note ? ` · ${item.note}` : ''}`) : ['저장된 점검 기록이 없습니다. 무고장 이력을 의미하지 않습니다.']
    if (source.kind === 'operating_history') return data.items.map(item => `${date(item.timestamp)} · 진동 ${item.vibration_mm_s.toFixed(2)} mm/s · 온도 ${item.bearing_temperature_c} °C · 유량 ${item.flow_m3_h} m³/h`)
  } catch {}
  return [source.content]
}
watch(() => [props.equipmentId, props.analysisRunId, props.researchCycleId], initialize, { immediate: true })
onBeforeUnmount(() => { ++version; controller?.abort(); loadController?.abort(); emit('busy', false) })
</script>

<template>
  <div class="chat-layout">
    <section class="card chat-card">
      <div class="chat-toolbar"><div class="chat-title"><span class="chat-avatar" aria-hidden="true">P</span><div><h2>설비 AI 상담</h2><small>{{ props.equipmentId || '설비를 선택해 주세요' }} · 근거를 확인하는 정비 도우미</small></div></div><button class="text-button" :disabled="loading || sending || !props.equipmentId" @click="newSession()">새 상담 ＋</button></div>
      <div class="chat-session-bar"><label for="chat-history">대화 기록</label><select id="chat-history" :value="selectedId" :disabled="loading || sending" @change="selectSession($event.target.value)"><option v-if="!sessions.length" value="">저장된 상담이 없습니다</option><option v-for="item in sessions" :key="item.id" :value="item.id">{{ date(item.updated_at) }} · {{ item.research_cycle_id ? `실험 주기 ${item.research_cycle_id}` : item.analysis_run_id ? '분석 연결' : '일반 설비 상담' }} · {{ item.id.slice(0, 6) }}</option></select><button class="text-button delete-chat" @click="deleteSession" :disabled="loading || sending || !session">삭제</button></div>
      <div class="chat-messages" role="log" aria-label="상담 대화" :aria-busy="sending">
        <div v-if="loading && !session" class="chat-loading">상담 대화를 준비하고 있습니다…</div>
        <div v-else-if="!messages.length && !pending" class="chat-welcome"><span class="chat-welcome-icon" aria-hidden="true">✳</span><h3>설비에 대해 궁금한 점을 물어보세요.</h3><p>저장된 분석·측정 기록과 점검 이력을 확인하고<br>답변에 사용한 근거를 함께 보여드립니다.</p><div class="suggestion-list"><button v-for="text in suggestions" :key="text" :disabled="!session || sending" @click="send(text)">{{ text }} <span>↗</span></button></div></div>
        <article v-for="message in messages" :key="message.id" :class="['chat-message', message.role]">
          <div class="message-byline">{{ message.role === 'user' ? '나' : 'PUMP GUARD' }} <span>{{ date(message.created_at) }}</span><span v-if="message.response?.response_kind === 'source_summary'" class="reference-tag">저장 자료 요약</span></div>
          <div v-if="message.response?.sections?.length" class="message-body"><p v-for="(section, index) in message.response.sections" :key="index">{{ displayChatText(section.text) }}<span class="citation-row"><span v-for="id in section.source_ids" :key="id" class="citation">{{ sourceTitle(message.response.sources.find(s => s.id === id)) || '참고 자료' }}</span></span></p></div><div v-else class="message-body"><p>{{ message.role === 'assistant' ? displayChatText(message.content) : message.content }}</p></div>
          <details v-if="message.response?.sources?.length" class="chat-sources"><summary>답변 근거 {{ message.response.sources.length }}건 확인</summary><section v-for="source in message.response.sources" :key="source.id"><strong>{{ sourceTitle(source) }}</strong><small>{{ source.kind === 'manual' ? source.id : '선택 설비의 서버 저장·조회 데이터' }}</small><ul><li v-for="(line, i) in sourceLines(source)" :key="i">{{ line }}</li></ul></section></details>
          <div v-if="message.response?.follow_up_questions?.length" class="chat-followups"><button v-for="followup in message.response.follow_up_questions" :key="followup" :disabled="sending || loading" @click="send(followup)">{{ followup }} ↗</button></div>
        </article>
        <article v-if="pending" class="chat-message user"><div class="message-byline">나</div><div class="message-body"><p>{{ pending }}</p></div></article>
        <div v-if="sending" class="chat-thinking" role="status"><span class="thinking-dots" aria-hidden="true">•••</span>{{ streamStage }}</div>
        <div ref="messagesEnd"></div>
      </div>
      <div v-if="error" class="alert error chat-error" role="alert">{{ error }}<button v-if="!session" @click="initialize" :disabled="loading">다시 연결</button></div>
      <p v-if="status" class="chat-notice" role="status">{{ status }}</p>
      <form class="chat-composer" @submit.prevent="send()"><label class="sr-only" for="chat-question">설비 관련 질문</label><textarea id="chat-question" v-model="question" rows="3" maxlength="2000" :disabled="sending || loading || !session" placeholder="예: 이번 분석에서 왜 주의로 나왔어?" @keydown="onKeydown"></textarea><div class="composer-bottom"><small>Enter 전송 · Shift+Enter 줄바꿈 <span>{{ question.length }} / 2000</span></small><button v-if="sending" type="button" class="chat-send stop" @click="controller?.abort()">생성 취소 ×</button><button v-else type="submit" class="chat-send" :disabled="loading || !session || !question.trim()">질문 보내기 ↑</button></div></form>
    </section>
    <aside class="chat-context"><section class="card"><div class="section-heading"><div><h2>상담에 연결된 정보</h2></div><span class="reference-tag">{{ props.equipmentId || '—' }}</span></div><div class="chat-context-status"><small>{{ session?.research_cycle_id ? '실측 자료 연결' : '진동·온도 기준 판정' }}</small><strong>{{ session?.research_cycle_id ? `실험 주기 ${session.research_cycle_id}` : labelStatus }}</strong><span v-if="analysis">{{ date(analysis.result.completed_at) }} 기준</span><span v-else>{{ session?.research_cycle_id ? 'UCI 유압 실험장치 · 과거 측정 기록' : '설비·점검 이력은 바로 질문할 수 있습니다.' }}</span></div><template v-if="analysis"><dl class="chat-measurements"><div v-for="(value, key) in analysis.measurements" :key="key"><dt>{{ measureLabels[key]?.[0] || key }}</dt><dd>{{ value ?? '미입력' }} <small v-if="value !== null">{{ measureLabels[key]?.[1] }}</small></dd></div></dl><p class="chat-context-note">이 대화는 저장된 분석을 설명합니다. 현재 입력 폼을 바꾸어도 연결된 결과는 바뀌지 않습니다.</p></template><p v-else-if="session?.research_cycle_id" class="chat-context-note">선택 주기까지 최대 5개 실험의 측정값과 연결된 최근 점검 기록을 함께 조회합니다. 점검 기록은 공개 데이터 화면에서 입력할 수 있습니다.</p><p v-else class="chat-context-note">수치 분석을 마친 뒤 <strong>‘이 결과에 질문하기’</strong>를 누르면 판정과 고장 후보를 연결해 설명할 수 있습니다.</p></section><section class="card chat-help"><h2>이렇게 질문해 보세요</h2><p>“진동 수치가 이전보다 얼마나 올랐어?”<br>“이 추세면 정비는 언제 검토해야 해?”<br>“현재 결과로 정비 계획을 세워줘”</p><small>상담에서 입력한 숫자로 측정값을 자동 변경하거나 점검 결과를 저장하지 않습니다.</small></section><p class="chat-storage-note">완료된 질문·답변은 이 컴퓨터의 서버에 저장됩니다. 대화 기록에서 다시 열거나 삭제할 수 있습니다.</p></aside>
  </div>
</template>
