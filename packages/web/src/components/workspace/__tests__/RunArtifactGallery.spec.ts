import { describe, it, expect, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import RunArtifactGallery from '@/components/workspace/RunArtifactGallery.vue'
import type { ArtifactEntry } from '@/api/scripts'

// Artifact URLs go through withApi(); identity keeps the assertions about the
// path (which is the part that must be right) readable.
vi.mock('@/utils/basePath', () => ({
  apiBase: '',
  withApi: (path: string) => path,
}))

const SHOT: ArtifactEntry = {
  name: 'screenshot',
  rel_path: 'test-results/login-fails/test-failed-1.png',
  kind: 'screenshot',
  mime: 'image/png',
  size_bytes: 2048,
  test_title: 'rejects a bad password',
}
const VIDEO: ArtifactEntry = {
  name: 'video',
  rel_path: 'test-results/login-fails/video.webm',
  kind: 'video',
  mime: 'video/webm',
  size_bytes: 400_000,
  test_title: 'rejects a bad password',
}
const TRACE: ArtifactEntry = {
  name: 'trace',
  rel_path: 'test-results/login-fails/trace.zip',
  kind: 'trace',
  mime: 'application/zip',
  size_bytes: 4096,
  test_title: 'rejects a bad password',
}
const REPORT: ArtifactEntry = {
  name: 'playwright-report',
  rel_path: 'html-report/index.html',
  kind: 'report',
  mime: 'text/html',
  size_bytes: 128,
  test_title: '',
}

function mountGallery(props: Record<string, unknown>) {
  return mount(RunArtifactGallery, {
    props: { runId: 'r1', artifacts: [], reportUrl: null, ...props },
  })
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('RunArtifactGallery', () => {
  it('renders screenshots, videos and downloads under the run', () => {
    const wrapper = mountGallery({ artifacts: [SHOT, VIDEO, TRACE] })

    expect(wrapper.find('.run-shot img').attributes('src')).toBe(
      '/api/scripts/runs/r1/artifacts/test-results/login-fails/test-failed-1.png',
    )
    expect(wrapper.find('.run-shot figcaption').text()).toBe('rejects a bad password')
    expect(wrapper.find('.run-video video').attributes('src')).toBe(
      '/api/scripts/runs/r1/artifacts/test-results/login-fails/video.webm',
    )
    expect(wrapper.find('.run-artifact-file').attributes('href')).toBe(
      '/api/scripts/runs/r1/artifacts/test-results/login-fails/trace.zip',
    )
    expect(wrapper.find('.run-artifact-file').attributes('download')).toBe('trace')
  })

  it('opens the report through the sandboxed URL, never as a download', async () => {
    const open = vi.fn()
    vi.stubGlobal('open', open)
    const wrapper = mountGallery({
      artifacts: [SHOT, REPORT],
      reportUrl: '/api/scripts/runs/r1/report/tok/index.html',
    })

    // The manifest entry for the report is not a file chip.
    expect(wrapper.findAll('.run-artifact-file')).toHaveLength(0)

    await wrapper.find('.run-report-btn').trigger('click')
    expect(open).toHaveBeenCalledWith(
      '/api/scripts/runs/r1/report/tok/index.html',
      '_blank',
      'noopener,noreferrer',
    )
    vi.unstubAllGlobals()
  })

  it('stays out of the panel when the run produced nothing', () => {
    expect(mountGallery({}).find('.run-artifacts').exists()).toBe(false)
    // A report on its own is still worth a button.
    expect(
      mountGallery({ reportUrl: '/api/scripts/runs/r1/report/tok/index.html' })
        .find('.run-report-btn').exists(),
    ).toBe(true)
  })
})
