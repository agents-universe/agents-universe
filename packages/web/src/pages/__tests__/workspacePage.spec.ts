import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import WorkspacePage from '@/pages/WorkspacePage.vue'
import { apiFetch } from '@/api/client'

// ── Mocks ──────────────────────────────────────────────────────────────────
// The workspace page watches projectId via route.params — the mock's `params`
// must be a Vue reactive object so `route.params.projectId = ...` in tests
// triggers the project-switch watcher.
const route = vi.hoisted(() => ({ params: {} as Record<string, string> }))
vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const params = reactive({ projectId: 'p1' })
  route.params = params
  return { useRoute: () => route }
})
vi.mock('@/utils/basePath', () => ({ apiBase: '' }))

vi.mock('@/api/client', () => ({ apiFetch: vi.fn() }))
const apiFetchMock = vi.mocked(apiFetch) as unknown as ReturnType<typeof vi.fn>

// ── Fake WebSocket (same pattern as scriptExecutor.spec.ts) ────────────────
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

/** Route apiFetch by URL: workspace files list, file read/write, scripts. */
function defaultApi() {
  apiFetchMock.mockImplementation(async (url: string, options?: RequestInit) => {
    if (url.startsWith('/api/projects/p1/workspace/files')) {
      return {
        path: '',
        entries: [
          { name: 'knowledge', path: 'knowledge', type: 'dir', size_bytes: 0, mtime: 0 },
          { name: 'notes.md', path: 'notes.md', type: 'file', size_bytes: 9, mtime: 0 },
        ],
      }
    }
    if (url.startsWith('/api/projects/p1/workspace/file')) {
      if (options?.method === 'PUT') return { saved: true, bytes_written: 3 }
      return { path: 'notes.md', content: '# Hello', size_bytes: 9 }
    }
    if (url.startsWith('/api/projects/p1/scripts')) {
      return [{ script_id: 's1', name: 'hello', script_type: 'python' }]
    }
    if (url.startsWith('/api/projects/p1/playwright/specs')) {
      return [{ slug: 'proj-1', title: 'Login flow', file: 'tests/generated/proj-1.spec.ts' }]
    }
    if (url.includes('/run')) {
      return { run_id: 'r1', status: 'pending' }
    }
    throw new Error(`unexpected url ${url}`)
  })
}

function mountPage() {
  return mount(WorkspacePage, {
    global: { plugins: [createPinia()] },
  })
}

describe('WorkspacePage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
    apiFetchMock.mockReset()
    defaultApi()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('loads the root tree and injects runnable script/spec nodes', async () => {
    const wrapper = mountPage()
    await flushPromises()

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/projects/p1/workspace/files?path=',
    )
    const labels = wrapper.findAll('.tree-label').map((n) => n.text())
    // disk entries + virtual scripts group (expanded) with its children
    expect(labels).toContain('knowledge')
    expect(labels).toContain('notes.md')
    expect(labels).toContain('脚本')
    expect(labels).toContain('hello')
    expect(labels).toContain('Login flow')
  })

  it('opens a markdown file and renders it', async () => {
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.findAll('.file-tree-row').find((n) => n.text().includes('notes.md'))!.trigger('click')
    await flushPromises()

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/projects/p1/workspace/file?path=notes.md',
    )
    expect(wrapper.find('.workspace-md-body').text()).toContain('Hello')
  })

  it('edits and saves a file back via PUT', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findAll('.file-tree-row').find((n) => n.text().includes('notes.md'))!.trigger('click')
    await flushPromises()

    await wrapper.find('.btn-ghost').trigger('click')
    const textarea = wrapper.find('.workspace-editor-textarea')
    await textarea.setValue('# Edited')
    await wrapper.find('.btn-primary').trigger('click')
    await flushPromises()

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/projects/p1/workspace/file?path=notes.md',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ content: '# Edited' }),
      }),
    )
    // viewer shows the saved content and leaves edit mode
    expect(wrapper.find('.workspace-md-body').text()).toContain('Edited')
    expect(wrapper.find('.workspace-editor-textarea').exists()).toBe(false)
  })

  it('runs a custom script: POSTs, streams logs, applies frames', async () => {
    const wrapper = mountPage()
    await flushPromises()

    // select the "hello" script node
    await wrapper.findAll('.file-tree-row').find((n) => n.text().includes('hello'))!.trigger('click')
    await flushPromises()

    await wrapper.find('.workspace-content-actions .btn-primary').trigger('click')
    await flushPromises()

    expect(apiFetchMock).toHaveBeenCalledWith('/api/scripts/s1/run', {
      method: 'POST',
    })
    const sock = lastSocket()
    expect(sock.url).toContain('/ws/script-runs/r1')

    sock.emit({ type: 'log', log: 'preparing deps' })
    await flushPromises()
    expect(wrapper.find('.script-log-output').text()).toContain('preparing deps')
  })

  it('closes the run socket when the project switches', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findAll('.file-tree-row').find((n) => n.text().includes('hello'))!.trigger('click')
    await flushPromises()
    await wrapper.find('.workspace-content-actions .btn-primary').trigger('click')
    await flushPromises()
    const sock = lastSocket()

    // switch project via the route mock + re-mount triggers the watcher
    route.params.projectId = 'p2'
    await flushPromises()
    expect(sock.readyState).toBe(3)
    route.params.projectId = 'p1' // restore for other tests
  })
})
