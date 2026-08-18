import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { resolveMentionAgent } from './mention'
import MentionPopup from './MentionPopup.vue'

describe('resolveMentionAgent', () => {
  const mentioned = [
    { slug: 'data-analyst', label: '数据分析专家' },
    { slug: 'quality-assurance', label: '测试专家' },
  ]

  it('routes to the agent whose @label survives in the text', () => {
    expect(resolveMentionAgent('@数据分析专家 看下这个指标', mentioned))
      .toEqual({ agentSlug: 'data-analyst' })
  })

  it('drops a mapping whose mention text was deleted', () => {
    expect(resolveMentionAgent('看下这个指标', mentioned))
      .toEqual({ agentSlug: undefined })
  })

  it('ignores hand-typed @names that never went through the popup', () => {
    expect(resolveMentionAgent('@随便打的 看下这个指标', mentioned))
      .toEqual({ agentSlug: undefined })
  })

  it('rejects two distinct mentioned agents', () => {
    expect(resolveMentionAgent('@数据分析专家 和 @测试专家 一起看', mentioned))
      .toEqual({ error: 'multiple' })
  })

  it('treats the same agent mentioned twice as one', () => {
    expect(resolveMentionAgent('@数据分析专家 先看，@数据分析专家 再确认', mentioned))
      .toEqual({ agentSlug: 'data-analyst' })
  })
})

describe('MentionPopup - agents only', () => {
  function mountPopup(excludeSlug?: string) {
    return mount(MentionPopup, { props: { excludeSlug } })
  }

  it('lists agents (excluding the current one) and emits the full item on select', async () => {
    const { setActivePinia, createPinia } = await import('pinia')
    const { useAgentStore } = await import('@/stores/agent')
    setActivePinia(createPinia())
    const store = useAgentStore()
    store.agents = [
      { slug: 'tech-lead', label: '技术负责人', agent_id: 'a1' },
      { slug: 'data-analyst', label: '数据分析专家', agent_id: 'a2' },
    ] as never

    const wrapper = mountPopup('tech-lead')
    const items = wrapper.findAll('.mention-item')
    expect(items.length).toBe(1)
    // Only the UI display name renders - the slug is routing detail.
    expect(items[0].text()).toContain('数据分析专家')
    expect(items[0].text()).not.toContain('data-analyst')

    await items[0].trigger('click')
    expect(wrapper.emitted('select')?.[0]?.[0]).toEqual({
      slug: 'data-analyst',
      label: '数据分析专家',
    })
  })

  it('filters by label via the search box', async () => {
    const { setActivePinia, createPinia } = await import('pinia')
    const { useAgentStore } = await import('@/stores/agent')
    setActivePinia(createPinia())
    const store = useAgentStore()
    store.agents = [
      { slug: 'tech-lead', label: '技术负责人', agent_id: 'a1' },
      { slug: 'data-analyst', label: '数据分析专家', agent_id: 'a2' },
    ] as never

    const wrapper = mountPopup()
    await wrapper.find('.mention-search').setValue('数据')
    const items = wrapper.findAll('.mention-item')
    expect(items.length).toBe(1)
    expect(items[0].text()).toContain('数据分析专家')
  })
})
