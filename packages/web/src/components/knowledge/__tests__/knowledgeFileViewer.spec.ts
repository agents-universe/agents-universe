/**
 * Navigation races in the knowledge viewer.
 *
 * Clicking a child starts an async load; the panel is pushed only when the
 * response arrives. If the user goes back (panel back button, breadcrumb,
 * Escape) while that request is in flight, the late response used to push the
 * panel they had just left back on top — the monotonic guard covered
 * navigateTo's own callers only, not the navigation that cancelled it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import KnowledgeFileViewer from '../KnowledgeFileViewer.vue'
import { useProjectStore } from '@/stores/project'
import type { Project } from '@/types'

const knowledgeMock = vi.hoisted(() => ({
  getFile: vi.fn(),
  getChildren: vi.fn(),
  getAncestors: vi.fn(),
  deleteFile: vi.fn(),
}))

vi.mock('@/api/knowledge', () => ({ knowledgeApi: knowledgeMock }))

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

interface EntryFixture {
  children: Array<{ slug: string; title: string }>
  ancestors: Array<{ slug: string; title: string }>
}

/** Per-slug children/ancestors; getFile resolves with a trivial document. */
const fixtures: Record<string, EntryFixture> = {
  root: { children: [{ slug: 'a', title: 'A' }], ancestors: [] },
  a: {
    children: [{ slug: 'b', title: 'B' }, { slug: 'c', title: 'C' }],
    ancestors: [{ slug: 'root', title: 'Root' }],
  },
  b: { children: [], ancestors: [{ slug: 'root', title: 'Root' }, { slug: 'a', title: 'A' }] },
  c: { children: [{ slug: 'd', title: 'D' }], ancestors: [{ slug: 'root', title: 'Root' }, { slug: 'a', title: 'A' }] },
  d: { children: [], ancestors: [] },
}

function detail(slug: string) {
  return {
    slug,
    title: slug.toUpperCase(),
    content: `# ${slug}`,
    tags: [],
    cross_references: [],
    parent_slug: null,
    children_slugs: [],
    depth: 0,
  }
}

function childrenOf(slug: string) {
  return (fixtures[slug]?.children ?? []).map((child) => ({
    slug: child.slug,
    title: child.title,
    summary: '',
    has_children: (fixtures[child.slug]?.children.length ?? 0) > 0,
    depth: 1,
  }))
}

async function mountViewer(slug = 'root'): Promise<VueWrapper> {
  setActivePinia(createPinia())
  useProjectStore().currentProject = { project_id: 'p-1' } as Project
  const wrapper = mount(KnowledgeFileViewer, {
    props: { slug },
    global: { stubs: { teleport: true } },
  })
  await flushPromises()
  return wrapper
}

/** The last (top) panel — where back buttons and breadcrumbs are active. */
function topPanel(wrapper: VueWrapper) {
  const panels = wrapper.findAll('.knowledge-viewer-panel')
  return panels[panels.length - 1]
}

describe('KnowledgeFileViewer navigation guards', () => {
  beforeEach(() => {
    knowledgeMock.getFile.mockReset()
    knowledgeMock.getChildren.mockReset()
    knowledgeMock.getAncestors.mockReset()
    knowledgeMock.getFile.mockImplementation(async (_pid: string, slug: string) => detail(slug))
    knowledgeMock.getChildren.mockImplementation(async (_pid: string, slug: string) => childrenOf(slug))
    knowledgeMock.getAncestors.mockImplementation(async (_pid: string, slug: string) => fixtures[slug]?.ancestors ?? [])
  })

  it('a slow child load does not reopen a panel after Back', async () => {
    const wrapper = await mountViewer()
    expect(wrapper.findAll('.knowledge-viewer-panel')).toHaveLength(1)

    // Open A, so the stack is [root, a] and a back button exists.
    await topPanel(wrapper).find('.knowledge-child-item').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.knowledge-viewer-panel')).toHaveLength(2)

    // Child B's load stays pending while the user backs out of A.
    const late = deferred<ReturnType<typeof detail>>()
    knowledgeMock.getFile.mockImplementation((_pid: string, slug: string) =>
      slug === 'b' ? late.promise : Promise.resolve(detail(slug))
    )
    const bItem = topPanel(wrapper)
      .findAll('.knowledge-child-item')
      .find((item) => item.text().includes('B'))!
    await bItem.trigger('click')

    await topPanel(wrapper).find('.panel-back-btn').trigger('click')
    expect(wrapper.findAll('.knowledge-viewer-panel')).toHaveLength(1)

    late.resolve(detail('b'))
    await flushPromises()

    // The user is still on the root panel: the stale response was dropped.
    expect(wrapper.findAll('.knowledge-viewer-panel')).toHaveLength(1)
    expect(wrapper.find('.modal-title').text()).toBe('ROOT')
  })

  it('a slow child load does not reopen a panel after jumping to an ancestor breadcrumb', async () => {
    const wrapper = await mountViewer()
    // root → A → C, so C's breadcrumb offers "Root" and "A".
    await topPanel(wrapper).find('.knowledge-child-item').trigger('click')
    await flushPromises()
    await topPanel(wrapper)
      .findAll('.knowledge-child-item')
      .find((item) => item.text().includes('C'))!
      .trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.knowledge-viewer-panel')).toHaveLength(3)

    // Child D's load stays pending while the user jumps back to A.
    const late = deferred<ReturnType<typeof detail>>()
    knowledgeMock.getFile.mockImplementation((_pid: string, slug: string) =>
      slug === 'd' ? late.promise : Promise.resolve(detail(slug))
    )
    await topPanel(wrapper)
      .findAll('.knowledge-child-item')
      .find((item) => item.text().includes('D'))!
      .trigger('click')

    const ancestorLinks = topPanel(wrapper).findAll('.breadcrumb-link')
    await ancestorLinks[ancestorLinks.length - 1].trigger('click')
    expect(wrapper.findAll('.knowledge-viewer-panel')).toHaveLength(2)

    late.resolve(detail('d'))
    await flushPromises()

    expect(wrapper.findAll('.knowledge-viewer-panel')).toHaveLength(2)
  })
})
