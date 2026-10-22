<script setup>
import { ref, watch } from 'vue'
import { request } from './api.js'

const props = defineProps({ equipmentId: { type: String, required: true } })
const emit = defineEmits(['saved'])
const metrics = [
  ['vibration_mm_s', '진동', 'mm/s'], ['bearing_temperature_c', '베어링 온도', '°C'],
  ['suction_pressure_bar', '흡입 압력', 'bar'], ['discharge_pressure_bar', '토출 압력', 'bar'],
  ['flow_m3_h', '유량', 'm³/h'], ['motor_current_a', '모터 전류', 'A'],
]
const metric = ref('vibration_mm_s'), csv = ref(''), count = ref(0), error = ref(''), message = ref(''), busy = ref(false)
let version = 0
async function refresh() {
  const current = ++version
  count.value = 0; error.value = ''; message.value = ''
  if (!props.equipmentId) return
  try {
    const data = await request(`/api/v1/lifecycle/readings?asset_id=${encodeURIComponent(props.equipmentId)}&metric=${metric.value}`)
    if (current === version) count.value = data.items.length
  } catch (err) { if (current === version) error.value = err.message }
}
async function onFile(event) {
  const file = event.target.files?.[0]
  if (file) csv.value = await file.text()
}
function parseCsv(raw) {
  const lines = raw.replace(/^\uFEFF/, '').trim().split(/\r?\n/).map(s => s.trim()).filter(Boolean)
  if (lines.shift()?.toLowerCase().replace(/\s/g, '') !== 'observed_at,value') throw new Error('첫 줄은 observed_at,value 형식이어야 합니다.')
  if (!lines.length || lines.length > 2000) throw new Error('측정 기록은 1~2000행이어야 합니다.')
  return lines.map((line, index) => {
    const fields = line.split(',').map(s => s.trim())
    if (fields.length !== 2 || !/^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(fields[0]) || !/(Z|[+-]\d\d:\d\d)$/.test(fields[0])) throw new Error(`${index + 2}행의 시각을 시간대가 포함된 ISO 형식으로 입력하세요.`)
    const value = Number(fields[1])
    if (!fields[1] || !Number.isFinite(value) || Number.isNaN(Date.parse(fields[0]))) throw new Error(`${index + 2}행의 수치를 확인하세요.`)
    return { observed_at: fields[0], value }
  })
}
async function save() {
  error.value = ''; message.value = ''
  let readings
  try { readings = parseCsv(csv.value) } catch (err) { error.value = err.message; return }
  busy.value = true
  try {
    await request('/api/v1/lifecycle/readings', { method: 'POST', body: { asset_id: props.equipmentId, metric: metric.value, readings } })
    count.value = readings.length
    message.value = `${readings.length}건을 저장했습니다. 이 설비·항목의 기존 기록은 교체됐습니다.`
    emit('saved')
  } catch (err) { error.value = err.message } finally { busy.value = false }
}
watch(() => [props.equipmentId, metric.value], refresh, { immediate: true })
</script>

<template>
  <details class="card history-import">
    <summary>날짜별 실측 이력 등록 <span>정비 계획 상담의 추가 근거 · {{ count }}건 저장됨</span></summary>
    <p class="section-description">현재 선택한 {{ equipmentId || '설비' }}의 계측 기록을 추가합니다. 분석 결과의 예제 운전 이력은 여기에 포함되지 않습니다.</p>
    <div class="history-fields"><label>측정 항목<select v-model="metric"><option v-for="item in metrics" :key="item[0]" :value="item[0]">{{ item[1] }} ({{ item[2] }})</option></select></label><label>CSV 파일<input type="file" accept=".csv,text/csv" @change="onFile"></label></div>
    <label class="history-label">또는 CSV 내용 붙여넣기<textarea v-model="csv" rows="4" placeholder="observed_at,value&#10;2026-10-01T09:00:00+09:00,2.1"></textarea></label>
    <button type="button" class="secondary-button" :disabled="busy || !equipmentId" @click="save">{{ busy ? '저장 중…' : '실측 이력 저장' }}</button>
    <p class="history-help">시각에 시간대를 포함해 주세요. 등록한 이력의 변화는 챗봇이 정비 계획을 설명할 때 참고하며, 설비 수명이나 교체일을 자동 확정하지 않습니다.</p>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p><p v-if="message" class="notice" role="status">{{ message }}</p>
  </details>
</template>

<style scoped>
.history-import{margin-top:22px}.history-import summary{cursor:pointer;color:#4f6d48;font-size:13px;font-weight:600}.history-import summary span{margin-left:12px;color:#94a589;font-size:10px;font-weight:400}.history-import .section-description{margin-top:17px}.history-fields{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:18px 0}.history-fields label,.history-label{display:grid;gap:8px;color:#6b805e;font-size:11px}.history-fields select,.history-fields input,.history-label textarea{width:100%;border:1px solid #dce5d5;border-radius:5px;background:#fcfdfb;color:#526c45;padding:10px;font:inherit}.history-label textarea{resize:vertical;font-size:11px;line-height:1.6}.history-import button{margin-top:14px}.history-help{color:#8c9d81;font-size:10px;line-height:1.8;margin-top:15px}@media(max-width:680px){.history-fields{grid-template-columns:1fr}.history-import summary span{display:block;margin:6px 0 0}}
</style>
