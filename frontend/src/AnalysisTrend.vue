<script setup>
import { computed, ref, watch } from 'vue'
import { request } from './api.js'

const props = defineProps({ analyses: { type: Array, default: () => [] }, equipmentId: { type: String, default: '' }, refreshKey: { type: Number, default: 0 } })
const metrics = [
  { key: 'vibration_mm_s', label: '진동', unit: 'mm/s' },
  { key: 'bearing_temperature_c', label: '베어링 온도', unit: '°C' },
  { key: 'suction_pressure_bar', label: '흡입 압력', unit: 'bar' },
  { key: 'discharge_pressure_bar', label: '토출 압력', unit: 'bar' },
  { key: 'flow_m3_h', label: '유량', unit: 'm³/h' },
  { key: 'motor_current_a', label: '모터 전류', unit: 'A' },
]
const selected = ref('vibration_mm_s')
const source = ref('analysis')
const measured = ref([]), loadError = ref(''), loading = ref(false)
const threshold = ref(''), direction = ref('above'), forecastResult = ref(null), forecastError = ref(''), forecasting = ref(false)
let loadVersion = 0
let forecastVersion = 0
async function loadMeasured() {
  const version = ++loadVersion
  measured.value = []; loadError.value = ''
  if (source.value !== 'measured' || !props.equipmentId) { loading.value = false; return }
  loading.value = true
  try {
    const data = await request(`/api/v1/lifecycle/readings?asset_id=${encodeURIComponent(props.equipmentId)}&metric=${encodeURIComponent(selected.value)}`)
    if (version === loadVersion) measured.value = data.items
  } catch (error) { if (version === loadVersion) loadError.value = error.message }
  finally { if (version === loadVersion) loading.value = false }
}
watch(() => [props.equipmentId, selected.value, source.value, props.refreshKey], loadMeasured, { immediate: true })
watch(() => [props.equipmentId, selected.value, source.value, props.refreshKey], () => { ++forecastVersion; forecasting.value = false; forecastResult.value = null; forecastError.value = ''; threshold.value = '' })
watch(() => [threshold.value, direction.value], () => { ++forecastVersion; forecasting.value = false; forecastResult.value = null; forecastError.value = '' })
async function runForecast() {
  const limit = Number(threshold.value)
  const version = ++forecastVersion
  const assetId = props.equipmentId, metricId = selected.value
  forecastError.value = ''; forecastResult.value = null
  if (threshold.value === '' || !Number.isFinite(limit)) { forecastError.value = '설비 담당자가 정한 한계값을 숫자로 입력해 주세요.'; return }
  forecasting.value = true
  try {
    const result = await request('/api/v1/lifecycle/forecast', {
      method: 'POST', body: { asset_id: assetId, metric: metricId, threshold: limit, direction: direction.value },
    })
    if (version === forecastVersion) forecastResult.value = result
  } catch (error) { if (version === forecastVersion) forecastError.value = error.message }
  finally { if (version === forecastVersion) forecasting.value = false }
}
const forecastStates = { insufficient_history: '계측 이력이 부족합니다', threshold_reached: '입력한 한계값에 도달했습니다', stale: '최근 기록을 다시 측정해야 합니다', uncertain_trend: '일관된 추세가 확인되지 않습니다', outside_horizon: '관측 기간 밖의 추정은 표시하지 않습니다', projection_elapsed: '현재값 재측정이 필요합니다', projected: '입력한 한계값 도달 시점의 추정' }
const metric = computed(() => metrics.find(item => item.key === selected.value) || metrics[0])
const items = computed(() => (source.value === 'measured' ? measured.value.map(item => ({
    at: Date.parse(item.observed_at), value: item.value, status: null,
  })) : props.analyses
  .map(item => ({
    at: Date.parse(item.created_at),
    value: item.measurements?.[selected.value],
    status: item.status,
  })))
  .filter(item => Number.isFinite(item.at) && typeof item.value === 'number' && Number.isFinite(item.value))
  .sort((a, b) => a.at - b.at)
  .slice(source.value === 'measured' ? -2000 : -100))
const chart = computed(() => {
  if (!items.value.length) return null
  const values = items.value.map(item => item.value)
  const low = Math.min(...values), high = Math.max(...values)
  const padding = Math.max((high - low) * 0.15, Math.abs(high) * 0.05, 0.1)
  const yMin = low - padding, yMax = high + padding
  const firstTime = items.value[0].at, lastTime = items.value.at(-1).at
  const points = items.value.map((item, index) => ({
    ...item,
    x: lastTime === firstTime ? 42 + index * (676 / Math.max(items.value.length - 1, 1)) : 42 + (item.at - firstTime) / (lastTime - firstTime) * 676,
    y: 180 - (item.value - yMin) / (yMax - yMin) * 140,
  }))
  return { points, line: points.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' '), yMin, yMax }
})
const change = computed(() => items.value.length < 2 ? null : items.value.at(-1).value - items.value[0].value)
const range = computed(() => items.value.length < 2 ? null : Math.max(...items.value.map(item => item.value)) - Math.min(...items.value.map(item => item.value)))
const visibleDots = computed(() => {
  const points = chart.value?.points || []
  return points.length <= 60 ? points : [points[0], points.at(-1)]
})
const date = timestamp => new Date(timestamp).toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
const number = value => Number(value.toFixed(2)).toLocaleString('ko-KR')
</script>

<template>
  <section class="card analysis-trend" aria-label="저장된 분석 수치의 시간별 변화">
    <div class="section-heading"><div><span class="section-number">04</span><h2>이전 수치와 변화</h2></div><small>{{ equipmentId }} · {{ source === 'measured' ? '등록 실측 최대 2,000건' : '분석 입력 최근 최대 100건' }}</small></div>
    <p class="section-description">분석할 때 입력한 수치와 별도로 등록한 날짜별 실측 이력을 구분해 봅니다. 같은 운전 조건에서 측정했는지 확인해 주세요.</p>
    <div class="trend-sources" role="group" aria-label="기록 출처"><button type="button" :class="{ active: source === 'analysis' }" :aria-pressed="source === 'analysis'" @click="source = 'analysis'">분석 입력 기록</button><button type="button" :class="{ active: source === 'measured' }" :aria-pressed="source === 'measured'" @click="source = 'measured'">등록한 날짜별 실측</button></div>
    <div class="trend-controls" role="group" aria-label="그래프 측정 항목">
      <button v-for="item in metrics" :key="item.key" type="button" :class="{ active: selected === item.key }" :aria-pressed="selected === item.key" @click="selected = item.key">{{ item.label }}</button>
    </div>
    <p v-if="loading" class="inline-empty">날짜별 기록을 불러오는 중입니다.</p>
    <p v-else-if="loadError" class="alert error" role="alert">{{ loadError }}</p>
    <p v-else-if="!items.length" class="inline-empty">저장된 {{ metric.label }} 수치가 없습니다. {{ source === 'analysis' ? '분석을 완료하면 기록이 나타납니다.' : '아래에서 날짜별 실측 CSV를 등록해 주세요.' }}</p>
    <template v-else>
      <div class="trend-summary"><strong>최근 {{ metric.label }} {{ number(items.at(-1).value) }} {{ metric.unit }}</strong><span v-if="change !== null">첫 기록 대비 {{ change >= 0 ? '+' : '' }}{{ number(change) }} {{ metric.unit }}</span><span v-if="range !== null">관측 범위 {{ number(range) }} {{ metric.unit }}</span><span>{{ items.length }}건의 입력 기록</span></div>
      <svg class="trend-chart" viewBox="0 0 760 220" role="img" :aria-label="`${metric.label} ${items.length}건의 시간별 변화. 첫 값 ${number(items[0].value)} ${metric.unit}, 최근 값 ${number(items.at(-1).value)} ${metric.unit}`">
        <line x1="42" y1="40" x2="718" y2="40" class="grid-line" />
        <line x1="42" y1="110" x2="718" y2="110" class="grid-line" />
        <line x1="42" y1="180" x2="718" y2="180" class="axis-line" />
        <text x="4" y="45">{{ number(chart.yMax) }}</text><text x="4" y="184">{{ number(chart.yMin) }}</text>
        <polyline v-if="chart.points.length > 1" :points="chart.line" fill="none" class="trend-line" />
        <circle v-for="(point, index) in visibleDots" :key="`${point.at}:${index}`" :cx="point.x" :cy="point.y" :r="index === visibleDots.length - 1 ? 5 : 3.5" class="trend-point"><title>{{ date(point.at) }} · {{ number(point.value) }} {{ metric.unit }}{{ point.status ? ` · ${point.status}` : '' }}</title></circle>
        <text x="42" y="208">{{ date(items[0].at) }}</text><text x="718" y="208" text-anchor="end">{{ date(items.at(-1).at) }}</text>
      </svg>
      <p v-if="items.length < 2" class="trend-note">기록이 한 건이라 변화 방향을 말할 수 없습니다. 다음 측정값과 함께 비교해 주세요.</p>
      <p v-else class="trend-note">선은 저장된 값을 잇습니다. 두 기록 사이의 센서값이나 고장 추세를 추정한 선은 아닙니다. 압력·유량·전류는 현재 위험도 판정에 사용되지 않습니다.</p>
    </template>
    <form v-if="source === 'measured'" class="trend-forecast" @submit.prevent="runForecast">
      <h3>입력한 한계값까지의 추세 확인</h3>
      <p>현장 또는 제조사 기준을 확인한 담당자가 한계값을 입력하세요. 도달 예상일은 고장일·교체일이 아닙니다.</p>
      <div class="trend-forecast-fields"><label>한계값 <span>({{ metric.unit }})</span><input v-model="threshold" type="number" step="any" required placeholder="한계값 입력"></label><label>방향<select v-model="direction"><option value="above">이상으로 증가</option><option value="below">이하로 감소</option></select></label><button type="submit" :disabled="forecasting || loading || !items.length">{{ forecasting ? '계산 중…' : '추세 계산' }}</button></div>
      <p v-if="forecastError" class="alert error" role="alert">{{ forecastError }}</p>
      <div v-if="forecastResult" class="trend-forecast-result" role="status"><strong>{{ forecastStates[forecastResult.state] || '추세 확인' }}</strong><p v-if="forecastResult.state === 'projected'">현재 기록에 같은 추세가 이어진다는 가정에서 {{ date(Date.parse(forecastResult.projected_at)) }}에 입력 한계값에 도달할 것으로 추정됩니다. 그 전에 재측정·현장 검토가 필요합니다.</p><p v-for="(step, index) in forecastResult.plan" :key="index">{{ step }}</p><small>{{ forecastResult.sample_count }}건 · 사용자 입력 측정값과 한계값에 기반한 제한적 추정</small></div>
    </form>
  </section>
</template>

<style scoped>
.analysis-trend{grid-column:1/-1}.analysis-trend .section-heading small{font-size:11px;color:#87998a}.trend-sources,.trend-controls{display:flex;gap:7px;flex-wrap:wrap;margin:18px 0}.trend-sources button,.trend-controls button{border:1px solid #dce6db;background:#f8faf7;color:#5c745f;border-radius:5px;padding:8px 11px;font:inherit;font-size:12px;cursor:pointer}.trend-sources button.active,.trend-controls button.active{background:#e5f0e3;border-color:#91ad8a;color:#315f3a;font-weight:700}.trend-summary{display:flex;gap:18px;flex-wrap:wrap;align-items:baseline;color:#68816b;font-size:12px}.trend-summary strong{color:#294e39;font-size:16px}.trend-chart{width:100%;height:auto;min-height:160px;margin-top:16px;background:#fbfdfb;border:1px solid #e8eee7;border-radius:7px}.trend-chart text{fill:#839587;font-size:11px}.grid-line{stroke:#e6eee5;stroke-dasharray:4 4}.axis-line{stroke:#bbcbbb}.trend-line{stroke:#397d61;stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}.trend-point{fill:#397d61;stroke:white;stroke-width:1.5}.trend-note{font-size:11px;color:#78907c;line-height:1.7;margin-top:11px}.trend-forecast{margin-top:22px;border-top:1px solid #e5ebe2;padding-top:18px}.trend-forecast h3{font-size:15px;color:#315341;margin:0}.trend-forecast>p{font-size:11px;color:#78907c;line-height:1.7}.trend-forecast-fields{display:flex;align-items:end;gap:12px;flex-wrap:wrap}.trend-forecast-fields label{display:grid;gap:6px;font-size:11px;color:#68816b}.trend-forecast-fields label span{display:inline}.trend-forecast-fields input,.trend-forecast-fields select{border:1px solid #dce6db;border-radius:5px;background:white;color:#315341;padding:9px;font:inherit;min-width:150px}.trend-forecast-fields button{border:0;border-radius:5px;background:#397d61;color:white;padding:10px 15px;font:inherit;font-size:12px;cursor:pointer}.trend-forecast-fields button:disabled{opacity:.5;cursor:default}.trend-forecast-result{margin-top:15px;padding:14px;background:#f0f6ed;border:1px solid #d9e6d6;border-radius:6px;color:#42644e;font-size:12px;line-height:1.7}.trend-forecast-result strong{font-size:14px}.trend-forecast-result p{margin:7px 0}.trend-forecast-result small{color:#7f967f}
</style>
