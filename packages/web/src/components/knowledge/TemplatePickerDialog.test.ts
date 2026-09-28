import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import TemplatePickerDialog from './TemplatePickerDialog.vue'
import type { KnowledgeTemplate } from '@/api/knowledge'

const listTemplates = vi.hoisted(() => vi.fn())
const copyTemplate = vi.hoisted(() => vi.fn())
vi.mock('@/api/knowledge', () => ({
  knowledgeApi: {
    listTemplates: (...args: unknown[]) => listTemplates(...args),
    copyTemplate: (...args: unknown[]) => copyTemplate(...args),
  },
}))

const triggerRefresh = vi.hoisted(() => vi.fn())
vi.mock('@/stores/knowledge', () => ({
  useKnowledgeStore: () => ({ triggerRefresh }),
}))

function tpl(overrides: Partial<KnowledgeTemplate> & { slug: string }): KnowledgeTemplate {
  const category = overrides.slug.split('/')[0] ?? 'domain'
  return {
    title: overrides.slug,
    category,
    summary: '',
    exists: false,
    ...overrides,
  }
}

describe('TemplatePickerDialog', () => {
  let wrapper: VueWrapper | null = null

  beforeEach(() => {
    listTemplates.mockReset()
    copyTemplate.mockReset()
    triggerRefresh.mockReset()
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    document.body.innerHTML = ''
    vi.useRealTimers()
  })

  function mountDialog(projectId = 'p-1') {
    wrapper = mount(TemplatePickerDialog, {
      props: { projectId },
      attachTo: document.body,
    })
    return wrapper
  }

  it('groups templates under their category headings', async () => {
    listTemplates.mockResolvedValue([
      tpl({ slug: 'technical/db-schema', title: 'DB Schema' }),
      tpl({ slug: 'domain/context', title: '项目背景' }),
      tpl({ slug: 'skills/test-patterns', title: 'Test Patterns' }),
    ])
    mountDialog()
    await vi.waitFor(() => {
      const headings = [...document.querySelectorAll('.picker-category-heading')].map((h) => h.textContent)
      expect(headings).toEqual(['知识域', '技能', '技术'])
    })
    expect(listTemplates).toHaveBeenCalledWith('p-1')
    expect(document.body.textContent).toContain('项目背景')
    // slug shown as the row's secondary line
    expect(document.body.textContent).toContain('domain/context')
  })

  it('copies a template, refreshes the store, and emits copied', async () => {
    listTemplates.mockResolvedValue([tpl({ slug: 'skills/test-patterns', title: 'Test Patterns' })])
    copyTemplate.mockResolvedValue({ slug: 'skills/test-patterns', created: true })
    mountDialog()

    const button = () =>
      [...document.querySelectorAll<HTMLButtonElement>('.template-copy-btn')]
        .find((b) => b.textContent?.includes('复制'))!
    await vi.waitFor(() => expect(button()).toBeTruthy())
    button().click()

    await vi.waitFor(() => {
      expect(copyTemplate).toHaveBeenCalledWith('p-1', 'skills/test-patterns')
    })
    expect(triggerRefresh).toHaveBeenCalledTimes(1)
    await vi.waitFor(() => {
      expect(wrapper!.emitted('copied')?.[0]).toEqual(['skills/test-patterns'])
    }, { timeout: 2000 })
  })

  it('disables copy and shows the exists badge when the project already has the template', async () => {
    listTemplates.mockResolvedValue([tpl({ slug: 'domain/context', exists: true })])
    mountDialog()

    await vi.waitFor(() => {
      expect(document.querySelector('.template-exists-badge')?.textContent).toContain('已存在')
    })
    const button = document.querySelector<HTMLButtonElement>('.template-copy-btn')!
    expect(button.disabled).toBe(true)
    button.click()
    expect(copyTemplate).not.toHaveBeenCalled()
    expect(wrapper!.emitted('copied')).toBeUndefined()
  })

  it('renders the API rejection message inline', async () => {
    listTemplates.mockResolvedValue([tpl({ slug: 'domain/context' })])
    copyTemplate.mockRejectedValue(new Error('boom'))
    mountDialog()

    await vi.waitFor(() => {
      const button = [...document.querySelectorAll<HTMLButtonElement>('.template-copy-btn')][0]
      expect(button).toBeTruthy()
      button.click()
    })
    await vi.waitFor(() => {
      expect(document.querySelector('.modal-error')?.textContent).toContain('boom')
    })
    expect(triggerRefresh).not.toHaveBeenCalled()
    expect(wrapper!.emitted('copied')).toBeUndefined()
  })

  it('shows a load error when the template list fails', async () => {
    listTemplates.mockRejectedValue(new Error('down'))
    mountDialog()
    await vi.waitFor(() => {
      expect(document.querySelector('.modal-error')?.textContent).toContain('模板列表加载失败')
    })
  })
})
