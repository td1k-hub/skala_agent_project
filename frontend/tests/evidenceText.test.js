import test from 'node:test'
import assert from 'node:assert/strict'
import { describeEvidence, previewEvidenceIds } from '../src/evidenceText.js'

test('renders current measurements and synthetic history in plain Korean', () => {
  assert.deepEqual(describeEvidence({ id: 'current:vibration_mm_s', observation: 'vibration_mm_s=20.0' }), {
    title: '현재 진동 20 mm/s', detail: '이번 분석에 입력한 측정값',
  })
  const history = describeEvidence({ id: 'history:vibration_mm_s', observation: '합성 이력 6건; vibration_mm_s: 2026-10-21T05:32:27+00:00=1.52, 2026-10-21T10:32:27+00:00=1.42; 이력 처음→마지막 변화=-0.100; 현재−마지막 이력=18.580' })
  assert.equal(history.title, '운전 이력 · 진동')
  assert.match(history.detail, /첫 1.52 → 최근 1.42 mm\/s/)
  assert.match(history.detail, /\+18.58 mm\/s/)
  assert.doesNotMatch(history.detail, /vibration_mm_s|2026-10-21T/)
})

test('renders inspection JSON as a dated check and outcome', () => {
  const result = describeEvidence({ id: 'inspection:one', observation: JSON.stringify({ created_at: '2026-10-21T05:01:44+00:00', checks: ['visual', 'temperature'], outcome: 'repair_required' }) })
  assert.match(result.title, /^점검 기록 · /)
  assert.equal(result.detail, '외관·온도 · 정비 필요')
  assert.doesNotMatch(result.detail, /created_at|repair_required/)
})

test('keeps the preview short while retaining all referenced evidence', () => {
  assert.deepEqual(previewEvidenceIds({ evidence_ids: ['current:vibration_mm_s', 'current:bearing_temperature_c', 'history:vibration_mm_s', 'inspection:one'] }), ['current:vibration_mm_s', 'current:bearing_temperature_c'])
  assert.deepEqual(previewEvidenceIds({ evidence_ids: ['history:vibration_mm_s', 'inspection:one'] }), ['history:vibration_mm_s'])
  assert.equal(describeEvidence({ id: 'maintenance:empty', observation: '' }).title, '점검 이력 없음')
})
