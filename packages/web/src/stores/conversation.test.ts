import { describe, it, expect, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { mapDbTasks, useConversationStore } from './conversation'
import type { ToolCallRecord, DbMessage, DbTask } from '@/types'

function makeDbMessage(over: Partial<DbMessage> = {}): DbMessage {
  return {
    message_id: 'm1',
    role: 'user',
    content: 'hello',
    tool_calls: [],
    created_at: '2026-01-01T00:00:00Z',
    ...over,
  }
}

describe('conversation store - per-conversation runtime', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    // appendDelta persists a draft on every streaming turn — without
    // clearing localStorage the previous test's draft leaks into the next
    // one (loadHistory would recover it as an extra message).
    localStorage.clear()
  })

  it('isolates messages between conversations', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.addMessage({ id: '1', role: 'user', content: 'in A', timestamp: 1 })

    store.startConversation('conv-b')
    store.addMessage({ id: '2', role: 'user', content: 'in B', timestamp: 2 })

    // Switch back to A
    store.startConversation('conv-a')

    expect(store.messages.length).toBe(1)
    expect(store.messages[0].content).toBe('in A')
  })

  it('preserves streaming state when switching away and back', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.appendDelta('partial ', undefined, 'conv-a')
    store.appendDelta('response', undefined, 'conv-a')

    // Switch to B
    store.startConversation('conv-b')
    expect(store.streamingContent).toBe('')

    // Switch back to A
    store.startConversation('conv-a')
    expect(store.streamingContent).toBe('partial response')
    expect(store.isStreaming).toBe(true)
  })

  it('appendDelta with targetId updates the correct conversation', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.startConversation('conv-b')

    // Append to A while B is active
    store.appendDelta('text for A', undefined, 'conv-a')

    expect(store.streamingContent).toBe('') // B is active, no streaming
    // Switch to A to verify
    store.startConversation('conv-a')
    expect(store.streamingContent).toBe('text for A')
  })

  it('appendDelta with taskId routes to streamingByTask', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.appendDelta('task text ', 'task-1', 'conv-a')
    store.appendDelta('more', 'task-1', 'conv-a')

    // Global streamingContent should be empty (text went to per-task buffer)
    expect(store.streamingContent).toBe('')
    expect(store.isStreaming).toBe(true)
    expect(store.taskStreamingText('task-1')).toBe('task text more')
  })

  it('hasStreamingContent checks specific conversation', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.startConversation('conv-b')

    store.appendDelta('streaming in A', undefined, 'conv-a')

    expect(store.hasStreamingContent('conv-a')).toBe(true)
    expect(store.hasStreamingContent('conv-b')).toBe(false)
  })

  it('getActiveToolCalls returns calls for the specified conversation', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.startConversation('conv-b')

    const call: ToolCallRecord = {
      callId: 'call-1',
      tool: 'shell',
      input: {},
      status: 'running',
    }
    store.addToolCall(call, 'conv-a')

    expect(store.getActiveToolCalls('conv-a')).toHaveLength(1)
    expect(store.getActiveToolCalls('conv-b')).toHaveLength(0)
  })

  it('streamingIds tracks which conversations are streaming', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.startConversation('conv-b')

    store.appendDelta('text', undefined, 'conv-a')
    expect(store.streamingIds['conv-a']).toBe(true)
    expect(store.streamingIds['conv-b']).toBeUndefined()

    // Finalize streaming in A
    store.finalizeStreaming('msg-1', undefined, 'conv-a')
    expect(store.streamingIds['conv-a']).toBeUndefined()
  })

  it('finalizeStreaming with taskId clears per-task buffer', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.appendDelta('task text', 'task-1', 'conv-a')

    store.finalizeStreaming('msg-1', 'task-1', 'conv-a')

    expect(store.taskStreamingText('task-1')).toBe('')
  })

  it('applySync restores streaming state on reconnect', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')

    // Simulate sync from server
    store.applySync('conv-a', {
      streamingContent: 'restored text',
      toolCalls: [{
        callId: 'c1',
        tool: 'shell',
        input: {},
        status: 'running',
      }],
    })

    expect(store.streamingContent).toBe('restored text')
    expect(store.isStreaming).toBe(true)
    expect(store.activeToolCalls).toHaveLength(1)
  })

  it('applySync does not overwrite existing live streaming', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.appendDelta('live delta', undefined, 'conv-a')

    // Sync arrives but should not overwrite (we already have live content)
    store.applySync('conv-a', {
      streamingContent: 'sync text',
      toolCalls: [],
    })

    expect(store.streamingContent).toBe('live delta')
  })

  it('loadHistory loads into the specified conversation', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.startConversation('conv-b')

    // Load history into A while B is active
    const msgs = [makeDbMessage({ message_id: 'h1', content: 'history A' })]
    store.loadHistory(msgs, 'conv-a')

    // B is active, should have no messages
    expect(store.messages).toHaveLength(0)

    // Switch to A
    store.startConversation('conv-a')
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].content).toBe('history A')
  })

  it('loadHistory maps model_name onto messages', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.loadHistory([makeDbMessage({
      message_id: 'h1',
      role: 'assistant',
      content: 'auto reply',
      model_name: 'claude-sonnet-5',
    })], 'conv-a')
    expect(store.messages[0].modelName).toBe('claude-sonnet-5')
  })

  it('stream_end message carries the model from model_selected', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    // model_selected: auto routing resolved tier "mid" → model
    store.setModelInfo('mid', 'claude-sonnet-5', 'conv-a')
    store.appendDelta('auto reply', undefined, 'conv-a')

    store.pushStreamingMessage('assistant-1', undefined, false, 'conv-a')
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].modelTier).toBe('mid')
    expect(store.messages[0].modelName).toBe('claude-sonnet-5')
  })

  it('setTasks targets the specified conversation', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.startConversation('conv-b')

    const tasks: DbTask[] = [{
      task_id: 't1',
      title: 'Task A',
      status: 'running',
    }]
    store.setTasks(tasks, 'conv-a')

    expect(store.tasks).toHaveLength(0) // B is active

    store.startConversation('conv-a')
    expect(store.tasks).toHaveLength(1)
    expect(store.tasks[0].title).toBe('Task A')
  })

  it('reset clears all runtimes', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.addMessage({ id: '1', role: 'user', content: 'A', timestamp: 1 })
    store.appendDelta('stream', undefined, 'conv-a')

    store.startConversation('conv-b')
    store.addMessage({ id: '2', role: 'user', content: 'B', timestamp: 2 })

    store.reset()

    expect(store.conversationId).toBeNull()
    expect(store.messages).toHaveLength(0)
    expect(Object.keys(store.streamingIds).length).toBe(0)
  })

  it('removeRuntime removes a single conversation', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.addMessage({ id: '1', role: 'user', content: 'A', timestamp: 1 })

    store.startConversation('conv-b')
    store.removeRuntime('conv-a')

    // B is active, unaffected
    expect(store.messages).toHaveLength(0)
    // A is gone
    store.startConversation('conv-a')
    expect(store.messages).toHaveLength(0)
  })

  it('clearStreamingState targets specific conversation', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.appendDelta('streaming A', undefined, 'conv-a')

    store.startConversation('conv-b')
    store.appendDelta('streaming B', undefined, 'conv-b')

    // Clear A while B is active
    store.clearStreamingState('conv-a')

    expect(store.streamingContent).toBe('streaming B') // B unaffected

    store.startConversation('conv-a')
    expect(store.streamingContent).toBe('')
    expect(store.isStreaming).toBe(false)
  })

  it('main stream_end keeps parallel task streams alive', () => {
    // A main-stream stream_end must not wipe task streams still running:
    // the per-task buffer and streaming flag survive. A snapshot with no
    // text, no tool calls and no images/files is skipped — pushing it would
    // create a blank assistant bubble (the abort flow produces exactly such
    // empty final stream_ends).
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.appendDelta('task text ', 'task-1', 'conv-a')
    store.appendDelta('task more', 'task-1', 'conv-a')

    store.pushStreamingMessage('assistant-1', undefined, false, 'conv-a')

    expect(store.messages).toHaveLength(0)
    // Task stream still intact
    expect(store.taskStreamingText('task-1', 'conv-a')).toBe('task text task more')
    expect(store.isStreaming).toBe(true)

    // The task's own stream_end clears its buffer
    store.finalizeStreaming('assistant-1', 'task-1', 'conv-a')
    expect(store.taskStreamingText('task-1', 'conv-a')).toBe('')
    expect(store.isStreaming).toBe(false)
  })

  it('task round winds down when the last task completes', () => {
    // Mid-run-injection sequence: a task's stream_end can arrive BEFORE its
    // task_completed (the agent emits them in that order) — at stream_end the
    // task is still pending, so no wind-down (correct: the turn isn't over).
    // But when the last task later turns terminal, nothing else fires: the
    // finished tool card keeps activeToolCalls non-empty, and without the
    // finalizeStreaming/updateTask idle re-check the turn stays "running"
    // forever (spinning Stop button) while the next turn snapshots the done
    // task's cards into a fresh message.
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.startThinking('conv-a')
    store.setTasks([{ task_id: 't1', title: 'Plan', status: 'pending' }], 'conv-a')

    store.addToolCall({ callId: 'c1', tool: 'shell', input: {}, status: 'running', taskId: 't1' }, 'conv-a')
    store.completeToolCall('c1', { exit_code: 0 }, 'done', 'conv-a')
    expect(store.isStreaming).toBe(true) // turn still live

    // Task stream ends before task_completed arrives
    store.finalizeStreaming('assistant-1', 't1', 'conv-a')
    expect(store.isStreaming).toBe(true) // plan still active — no wind-down yet

    // Last task completes — the turn's true end
    store.updateTask('t1', { status: 'completed' }, 'conv-a')
    expect(store.isStreaming).toBe(false)
    expect(store.streamingIds['conv-a']).toBeUndefined()
    expect(store.activeToolCalls).toHaveLength(0)
  })

  it('task stream_end after task_completed also winds down', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.startThinking('conv-a')
    store.setTasks([{ task_id: 't1', title: 'Plan', status: 'running' }], 'conv-a')
    store.appendDelta('partial task text', 't1', 'conv-a')

    // task_completed lands first — plan now terminal, but the per-task
    // buffer is still streaming, so the turn stays alive.
    store.updateTask('t1', { status: 'completed' }, 'conv-a')
    expect(store.isStreaming).toBe(true)

    // The task's own stream_end clears the buffer and winds down.
    store.finalizeStreaming('assistant-1', 't1', 'conv-a')
    expect(store.isStreaming).toBe(false)
    expect(store.taskStreamingText('t1', 'conv-a')).toBe('')
  })

  it('appendDelta guards falsy deltas', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.appendDelta('real text', undefined, 'conv-a')
    // A malformed frame without `delta` must not append "undefined"
    store.appendDelta('' as string, undefined, 'conv-a')
    expect(store.streamingContent).toBe('real text')
  })

  it('real tool failure snapshot is pushed despite empty text', () => {
    // A tool that genuinely failed (not aborted) has no text — its error
    // tool card IS the turn's output and must be pushed, not skipped as an
    // "empty snapshot" (the skip exists only for abort duplicates).
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.addToolCall({
      callId: 'tc-1',
      tool: 'code_executor',
      input: { code: 'raise' },
      status: 'running',
    }, 'conv-a')
    store.completeToolCall('tc-1', { error: 'boom' }, 'error', 'conv-a')

    store.pushStreamingMessage('assistant-1', undefined, false, 'conv-a')

    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].toolCalls?.[0].status).toBe('error')
  })

  it('abort snapshot marks the turn so the final stream_end is skipped', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.appendDelta('partial text', undefined, 'conv-a')
    store.addToolCall({
      callId: 'tc-1',
      tool: 'code_executor',
      input: { code: 'x' },
      status: 'running',
    }, 'conv-a')

    // Stop: pushes the abort snapshot (text + error cards)…
    store.abortStreaming('工具调用已停止', 'conv-a')
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].isError).toBe(true)

    // …and the server's final stream_end finds nothing new to render.
    store.pushStreamingMessage('assistant-1', undefined, false, 'conv-a')
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].id.startsWith('abort-')).toBe(true)
  })

  it('new content clears the abort snapshot flag', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.appendDelta('partial', undefined, 'conv-a')
    store.abortStreaming('stop', 'conv-a')
    // A fresh turn starts streaming — the abort marker must be gone, so a
    // genuinely failed tool in this new turn is not mistaken for a
    // duplicate abort snapshot.
    store.appendDelta('next turn', undefined, 'conv-a')
    store.addToolCall({
      callId: 'tc-1',
      tool: 'code_executor',
      input: {},
      status: 'running',
    }, 'conv-a')
    store.completeToolCall('tc-1', { error: 'boom' }, 'error', 'conv-a')

    store.pushStreamingMessage('assistant-2', undefined, false, 'conv-a')
    expect(store.messages).toHaveLength(2) // abort msg + new turn snapshot
  })

  it('addStreamingFiles snapshots into the finalized message attachments', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.appendDelta('report ready', undefined, 'conv-a')
    store.addStreamingFiles([{
      id: 'f1',
      url: '/api/media/p/c/report.csv',
      name: 'report.csv',
      media_type: 'text/csv',
      size: 42,
    }], 'conv-a')

    store.pushStreamingMessage('assistant-1', undefined, false, 'conv-a')

    const msg = store.messages[0]
    expect(msg.attachments).toHaveLength(1)
    expect(msg.attachments![0].name).toBe('report.csv')
    // buffered files must not leak into the next turn
    store.appendDelta('next turn', undefined, 'conv-a')
    expect(store.streamingContent).toBe('next turn')
  })

  it('addStreamingFiles replaces a same-name deliverable instead of stacking', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.addStreamingFiles([{
      id: 'f1',
      url: '/api/media/p/c/code1.html',
      name: 'web-slides.html',
      media_type: 'text/html',
      size: 10,
    }], 'conv-a')
    store.addStreamingFiles([{
      id: 'f2',
      url: '/api/media/p/c/code2.html',
      name: 'web-slides.html',
      media_type: 'text/html',
      size: 12,
    }, {
      id: 'f3',
      url: '/api/media/p/c/code1.csv',
      name: 'data.csv',
      media_type: 'text/csv',
      size: 5,
    }], 'conv-a')

    store.pushStreamingMessage('assistant-1', undefined, false, 'conv-a')
    const msg = store.messages[0]
    expect(msg.attachments).toHaveLength(2)
    expect(msg.attachments!.find((a) => a.name === 'web-slides.html')!.id).toBe('f2')
  })

  it('streamingFiles are cleared with the rest of streaming state', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.addStreamingFiles([{
      id: 'f1',
      url: '/api/media/p/c/report.csv',
      name: 'report.csv',
      media_type: 'text/csv',
      size: 42,
    }], 'conv-a')

    store.clearStreamingState('conv-a')

    store.addStreamingFiles([{
      id: 'f1',
      url: '/api/media/p/c/report.csv',
      name: 'report.csv',
      media_type: 'text/csv',
      size: 42,
    }], 'conv-a')
    store.pushStreamingMessage('assistant-2', undefined, false, 'conv-a')
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].attachments).toHaveLength(1)
  })

  it('images and files both snapshot into one message', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.appendDelta('analysis done', undefined, 'conv-a')
    store.addStreamingImages([{ id: 'i1', url: '/api/media/p/c/shot.png', alt: 'shot' }], 'conv-a')
    store.addStreamingFiles([{
      id: 'f1',
      url: '/api/media/p/c/data.json',
      name: 'data.json',
      media_type: 'application/json',
      size: 2,
    }], 'conv-a')

    store.pushStreamingMessage('assistant-3', undefined, false, 'conv-a')

    const msg = store.messages[0]
    expect(msg.images).toHaveLength(1)
    expect(msg.attachments).toHaveLength(1)
  })

  it('second Stop before abort_ack pushes no duplicate abort message', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.appendDelta('partial', undefined, 'conv-a')
    store.addToolCall({
      callId: 'tc-1',
      tool: 'code_executor',
      input: {},
      status: 'running',
    }, 'conv-a')

    store.abortStreaming('stop', 'conv-a')
    expect(store.messages).toHaveLength(1)

    // Parallel-task streaming keeps isStreaming alive, so the Stop button
    // stays visible — a second click before abort_ack arrives must not
    // append a second abort bubble.
    store.abortStreaming('stop again', 'conv-a')
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].content).toBe('partial')
  })

  it('applySync resets the abort snapshot flag for live content', () => {
    // Half-open connection: Stop pushes the abort snapshot locally while the
    // server turn actually kept running. Reconnect sync restores live state —
    // a later all-failed stream_end of THAT turn must not be skipped as an
    // abort duplicate.
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.appendDelta('partial', undefined, 'conv-a')
    store.abortStreaming('stop', 'conv-a')

    store.applySync('conv-a', {
      streamingContent: 'restored live text',
      toolCalls: [],
    })
    store.appendDelta(' continues', undefined, 'conv-a')
    store.addToolCall({
      callId: 'tc-1',
      tool: 'code_executor',
      input: {},
      status: 'running',
    }, 'conv-a')
    store.completeToolCall('tc-1', { error: 'boom' }, 'error', 'conv-a')

    store.pushStreamingMessage('assistant-2', undefined, false, 'conv-a')
    expect(store.messages).toHaveLength(2) // abort msg + real failure snapshot
    expect(store.messages[1].toolCalls?.[0].status).toBe('error')
  })

  it('loadHistory keeps messages pushed after the history snapshot', () => {
    // Reconnect race: the final stream_end message was pushed while the
    // history GET was in flight — the server persists it only AFTER pushing
    // it, so it sits in neither history nor the draft. The non-streaming
    // branch must not drop the just-finished answer.
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.addMessage({
      id: 'live-final',
      role: 'assistant',
      content: 'just finished',
      timestamp: Date.now() + 1000,
    })

    store.loadHistory([makeDbMessage({
      message_id: 'h1',
      role: 'user',
      content: 'older question',
      created_at: '2026-01-01T00:00:00Z',
    })], 'conv-a')

    expect(store.messages.map((m) => m.id)).toEqual(['h1', 'live-final'])
  })

  it('loadHistory streaming branch unregisters a deduped injection', () => {
    // A turn is live (isStreaming) and the server already persisted the
    // injected message — the content-match dedup drops the optimistic copy,
    // and the stale pendingInjected entry must go too, or confirmInjected's
    // content-fallback could latch onto it for a later, genuinely-new
    // injection of the same text.
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.startThinking('conv-a') // turn is live
    store.addMessage({
      id: 'opt-inj',
      role: 'user',
      content: 'injected instruction',
      timestamp: Date.now(),
    })
    store.registerInjectedMessage('opt-inj', 'injected instruction', 'conv-a')

    store.loadHistory([makeDbMessage({
      message_id: 'h1',
      role: 'user',
      content: 'injected instruction',
      created_at: new Date(Date.now() + 5000).toISOString(),
    })], 'conv-a')

    expect(store.messages.map((m) => m.id)).toEqual(['h1'])
    expect(store.pendingInjected.length).toBe(0)
  })

  it('loadHistory drops stale local messages older than the history tail', () => {
    // Compression replaced old rows with a summary line — stale local copies
    // from before the summary must not resurrect alongside it.
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.addMessage({
      id: 'stale-old',
      role: 'assistant',
      content: 'old answer',
      timestamp: 1000,
    })

    store.loadHistory([makeDbMessage({
      message_id: 'h1',
      role: 'assistant',
      content: 'summary of old rows',
      created_at: '2026-08-14T12:00:00Z',
    })], 'conv-a')

    expect(store.messages.map((m) => m.id)).toEqual(['h1'])
  })

  it('loadHistory resurrects a draft newer than the last persisted message', () => {
    localStorage.setItem('agents-universe:draft:conv-a', JSON.stringify({
      activeToolCalls: [{ callId: 'c1', tool: 'shell', input: {}, status: 'running' }],
      streamingContent: 'interrupted text',
      streamingImages: [],
      streamingFiles: [],
      tasks: [],
      savedAt: Date.now(),
    }))
    const store = useConversationStore()
    store.startConversation('conv-a')

    store.loadHistory([makeDbMessage({
      message_id: 'h1',
      role: 'assistant',
      content: 'persisted reply',
      created_at: '2026-01-01T00:00:00Z',
    })], 'conv-a')

    // Draft is newer than the last message → the interrupted execution is
    // recovered as an error bubble.
    expect(store.messages.length).toBe(2)
    expect(store.messages[1].isError).toBe(true)
    expect(store.messages[1].id.startsWith('recovered-')).toBe(true)
    localStorage.removeItem('agents-universe:draft:conv-a')
  })

  it('loadHistory does not resurrect a draft older than the last message', () => {
    // The turn finished while offline: its output was persisted, so the
    // stale draft must not come back as a fake "interrupted" recovery note.
    localStorage.setItem('agents-universe:draft:conv-a', JSON.stringify({
      activeToolCalls: [{ callId: 'c1', tool: 'shell', input: {}, status: 'running' }],
      streamingContent: 'stale text',
      streamingImages: [],
      streamingFiles: [],
      tasks: [],
      savedAt: 1000,
    }))
    const store = useConversationStore()
    store.startConversation('conv-a')

    store.loadHistory([makeDbMessage({
      message_id: 'h1',
      role: 'assistant',
      content: 'finished while offline',
      created_at: '2026-08-14T12:00:00Z',
    })], 'conv-a')

    expect(store.messages.length).toBe(1)
    expect(store.messages[0].id).toBe('h1')
    localStorage.removeItem('agents-universe:draft:conv-a')
  })
})

describe('conversation store - durable last-run status', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  const failedRun = {
    run_id: 'r1',
    status: 'failed' as const,
    user_message_id: 'um-1',
    started_at: '2026-08-24T00:00:00Z',
    ended_at: '2026-08-24T00:00:01Z',
    error_message: 'boom',
    streaming_snapshot: 'partial text',
    tokens_used: null,
  }

  it('setLastRun stores the run for the active conversation', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.setLastRun(failedRun)
    expect(store.lastRun?.status).toBe('failed')
  })

  it('setLastRun with targetId stores into a not-yet-active runtime', () => {
    const store = useConversationStore()
    store.startConversation('conv-b')
    // A background conversation's run is fetched before its runtime exists —
    // setLastRun must create it, not throw.
    store.setLastRun(failedRun, 'conv-a')
    store.startConversation('conv-a')
    expect(store.lastRun?.run_id).toBe('r1')
  })

  it('startThinking clears the stale notice when a new turn begins', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.setLastRun(failedRun)
    store.startThinking('conv-a')
    expect(store.lastRun).toBeNull()
  })

  it('clearLastRun nulls the run without touching other state', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.setLastRun(failedRun)
    store.addMessage({ id: '1', role: 'user', content: 'hi', timestamp: 1 })
    store.clearLastRun('conv-a')
    expect(store.lastRun).toBeNull()
    expect(store.messages).toHaveLength(1)
  })

  it('removeRuntime drops the stored run with the conversation', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.setLastRun(failedRun)
    store.removeRuntime('conv-a')
    store.startConversation('conv-a')
    expect(store.lastRun).toBeNull()
  })

  it('reset clears lastRun across runtimes', () => {
    const store = useConversationStore()
    store.startConversation('conv-a')
    store.setLastRun(failedRun)
    store.reset()
    expect(store.lastRun).toBeNull()
  })
})

describe('mapDbTasks', () => {  it('maps error_message to the error field', () => {
    const tasks: DbTask[] = [{
      task_id: 't1',
      title: 'Task A',
      status: 'failed',
      error_message: 'LLM API error',
    }]
    const mapped = mapDbTasks(tasks)
    expect(mapped[0].error).toBe('LLM API error')
    expect(mapped[0].status).toBe('failed')
  })

  it('passes skipped status through from DB rows', () => {
    // The stale-task reconcile and cascade-skip persist `skipped` — the
    // sidebar must render a grey dash, not a red error, after reload.
    const tasks: DbTask[] = [{
      task_id: 't2',
      title: 'Task B',
      status: 'skipped',
      error_message: 'Skipped: dependency failed',
    }]
    const mapped = mapDbTasks(tasks)
    expect(mapped[0].status).toBe('skipped')
    expect(mapped[0].error).toBe('Skipped: dependency failed')
  })

  it('leaves error undefined when error_message is absent', () => {
    const tasks: DbTask[] = [{
      task_id: 't3',
      title: 'Task C',
      status: 'completed',
    }]
    const mapped = mapDbTasks(tasks)
    expect(mapped[0].error).toBeUndefined()
  })
})
