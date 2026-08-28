import { describe, expect, it, vi, afterEach } from 'vitest'
import { publishApi } from './publish'

function sseStream(frames: string[], chunkSizes?: number[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  const data = encoder.encode(frames.join('\n\n') + '\n\n')
  const sizes = chunkSizes ?? [data.length]
  let offset = 0
  const parts: Uint8Array[] = []
  for (const size of sizes) {
    parts.push(data.slice(offset, offset + size))
    offset += size
  }
  return new ReadableStream({
    start(controller) {
      for (const p of parts) controller.enqueue(p)
      controller.close()
    },
  })
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('publishApi.runSession', () => {
  it('accumulates stream_delta across chunks and reports deltas live', async () => {
    const deltaA = { type: 'stream_delta', delta: '你好' }
    const deltaB = { type: 'stream_delta', delta: '，世界' }
    const end = { type: 'stream_end' }
    const frames = [
      `data: ${JSON.stringify(deltaA)}`,
      `data: ${JSON.stringify(deltaB)}`,
      `data: ${JSON.stringify(end)}`,
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      body: sseStream(frames, [12, 40, 80]),
    }))

    const seen: string[] = []
    const text = await publishApi.runSession('p1', 'tok', 'hello', (d) => seen.push(d))

    expect(text).toBe('你好，世界')
    expect(seen).toEqual(['你好', '，世界'])
  })

  it('throws ApiError on an SSE error frame', async () => {
    const frames = [
      `data: ${JSON.stringify({ type: 'error', message: '模型调用失败' })}`,
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      body: sseStream(frames),
    }))

    await expect(publishApi.runSession('p1', 'tok', 'hello'))
      .rejects.toThrowError('模型调用失败')
  })

  it('throws with the server detail on non-ok responses', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      statusText: 'Conflict',
      json: async () => ({ detail: '该会话已有一轮运行' }),
    }))

    await expect(publishApi.runSession('p1', 'tok', 'hello'))
      .rejects.toThrowError('该会话已有一轮运行')
  })

  it('skips malformed frames without aborting the stream', async () => {
    const delta = { type: 'stream_delta', delta: 'ok' }
    const frames = [
      'data: not-json{{{',
      `data: ${JSON.stringify(delta)}`,
      `data: ${JSON.stringify({ type: 'stream_end' })}`,
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      body: sseStream(frames, [9, 30, 60]),
    }))

    const text = await publishApi.runSession('p1', 'tok', 'hello')
    expect(text).toBe('ok')
  })
})
