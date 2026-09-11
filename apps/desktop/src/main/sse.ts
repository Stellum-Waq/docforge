/**
 * 极简 SSE（Server-Sent Events）客户端。
 *
 * 内核用 SSE 推送任务进度。之所以不用 WebSocket：这里是纯单向的服务端推送，
 * SSE 协议更简单、EventSource 语义清晰、且断线重连逻辑我们本来就要自己控制
 * （因为要附带 Bearer Token，浏览器原生 EventSource 不支持自定义请求头）。
 *
 * 因此这里用 fetch + ReadableStream 手写解析。要点：
 *  * 事件以空行（\n\n）分隔，data 行可能有多条，需按规范用 \n 拼接
 *  * 以 ":" 开头的是注释行（服务端心跳），直接忽略
 *  * 分片可能把一个事件切成两半，必须保留跨 chunk 的缓冲区
 */

export interface SseHandlers {
  onEvent: (payload: unknown, eventName: string) => void
  onError?: (error: Error) => void
}

export async function streamSse(
  url: string,
  token: string,
  signal: AbortSignal,
  handlers: SseHandlers
): Promise<void> {
  const response = await fetch(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'text/event-stream',
      'Cache-Control': 'no-cache'
    },
    signal
  })

  if (!response.ok) {
    throw new Error(`事件流连接失败：HTTP ${response.status}`)
  }
  if (!response.body) {
    throw new Error('事件流没有响应体')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })

      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        dispatchBlock(block, handlers)
        boundary = buffer.indexOf('\n\n')
      }
    }
  } finally {
    // 主动取消时不会抛错，但也必须释放底层连接
    reader.releaseLock()
  }
}

function dispatchBlock(block: string, handlers: SseHandlers): void {
  let eventName = 'message'
  const dataLines: string[] = []

  for (const rawLine of block.split('\n')) {
    const line = rawLine.replace(/\r$/, '')
    if (!line || line.startsWith(':')) continue // 空行或心跳注释

    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    // 规范要求：冒号后若有一个空格要吃掉
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)

    if (field === 'event') eventName = value
    else if (field === 'data') dataLines.push(value)
  }

  if (dataLines.length === 0) return

  const raw = dataLines.join('\n')
  try {
    handlers.onEvent(JSON.parse(raw), eventName)
  } catch (err) {
    handlers.onError?.(new Error(`事件数据解析失败：${(err as Error).message}`))
  }
}
