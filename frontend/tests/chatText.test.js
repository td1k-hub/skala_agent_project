import test from 'node:test'
import assert from 'node:assert/strict'
import { displayChatText } from '../src/chatText.js'

test('hides embedded source metadata in previously saved answers', () => {
  assert.equal(displayChatText("진동을 확인했습니다. (source_ids: ['analysis:abc-123'])"), '진동을 확인했습니다.')
  assert.equal(displayChatText("진동을 확인했습니다. source_ids: ['analysis:abc-123']"), '진동을 확인했습니다.')
  assert.equal(displayChatText('근거 확인.\n（source_ids: ["equipment:P-101", "manual:x"]）\n추가 점검.'), '근거 확인.\n추가 점검.')
})

test('preserves ordinary prose, units, parentheses and measurements', () => {
  const text = '진동(mm/s)은 [1.5, 2.0]입니다. (추가 확인 필요)'
  assert.equal(displayChatText(text), text)
})
