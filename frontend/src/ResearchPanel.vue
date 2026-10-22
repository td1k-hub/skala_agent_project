<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { request } from './api.js'

const emit = defineEmits(['consult'])
const inspectionChecks = ref(['visual'])
const inspectionNote = ref(''), inspectionOutcome = ref('follow_up'), savingInspection = ref(false), inspectionMessage = ref(''), inspectionError = ref('')
const metadata = ref(null), cycle = ref(null), cycleId = ref(1), sensorId = ref('VS1')
const error = ref(''), loading = ref(false), playing = ref(false), second = ref(0)
let controller, timer
const sensor = computed(() => metadata.value?.sensors.find(s => s.id === sensorId.value))
const values = computed(() => cycle.value?.traces[sensorId.value] || [])
const bounds = computed(() => {
  if (!values.value.length) return [0, 1]
  const min = Math.min(...values.value), max = Math.max(...values.value)
  const pad = Math.max((max - min) * .1, Math.abs(max) * .001, .001)
  return [min - pad, max + pad]
})
const points = computed(() => values.value.map((v, i) => `${20 + i * 760 / 59},${170 - (v - bounds.value[0]) * 150 / (bounds.value[1] - bounds.value[0])}`).join(' '))
const display = value => Number.isFinite(value) ? value.toLocaleString('ko-KR', { maximumFractionDigits: 3 }) : '—'
const leakage = ['누설 없음', '약한 내부 누설', '심한 내부 누설']

function stop() { playing.value = false; clearInterval(timer) }
function togglePlayback() {
  if (playing.value) return stop()
  if (second.value >= 59) second.value = 0
  playing.value = true
  timer = setInterval(() => {
    if (second.value >= 59) stop()
    else second.value++
  }, 1000)
}
async function saveInspection() {
  if (!cycle.value || savingInspection.value || !inspectionChecks.value.length) return
  savingInspection.value = true; inspectionMessage.value = ''; inspectionError.value = ''
  try {
    await request('/api/v1/inspections', { method: 'POST', body: { equipment_id: 'UCI-447', research_cycle_id: cycle.value.cycle_id, checks: inspectionChecks.value, outcome: inspectionOutcome.value, note: inspectionNote.value } })
    inspectionMessage.value = '점검 기록을 저장했습니다. 상담에서 측정값과 함께 조회할 수 있습니다.'
    inspectionNote.value = ''
  } catch (err) { inspectionError.value = err.message }
  finally { savingInspection.value = false }
}
async function loadCycle() {
  controller?.abort(); stop(); second.value = 0; inspectionMessage.value = ''; inspectionError.value = ''
  const id = Number(cycleId.value)
  if (!Number.isInteger(id) || id < 1 || id > (metadata.value?.cycles || 2205)) {
    error.value = '1~2205 범위의 실험 주기를 입력해 주세요.'; cycle.value = null; loading.value = false; return
  }
  const current = new AbortController(); controller = current
  loading.value = true; error.value = ''; cycle.value = null
  try { cycle.value = await request(`/api/v1/research/uci447/cycles/${id}`, { signal: current.signal }) }
  catch (err) { if (!current.signal.aborted) error.value = err.message }
  finally { if (controller === current) loading.value = false }
}
async function initialize() {
  const current = new AbortController(); controller = current
  try {
    metadata.value = await request('/api/v1/research/uci447', { signal: current.signal })
    if (!current.signal.aborted) await loadCycle()
  } catch (err) { if (!current.signal.aborted) error.value = err.message }
}
onMounted(initialize)
watch(cycleId, loadCycle)
onBeforeUnmount(() => { controller?.abort(); stop() })
</script>

<template>
  <div class="research-panel">
    <p v-if="error" class="alert error" role="alert">{{ error }} <button @click="metadata ? loadCycle() : initialize()">다시 조회</button></p>
    <section v-if="metadata" class="card research-source">
      <div class="section-heading"><div><h2>UCI 유압 시스템 실험 데이터</h2><span class="reference-tag">공개 실측 자료</span></div><a :href="metadata.source_url" target="_blank" rel="noopener noreferrer" class="text-button">원본 출처 ↗</a></div>
      <p class="section-description">{{ metadata.cycles.toLocaleString() }}개 실험 주기 · 주기당 60초 · {{ metadata.sensor_count }}개 센서 · {{ metadata.license }}</p>
      <p class="notice">{{ metadata.limitation }}</p>
      <small>{{ metadata.authors }} · DOI {{ metadata.doi }}</small>
    </section>
    <section v-if="metadata" class="card">
      <div class="research-controls">
        <label>실험 주기 <input v-model.lazy="cycleId" type="number" min="1" :max="metadata.cycles" :disabled="savingInspection"></label>
        <button class="text-button" :disabled="Number(cycleId) <= 1 || loading || savingInspection" @click="cycleId = Number(cycleId) - 1">← 이전</button>
        <button class="text-button" :disabled="Number(cycleId) >= metadata.cycles || loading || savingInspection" @click="cycleId = Number(cycleId) + 1">다음 →</button>
        <label>표시 센서 <select v-model="sensorId"><option v-for="item in metadata.sensors" :key="item.id" :value="item.id">{{ item.id }} · {{ item.label }} ({{ item.unit }})</option></select></label>
      </div>
      <p v-if="loading" class="inline-empty">측정값을 불러오는 중입니다.</p>
      <template v-if="cycle">
        <div class="research-labels"><span>원본 라벨 · 모델 예측 아님</span><strong>펌프: {{ leakage[cycle.labels.pump_leakage] }}</strong><span>냉각기 {{ cycle.labels.cooler_percent }}%</span><span>밸브 {{ cycle.labels.valve_percent }}%</span><span>축압기 {{ cycle.labels.accumulator_bar }} bar</span><span>{{ cycle.labels.stable_flag === 0 ? '안정 상태 도달' : '안정 상태 미도달 가능' }}</span></div>
        <div class="section-heading"><h3>{{ sensor?.label }} · {{ sensorId }}</h3><strong>{{ display(values[second]) }} <small>{{ sensor?.unit }}</small></strong></div>
        <svg class="research-chart" viewBox="0 0 800 200" role="img" :aria-label="`${sensorId} 실험 주기 내 60초 측정 추세, 1초 평균`">
          <line x1="20" y1="170" x2="780" y2="170" stroke="#d8e3dc" />
          <polyline :points="points" fill="none" stroke="#377c64" stroke-width="2.5" />
          <line :x1="20 + second * 760 / 59" y1="20" :x2="20 + second * 760 / 59" y2="170" stroke="#9cae73" stroke-dasharray="4 4" />
          <text x="20" y="193">0초</text><text x="750" y="193">59초</text>
        </svg>
        <p class="section-description">세로 범위 {{ display(bounds[0]) }}~{{ display(bounds[1]) }} {{ sensor?.unit }} · 그래프와 재생값은 1초 구간 평균입니다.</p>
        <div class="research-controls"><button class="chat-send" @click="togglePlayback">{{ playing ? '일시정지' : '실험 기록 재생' }}</button><label class="research-slider"><span>{{ second }}초</span><input v-model.number="second" type="range" min="0" max="59" aria-label="실험 기록 재생 위치"></label></div>
        <div class="table-wrap"><table><thead><tr><th>센서</th><th>60초 평균</th><th>최솟값</th><th>최댓값</th><th>원본 수집 주파수</th></tr></thead><tbody><tr v-for="item in metadata.sensors" :key="item.id"><td>{{ item.id }} · {{ item.label }}</td><td>{{ display(cycle.summary[item.id].mean) }} {{ item.unit }}</td><td>{{ display(cycle.summary[item.id].min) }}</td><td>{{ display(cycle.summary[item.id].max) }}</td><td>{{ item.sampling_hz }} Hz</td></tr></tbody></table></div>
      </template>
    </section>
    <section v-if="cycle" class="card">
      <div class="section-heading"><h2>이 실험 기록에 대해 상담하기</h2><button class="chat-send" :disabled="savingInspection" @click="emit('consult', cycle.cycle_id)">실측 데이터로 AI 상담 ↗</button></div>
      <p class="section-description">주기 {{ cycle.cycle_id }}의 실측 자료를 상담에 연결합니다. 아래에 직접 확인한 내용과 점검 결과를 기록하면 함께 검토합니다.</p>
      <form @submit.prevent="saveInspection"><fieldset :disabled="savingInspection || loading">
        <label class="research-note">점검 메모<textarea v-model="inspectionNote" maxlength="1000" rows="3" placeholder="확인한 항목, 관찰 내용, 추가 확인이 필요한 내용을 적어주세요."></textarea></label>
        <div class="research-controls"><label>점검 결과<select v-model="inspectionOutcome"><option value="pass">점검 통과</option><option value="follow_up">추가 점검</option><option value="repair_required">정비 필요</option></select></label><label v-for="(label, key) in { visual: '외관', vibration: '진동', temperature: '온도', leak: '누유', alignment: '정렬' }" :key="key"><input v-model="inspectionChecks" type="checkbox" :value="key">{{ label }}</label><button type="submit" class="text-button" :disabled="!inspectionChecks.length">{{ savingInspection ? '저장 중…' : '점검 기록 저장' }}</button></div>
      </fieldset></form>
      <p v-if="inspectionMessage" class="notice" role="status">{{ inspectionMessage }}</p><p v-if="inspectionError" class="alert error" role="alert">{{ inspectionError }}</p>
      <small>입력한 점검 기록은 UCI-447의 선택 주기에만 연결되며 원본 실험 라벨과 구분됩니다.</small>
    </section>
    <p v-if="metadata" class="research-footnote">TS는 온도, EPS1은 모터 전력입니다. 베어링 온도나 모터 전류로 변환하지 않았습니다. PS 센서의 흡입·토출 위치도 임의로 지정하지 않았습니다. 각 주기는 별도 실험이며 실제 관측 시각은 제공되지 않습니다. 실측 자료와 입력한 점검 기록을 AI 상담에서 함께 검토할 수 있습니다. 별도 이상 탐지 모델의 학습과 성능 평가는 아직 수행하지 않았습니다.</p>
  </div>
</template>

<style scoped>
.research-note{display:grid;gap:9px;font-size:12px;margin:15px 0}.research-note textarea{width:100%;font:inherit;padding:12px;border:1px solid #d8e3dc;border-radius:5px;resize:vertical}.research-panel{display:grid;gap:22px}.research-source>small{color:#7c8e80;display:block;margin-top:14px;font-size:10px}.research-controls{display:flex;align-items:center;gap:18px;flex-wrap:wrap;margin-bottom:22px}.research-controls label{display:flex;align-items:center;gap:10px;font-size:12px;color:#60796a}.research-controls input[type=number]{width:90px}.research-controls input,.research-controls select{border:1px solid #d8e3dc;border-radius:5px;padding:7px;background:#fafcf9;color:#355a46}.research-labels{display:flex;align-items:center;flex-wrap:wrap;gap:12px;padding:14px;background:#edf3e8;margin-bottom:25px;border-radius:6px;font-size:11px;color:#59734f}.research-labels>span:first-child{width:100%;font-size:10px;color:#819176}.research-chart{width:100%;height:auto;margin-top:15px}.research-chart text{fill:#87968b;font-size:11px}.research-slider{flex:1;min-width:150px}.research-slider input{width:100%;padding:0}.research-slider span{white-space:nowrap}.research-footnote{font-size:11px;line-height:1.9;color:#7c8e80}.research-source .notice{line-height:1.8}
</style>
