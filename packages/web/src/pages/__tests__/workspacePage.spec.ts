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
vi.mock('@/utils/basePath', () => ({
  apiBase: '',
  // Artifact and report URLs are built with withApi(); identity keeps the
  // assertions readable while still exercising the call.
  withApi: (path: string) => path,
}))

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
  emitClose() { this.onclose?.({}) }
}

function lastSocket(): FakeWebSocket {
  const ws = FakeWebSocket.instances.at(-1)
  if (!ws) throw new Error('no WebSocket created')
  return ws
}

/** A finished run as the history endpoint projects it (no result, no log). */
function runRow(overrides: Record<string, unknown> = {}) {
  return {
    run_id: 'r0',
    script_id: 's1',
    spec_slug: 'proj-1',
    status: 'completed',
    exit_code: 1,
    triggered_by: 'u1',
    started_at: '2026-09-15T10:00:00+00:00',
    completed_at: '2026-09-15T10:00:12+00:00',
    created_at: '2026-09-15T10:00:00+00:00',
    summary: {
      status: 'failed',
      source: 'json',
      partial: false,
      counts: { total: 2, passed: 1, failed: 1, flaky: 0, skipped: 0 },
      duration_ms: 12400,
      failed_count: 1,
    },
    ...overrides,
  }
}

/** The run-detail payload: verdict, artifacts and the sandboxed report URL. */
function runDetail(runId = 'r1') {
  return {
    ...runRow({ run_id: runId }),
    result: {
      source: 'json',
      partial: false,
      status: 'failed',
      counts: { total: 2, passed: 1, failed: 1, flaky: 0, skipped: 0 },
      duration_ms: 12400,
      failed_tests: [
        {
          title: 'rejects a bad password',
          file: 'tests/generated/proj-1.spec.ts',
          line: 12,
          error: 'Error: expected 200, got 401',
        },
      ],
      tests: [],
      truncated: false,
    },
    artifacts: [
      {
        name: 'screenshot',
        rel_path: 'test-results/login-fails/test-failed-1.png',
        kind: 'screenshot',
        mime: 'image/png',
        size_bytes: 2048,
        test_title: 'rejects a bad password',
      },
      {
        name: 'trace',
        rel_path: 'trace.zip',
        kind: 'trace',
        mime: 'application/zip',
        size_bytes: 4096,
        test_title: 'rejects a bad password',
      },
      {
        name: 'playwright-report',
        rel_path: 'html-report/index.html',
        kind: 'report',
        mime: 'text/html',
        size_bytes: 100,
        test_title: '',
      },
    ],
    artifacts_truncated: false,
    report_url: `/api/scripts/runs/${runId}/report/tok/index.html`,
    log: 'Running 2 tests\n',
  }
}

/** Route apiFetch by URL: workspace files list, file read/write, scripts. */
function defaultApi() {
  apiFetchMock.mockImplementation(async (url: string, options?: RequestInit) => {
    // Matched before the generic /run rule below - "/runs" contains "/run",
    // and a catch-all answer would hand the card a run without a result.
    if (url.startsWith('/api/scripts/runs/')) return runDetail(url.split('/')[4])
    if (url.endsWith('/runs')) return [runRow()]
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
      return { path: 'notes.md', content: '# Hello\n\nSee [[global-rules]]', size_bytes: 9 }
    }
    if (url.startsWith('/api/projects/p1/scripts')) {
      return [{ script_id: 's1', name: 'hello', script_type: 'python' }]
    }
    if (url.startsWith('/api/projects/p1/playwright/specs') && !url.includes('/run')) {
      return [{ slug: 'proj-1', title: 'Login flow', file: 'tests/generated/proj-1.spec.ts' }]
    }
    if (url.startsWith('/api/projects/p1/knowledge')) {
      if (options?.method === 'PUT') return { slug: 'global-rules', updated: true }
      return {
        slug: 'global-rules',
        title: 'Global rules',
        content: '# Global\nbody',
        tags: [],
        cross_references: [],
        parent_slug: null,
        children_slugs: [],
        depth: 0,
      }
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
    // Exact pathname, not toContain: /api/ws/... regressions must fail — only
    // /ws/* is upgraded by the vite dev proxy and nginx.
    expect(new URL(sock.url).pathname).toBe('/ws/script-runs/r1')

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

  it('does not report connection lost after a done frame + server close', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findAll('.file-tree-row').find((n) => n.text().includes('hello'))!.trigger('click')
    await flushPromises()
    await wrapper.find('.workspace-content-actions .btn-primary').trigger('click')
    await flushPromises()
    const sock = lastSocket()

    // Server sends the authoritative done frame, then closes the socket.
    sock.emit({ type: 'done', status: 'completed', exit_code: 0 })
    await flushPromises()
    sock.emitClose()
    await flushPromises()

    const out = wrapper.find('.script-log-output').text()
    expect(out).toContain('completed')
    // The normal close after done must NOT show the spurious lost-connection error.
    expect(out).not.toContain('连接中断')
  })

  it('reports connection lost only when the socket closes without a done frame', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findAll('.file-tree-row').find((n) => n.text().includes('hello'))!.trigger('click')
    await flushPromises()
    await wrapper.find('.workspace-content-actions .btn-primary').trigger('click')
    await flushPromises()
    const sock = lastSocket()

    sock.emitClose()
    await flushPromises()
    expect(wrapper.find('.script-log-output').text()).toContain('连接中断')
  })

  it('injects APP_BASE_URL from the Playwright base-URL input', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findAll('.file-tree-row').find((n) => n.text().includes('Login flow'))!.trigger('click')
    await flushPromises()

    const input = wrapper.find('.workspace-content-actions .executor-base-url')
    expect(input.exists()).toBe(true)
    await input.setValue('https://demo.example.com')
    await wrapper.find('.workspace-content-actions .btn-primary').trigger('click')
    await flushPromises()

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/projects/p1/playwright/specs/proj-1/run',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ env: { APP_BASE_URL: 'https://demo.example.com' } }),
      }),
    )
  })

  it('shows the verdict and artifacts once a run finishes', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findAll('.file-tree-row').find((n) => n.text().includes('Login flow'))!.trigger('click')
    await flushPromises()
    await wrapper.find('.workspace-content-actions .btn-primary').trigger('click')
    await flushPromises()

    const sock = lastSocket()
    sock.emit({ type: 'done', status: 'failed', exit_code: 1 })
    await flushPromises()

    // The done frame carries no verdict, so the card is built from the detail
    // fetch the done handler kicks off.
    expect(apiFetchMock).toHaveBeenCalledWith('/api/scripts/runs/r1')
    const card = wrapper.find('.run-result-card')
    expect(card.classes()).toContain('failed')
    expect(card.find('.run-result-counts').text()).toContain('1 失败')
    expect(card.find('.run-failure-title').text()).toBe('rejects a bad password')
    expect(card.find('.run-failure-location').text()).toContain('tests/generated/proj-1.spec.ts:12')

    expect(wrapper.find('.run-shot img').attributes('src')).toBe(
      '/api/scripts/runs/r1/artifacts/test-results/login-fails/test-failed-1.png',
    )
    // The trace downloads; the report entry is only reachable sandboxed.
    expect(wrapper.find('.run-artifact-file').attributes('href')).toBe(
      '/api/scripts/runs/r1/artifacts/trace.zip',
    )
    expect(wrapper.find('.run-report-btn').exists()).toBe(true)
    expect(wrapper.find('.run-artifacts').text()).not.toContain('playwright-report')
  })

  it('reopens a past run from the history list', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findAll('.file-tree-row').find((n) => n.text().includes('Login flow'))!.trigger('click')
    await flushPromises()

    // Selecting the spec lists its runs - scoped by slug, since every spec
    // shares the project's hidden anchor script.
    expect(apiFetchMock).toHaveBeenCalledWith('/api/projects/p1/playwright/specs/proj-1/runs')
    const row = wrapper.find('.run-history-row')
    expect(row.exists()).toBe(true)

    await row.trigger('click')
    await flushPromises()

    expect(apiFetchMock).toHaveBeenCalledWith('/api/scripts/runs/r0')
    expect(wrapper.find('.run-result-card').exists()).toBe(true)
    // Replaying a finished run goes through the same log socket.
    expect(new URL(lastSocket().url).pathname).toBe('/ws/script-runs/r0')
  })

  it('drops the run card when the project switches', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findAll('.file-tree-row').find((n) => n.text().includes('Login flow'))!.trigger('click')
    await flushPromises()
    await wrapper.find('.workspace-content-actions .btn-primary').trigger('click')
    await flushPromises()
    lastSocket().emit({ type: 'done', status: 'failed', exit_code: 1 })
    await flushPromises()
    expect(wrapper.find('.run-result-card').exists()).toBe(true)

    route.params.projectId = 'p2'
    await flushPromises()

    // The other project's run must not linger on screen.
    expect(wrapper.find('.run-result-card').exists()).toBe(false)
    route.params.projectId = 'p1' // restore for other tests
  })

  it('opens a knowledge cross-reference through the knowledge API', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findAll('.file-tree-row').find((n) => n.text().includes('notes.md'))!.trigger('click')
    await flushPromises()

    const link = wrapper.find('.workspace-md-body a.knowledge-link')
    expect(link.exists()).toBe(true)
    await link.trigger('click')
    await flushPromises()

    expect(apiFetchMock).toHaveBeenCalledWith('/api/projects/p1/knowledge/global-rules')
    expect(wrapper.find('.workspace-md-body').text()).toContain('Global')
  })
})
