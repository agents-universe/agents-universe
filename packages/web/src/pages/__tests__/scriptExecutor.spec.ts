import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ScriptExecutorPage from '@/pages/ScriptExecutorPage.vue'
import { useProjectStore } from '@/stores/project'
import { apiFetch } from '@/api/client'

vi.mock('@/api/client', () => ({ apiFetch: vi.fn() }))
const apiFetchMock = vi.mocked(apiFetch) as unknown as ReturnType<typeof vi.fn>

// ── Fake WebSocket (same pattern as injection.spec.ts) ──────────────────────

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  url: string
  readyState = 0
  onopen: ((e: unknown) => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: ((e: unknown) => void) | null = null
  sent: string[] = []

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  send(data: string) { this.sent.push(data) }
  close() { this.readyState = 3 }
  emit(payload: unknown) { this.onmessage?.({ data: JSON.stringify(payload) }) }
}

function lastSocket(): FakeWebSocket {
  const ws = FakeWebSocket.instances.at(-1)
  if (!ws) throw new Error('no WebSocket created')
  return ws
}

function setCurrentProject(id: string) {
  useProjectStore().setCurrentProject({ project_id: id, display_name: id } as never)
}

function mountPage() {
  return mount(ScriptExecutorPage)
}

describe('ScriptExecutorPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
    apiFetchMock.mockReset()
    localStorage.clear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('loads custom scripts on mount and renders them', async () => {
    apiFetchMock.mockResolvedValue([{ script_id: 's1', name: 'hello', script_type: 'python' }])
    setCurrentProject('p1')
    const wrapper = mountPage()
    await flushPromises()
    expect(apiFetchMock).toHaveBeenCalledWith('/api/projects/p1/scripts')
    expect(wrapper.text()).toContain('hello')
  })

  it('loads playwright specs when switching to the playwright section', async () => {
    apiFetchMock.mockImplementation(async (url: string) =>
      url.includes('/playwright/specs')
        ? [{ slug: 'proj-1', title: 'Login flow', file: 'tests/generated/proj-1.spec.ts' }]
        : [])
    setCurrentProject('p1')
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.find('[data-tour-target="executor-section-playwright"]').trigger('click')
    await flushPromises()
    expect(apiFetchMock).toHaveBeenCalledWith('/api/projects/p1/playwright/specs')
    expect(wrapper.text()).toContain('Login flow')
  })

  it('runs a spec: POSTs with the base-url env, streams logs, applies the done frame', async () => {
    apiFetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/run')) return { run_id: 'r9', status: 'pending' }
      if (url.includes('/playwright/specs')) {
        return [{ slug: 'proj-1', title: 'Login flow', file: 'tests/generated/proj-1.spec.ts' }]
      }
      return []
    })
    setCurrentProject('p1')
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-tour-target="executor-section-playwright"]').trigger('click')
    await flushPromises()

    await wrapper.find('.executor-base-url').setValue('http://demo.local')
    await wrapper.find('.script-item').trigger('click')
    await flushPromises()

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/projects/p1/playwright/specs/proj-1/run',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ env: { APP_BASE_URL: 'http://demo.local' } }),
      }),
    )
    const sock = lastSocket()
    expect(sock.url).toContain('/ws/script-runs/r9')

    // Phase progress frames stream into the log panel.
    sock.emit({ type: 'log', log: '[executor] Preparing test dependencies' })
    await flushPromises()
    expect(wrapper.find('.script-log-output').text()).toContain('Preparing test dependencies')

    // The done frame is authoritative for the final status.
    sock.emit({ type: 'done', status: 'completed' })
    await flushPromises()
    expect(wrapper.find('.script-status').classes()).toContain('completed')
  })

  it('marks a run failed when the socket closes without a done frame', async () => {
    apiFetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/run')) return { run_id: 'r9', status: 'pending' }
      if (url.includes('/playwright/specs')) {
        return [{ slug: 'proj-1', title: 'Login flow', file: 'x' }]
      }
      return []
    })
    setCurrentProject('p1')
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-tour-target="executor-section-playwright"]').trigger('click')
    await flushPromises()
    await wrapper.find('.script-item').trigger('click')
    await flushPromises()

    const sock = lastSocket()
    sock.onclose?.({})
    await flushPromises()
    expect(wrapper.find('.script-status').classes()).toContain('failed')
    // Never a green 'completed' on an abnormal close - the run may still be
    // executing server-side.
    expect(wrapper.find('.script-status').classes()).not.toContain('completed')
  })

  it('closes the socket and reloads the lists when the project switches', async () => {
    apiFetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/run')) return { run_id: 'r9', status: 'pending' }
      if (url.includes('/playwright/specs')) {
        return [{ slug: 'proj-1', title: 'Login flow', file: 'x' }]
      }
      return []
    })
    setCurrentProject('p1')
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('[data-tour-target="executor-section-playwright"]').trigger('click')
    await flushPromises()
    await wrapper.find('.script-item').trigger('click')
    await flushPromises()
    const sock = lastSocket()

    setCurrentProject('p2')
    await flushPromises()

    expect(sock.readyState).toBe(3) // old run's socket dropped
    expect(apiFetchMock).toHaveBeenCalledWith('/api/projects/p2/scripts')
    expect(apiFetchMock).toHaveBeenCalledWith('/api/projects/p2/playwright/specs')
  })
})
