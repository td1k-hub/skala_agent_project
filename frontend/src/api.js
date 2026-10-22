export class ApiError extends Error {
  constructor(message, code) { super(message); this.code = code }
}

export async function request(path, { method = 'GET', body, signal } = {}) {
  const response = await fetch(path, {
    method, signal,
    ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  })
  if (response.status === 204) return null
  let data
  try { data = await response.json() } catch { throw new ApiError('서버 응답을 읽지 못했습니다. 백엔드 연결을 확인해 주세요.') }
  if (!response.ok) throw new ApiError(data.error?.message || '요청을 처리하지 못했습니다.', data.error?.code)
  return data
}

// Incremental UTF-8 decoding and line parsing: CRLF, comments and multiline data.
export function createEventParser(onEvent) {
  let buffer = '', event = 'message', data = []
  function line(value) {
    if (value === '') {
      if (data.length) {
        let parsed
        try { parsed = JSON.parse(data.join('\n')) } catch { throw new ApiError('분석 이벤트의 형식을 읽지 못했습니다.') }
        onEvent(event, parsed)
      }
      event = 'message'; data = []
      return
    }
    if (value.startsWith(':')) return
    const colon = value.indexOf(':')
    const key = colon === -1 ? value : value.slice(0, colon)
    let content = colon === -1 ? '' : value.slice(colon + 1)
    if (content.startsWith(' ')) content = content.slice(1)
    if (key === 'event') event = content
    if (key === 'data') data.push(content)
  }
  return {
    push(chunk) {
      buffer += chunk
      let start = 0
      for (let i = 0; i < buffer.length; i++) {
        if (buffer[i] !== '\r' && buffer[i] !== '\n') continue
        if (buffer[i] === '\r' && i === buffer.length - 1) break
        line(buffer.slice(start, i))
        if (buffer[i] === '\r' && buffer[i + 1] === '\n') i++
        start = i + 1
      }
      buffer = buffer.slice(start)
      if (buffer.length > 1024 * 1024) throw new ApiError('분석 응답이 허용 크기를 초과했습니다.')
    },
    finish() {
      if (buffer.endsWith('\r')) this.push('\n')
    },
  }
}

export async function postStream(path, body, { signal, onEvent = () => {} }) {
  const response = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
  })
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    throw new ApiError(data.error?.message || '분석 요청에 실패했습니다.', data.error?.code)
  }
  if (!response.headers.get('content-type')?.includes('text/event-stream') || !response.body) {
    throw new ApiError('분석 스트림에 연결하지 못했습니다. 백엔드 연결을 확인해 주세요.')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let result = null
  const parser = createEventParser((event, data) => {
    if (event === 'error') throw new ApiError(data.message || '분석 중 오류가 발생했습니다.', data.code)
    if (event === 'result') result = data
    onEvent(event, data)
  })
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) { parser.push(decoder.decode()); parser.finish(); break }
      parser.push(decoder.decode(value, { stream: true }))
    }
    if (!result) throw new ApiError('최종 결과 없이 연결이 종료됐습니다. 다시 분석해 주세요.')
    return result
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

export const analyze = (body, options) => postStream('/api/v1/analyses/stream', body, options)
export const askChat = (sessionId, body, options) => postStream(`/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages/stream`, body, options)
