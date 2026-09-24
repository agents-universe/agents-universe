import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { useScriptRunLog, type LogLine } from './useScriptRunLog'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  onmessage: ((e: MessageEvent) => void) | null = null
  onclose: ((e: CloseEvent) => void) | null = null
  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }
  close() {
    // Tests fire onclose manually — a real close event would race the
    // assertion the same way the production bug does.
  }
  send(_data: string) {}
}

// The composable must run inside a component (onBeforeUnmount + useI18n);
// capture its API from setup.
let api: ReturnType<typeof useScriptRunLog> | null = null
const Host = defineComponent({
  setup() {
    api = useScriptRunLog()
    return () => h('div')
  },
})

function wsAt(i: number): FakeWebSocket {
  return FakeWebSocket.instances[i]
}

describe('useScriptRunLog reconnect', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    api = null
    vi.stubGlobal('WebSocket', FakeWebSocket)
    mount(Host)
  })

  it('reconnecting to the same run does not inject a bogus connection-lost line', () => {
    // connectToRun resets the SHARED wsFinished flag before the old socket's
    // close event lands; the old onclose passed the guard (alive, same runId)
    // and pushed "connection lost" into the freshly cleared buffer of the new
    // connection — a red error line for a run that never dropped.
    api!.connectToRun('r-1')
    expect(FakeWebSocket.instances).toHaveLength(1)

    api!.connectToRun('r-1') // same run: server would replay from scratch
    expect(FakeWebSocket.instances).toHaveLength(2)

    // Old socket's close arrives late (close() only queues the event).
    wsAt(0).onclose?.({} as CloseEvent)
    expect(api!.logs.value).toEqual([])
  })

  it('still reports a genuine drop of the active connection', () => {
    api!.connectToRun('r-2')
    wsAt(0).onclose?.({} as CloseEvent)

    const errs = api!.logs.value.filter((l: LogLine) => l.level === 'error')
    expect(errs).toHaveLength(1)
  })
})
