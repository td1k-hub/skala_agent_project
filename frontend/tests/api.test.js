import { test } from 'node:test'
import assert from 'node:assert/strict'
import { analyze, askChat, createEventParser, request } from '../src/api.js'

function streamResponse(text, width = 1) {
  const bytes = new TextEncoder().encode(text)
  return new Response(new ReadableStream({ start(controller) {
    for (let i = 0; i < bytes.length; i += width) controller.enqueue(bytes.slice(i, i + width))
    controller.close()
  } }), { headers: { 'Content-Type': 'text/event-stream' } })
}

test('parses comments, split CRLF and multiline JSON', () => {
  const events = []
  const parser = createEventParser((event, data) => events.push([event, data]))
  const text = ': heartbeat\r\n\r\nid: a:1\r\nevent: result\r\ndata: {\r\ndata: "status": "주의"}\r\n\r\n'
  for (const char of text) parser.push(char)
  assert.deepEqual(events, [['result', { status: '주의' }]])
})

test('decodes Korean UTF-8 split at every byte and sends POST body', async t => {
  const events = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/v1/analyses/stream')
    assert.equal(options.method, 'POST')
    assert.deepEqual(JSON.parse(options.body), { equipment_id: 'P-101' })
    return streamResponse('event: started\ndata: {"run_id":"a"}\n\n: heartbeat\n\nevent: result\ndata: {"status":"주의"}\n\n')
  })
  const result = await analyze({ equipment_id: 'P-101' }, { onEvent: event => events.push(event) })
  assert.equal(result.status, '주의')
  assert.deepEqual(events, ['started', 'result'])
})

test('accepts a CR-only event delimiter at end of stream', async t => {
  t.mock.method(globalThis, 'fetch', async () => streamResponse('event: result\rdata: {"status":"관찰"}\r\r'))
  assert.equal((await analyze({}, { onEvent() {} })).status, '관찰')
})

test('stream errors propagate without a result', async t => {
  t.mock.method(globalThis, 'fetch', async () => streamResponse('event: error\ndata: {"code":"UPSTREAM_TIMEOUT","message":"시간 초과"}\n\n'))
  await assert.rejects(analyze({}, { onEvent() {} }), err => err.code === 'UPSTREAM_TIMEOUT' && err.message === '시간 초과')
})

test('incomplete stream fails even with completed stages', async t => {
  t.mock.method(globalThis, 'fetch', async () => streamResponse('event: stage\ndata: {"state":"completed"}\n\n'))
  await assert.rejects(analyze({}, { onEvent() {} }), /최종 결과 없이/)
})

test('malformed events do not expose raw contents', async t => {
  t.mock.method(globalThis, 'fetch', async () => streamResponse('event: result\ndata: PRIVATE_INVALID_JSON\n\n'))
  await assert.rejects(analyze({}, { onEvent() {} }), err => !err.message.includes('PRIVATE_INVALID_JSON') && err.message.includes('형식'))
})

test('abort propagates through the active stream', async t => {
  const abort = new AbortController()
  t.mock.method(globalThis, 'fetch', async (_, { signal }) => new Response(new ReadableStream({ start(controller) {
    signal.addEventListener('abort', () => controller.error(new DOMException('Aborted', 'AbortError')))
    controller.enqueue(new TextEncoder().encode('event: started\ndata: {}\n\n'))
  } }), { headers: { 'Content-Type': 'text/event-stream' } }))
  const pending = analyze({}, { signal: abort.signal, onEvent() { abort.abort() } })
  await assert.rejects(pending, { name: 'AbortError' })
})

test('HTTP errors preserve sanitized server code and message', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ error: { code: 'INVALID_INPUT', message: '입력 확인' } }), { status: 422 }))
  await assert.rejects(analyze({}, { onEvent() {} }), err => err.code === 'INVALID_INPUT')
  await assert.rejects(request('/api/v1/inspections', { method: 'POST', body: {} }), /입력 확인/)
})

test('HTML proxy errors are not accepted as an SSE stream', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response('<html>Oops</html>', { headers: { 'Content-Type': 'text/html' } }))
  await assert.rejects(analyze({}, { onEvent() {} }), /백엔드 연결/)
})

test('chat streams use the selected server session and only send the question', async t => {
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/v1/chat/sessions/session-123/messages/stream')
    assert.deepEqual(JSON.parse(options.body), { message: '최근 점검 이력이 있어?' })
    return streamResponse('event: stage\ndata: {"stage":"chat_review","state":"started"}\n\nevent: result\ndata: {"answer":"저장된 점검 기록이 없습니다.","sources":[]}\n\n')
  })
  const events = []
  const result = await askChat('session-123', { message: '최근 점검 이력이 있어?' }, { onEvent: name => events.push(name) })
  assert.match(result.answer, /점검 기록/)
  assert.deepEqual(events, ['stage', 'result'])
})

test('successful chat deletion accepts an empty 204 response', async t => {
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(options.method, 'DELETE')
    return new Response(null, { status: 204 })
  })
  assert.equal(await request('/api/v1/chat/sessions/123', { method: 'DELETE' }), null)
})
