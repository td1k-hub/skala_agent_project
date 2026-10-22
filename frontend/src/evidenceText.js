const measurements = {
  vibration_mm_s: ['진동', 'mm/s'],
  bearing_temperature_c: ['베어링 온도', '°C'],
  suction_pressure_bar: ['흡입 압력', 'bar'],
  discharge_pressure_bar: ['토출 압력', 'bar'],
  flow_m3_h: ['유량', 'm³/h'],
  motor_current_a: ['모터 전류', 'A'],
}
const checks = { visual: '외관', vibration: '진동', temperature: '온도', leak: '누유', alignment: '정렬' }
const outcomes = { pass: '점검 통과', follow_up: '추가 점검', repair_required: '정비 필요' }

function number(value) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 3 }).format(parsed) : null
}

function date(value) {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '날짜 미확인' : parsed.toLocaleString('ko-KR', {
    month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit',
  })
}

export function describeEvidence(item) {
  if (!item) return { title: '근거를 찾을 수 없음', detail: '저장된 관측 기록을 확인해 주세요.' }
  const [kind, key] = (item.id || '').split(':', 2)
  if (kind === 'current' && measurements[key]) {
    const [label, unit] = measurements[key]
    const value = number(item.observation?.split('=')[1])
    return { title: value === null ? `현재 ${label}` : `현재 ${label} ${value} ${unit}`, detail: '이번 분석에 입력한 측정값' }
  }
  if (kind === 'history' && measurements[key]) {
    const [label, unit] = measurements[key]
    const count = item.observation?.match(/^합성 이력 (\d+)건/)
    const record = item.observation?.split('; ')[1] || ''
    const values = [...record.matchAll(/=([-+]?\d+(?:\.\d+)?)(?=,|$)/g)].map(match => number(match[1]))
    const difference = item.observation?.match(/현재−마지막 이력=([-+]?\d+(?:\.\d+)?)/)
    const summary = values.length >= 2 ? `첫 ${values[0]} → 최근 ${values[1]} ${unit}` : '수치 확인 필요'
    const comparison = difference ? ` · 현재와 최근 기록의 차이 ${Number(difference[1]) > 0 ? '+' : ''}${number(difference[1])} ${unit}` : ''
    return { title: `운전 이력 · ${label}`, detail: `합성 관측 ${count?.[1] || '?'}건: ${summary}${comparison}` }
  }
  if (kind === 'inspection') {
    try {
      const value = JSON.parse(item.observation)
      const performed = Array.isArray(value.checks) ? value.checks.map(check => checks[check] || check).join('·') : '항목 미확인'
      return { title: `점검 기록 · ${date(value.created_at)}`, detail: `${performed} · ${outcomes[value.outcome] || '결과 미확인'}` }
    } catch {
      return { title: '점검 기록', detail: '기록 형식을 읽을 수 없습니다.' }
    }
  }
  if (item.id === 'maintenance:empty') return { title: '점검 이력 없음', detail: '저장된 기록이 없습니다. 고장이 없다는 뜻은 아닙니다.' }
  return { title: '관측 기록', detail: '화면에서 해석할 수 없는 형식입니다. 실행 정보를 확인해 주세요.' }
}

export function previewEvidenceIds(cause) {
  const ids = cause?.evidence_ids || []
  const current = ids.filter(id => id.startsWith('current:'))
  return (current.length ? current : ids.filter(id => id.startsWith('history:'))).slice(0, 3)
}
