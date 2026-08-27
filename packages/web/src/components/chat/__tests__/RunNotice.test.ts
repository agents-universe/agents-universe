import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import RunNotice from '@/components/chat/RunNotice.vue'
import type { ConversationRun } from '@/types'

function makeRun(over: Partial<ConversationRun> = {}): ConversationRun {
  return {
    run_id: 'r1',
    status: 'interrupted',
    user_message_id: 'um-1',
    started_at: '2026-08-24T00:00:00Z',
    ended_at: null,
    error_message: null,
    streaming_snapshot: null,
    tokens_used: null,
    ...over,
  }
}

describe('RunNotice', () => {
  it('shows the interrupted label for interrupted runs', () => {
    const wrapper = mount(RunNotice, { props: { run: makeRun() } })
    expect(wrapper.find('.run-notice-label').text()).toBe('上次运行被中断。直接输入消息即可继续之前的任务。')
    expect(wrapper.classes()).not.toContain('run-notice-failed')
  })

  it('shows the failed label and failed styling for failed runs', () => {
    const wrapper = mount(RunNotice, { props: { run: makeRun({ status: 'failed' }) } })
    expect(wrapper.find('.run-notice-label').text()).toBe('上次运行失败。可直接输入消息重试。')
    expect(wrapper.classes()).toContain('run-notice-failed')
  })

  it('renders the recovered partial text from the snapshot', () => {
    const wrapper = mount(RunNotice, {
      props: { run: makeRun({ streaming_snapshot: 'partial answer...' }) },
    })
    const snapshot = wrapper.find('.run-notice-snapshot')
    expect(snapshot.exists()).toBe(true)
    expect(snapshot.text()).toBe('partial answer...')
  })

  it('hides the snapshot when the run has no recovered text', () => {
    const wrapper = mount(RunNotice, { props: { run: makeRun() } })
    expect(wrapper.find('.run-notice-snapshot').exists()).toBe(false)
  })

  it('never renders a rerun button - continuation is typing, not clicking', () => {
    const wrapper = mount(RunNotice, { props: { run: makeRun() } })
    expect(wrapper.find('button').exists()).toBe(false)
  })
})
