import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import RunNotice from '../RunNotice.vue'
import type { ConversationRun } from '@/types'

const base: ConversationRun = {
  run_id: 'r1',
  status: 'failed',
  user_message_id: null,
  started_at: '2026-08-25T12:00:00',
  ended_at: '2026-08-25T12:05:00',
  error_message: null,
  streaming_snapshot: null,
  tokens_used: null,
}

describe('RunNotice', () => {
  it('shows the failed run error message', () => {
    const wrapper = mount(RunNotice, {
      props: {
        run: { ...base, error_message: 'LLM API error: 404 model=deepseek-v4-flash' },
        canRerun: true,
      },
    })
    expect(wrapper.text()).toContain('LLM API error: 404')
  })

  it('shows the interrupted snapshot, not an error message, for interrupted runs', () => {
    const wrapper = mount(RunNotice, {
      props: {
        run: { ...base, status: 'interrupted', streaming_snapshot: 'partial text' },
        canRerun: false,
      },
    })
    expect(wrapper.text()).toContain('partial text')
  })

  it('omits the error text when the failed run has none', () => {
    const wrapper = mount(RunNotice, {
      props: { run: base, canRerun: false },
    })
    expect(wrapper.find('.run-notice-snapshot').exists()).toBe(false)
  })
})
