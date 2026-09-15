import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import RunResultCard from '@/components/workspace/RunResultCard.vue'
import type { RunResult } from '@/api/scripts'

function result(overrides: Partial<RunResult> = {}): RunResult {
  return {
    source: 'json',
    partial: false,
    status: 'failed',
    counts: { total: 3, passed: 2, failed: 1, flaky: 0, skipped: 0 },
    duration_ms: 12400,
    failed_tests: [
      {
        title: 'rejects a bad password',
        file: 'tests/generated/login.spec.ts',
        line: 12,
        error: 'Error: expected 200, got 401\n    at login.spec.ts:12',
      },
    ],
    tests: [],
    truncated: false,
    ...overrides,
  }
}

function mountCard(props: Record<string, unknown>) {
  return mount(RunResultCard, { props: { status: 'failed', result: null, exitCode: null, ...props } })
}

describe('RunResultCard', () => {
  it('shows the verdict, counts, duration, exit code and failed tests', () => {
    const wrapper = mountCard({ result: result(), exitCode: 1 })

    expect(wrapper.classes()).toContain('failed')
    expect(wrapper.find('.run-result-badge').text()).toBe('失败')
    expect(wrapper.find('.run-result-counts').text()).toContain('2 通过')
    expect(wrapper.find('.run-result-counts').text()).toContain('1 失败')
    expect(wrapper.text()).toContain('耗时')
    expect(wrapper.text()).toContain('12.4s')
    expect(wrapper.text()).toContain('退出码 1')
    expect(wrapper.find('.run-failure-title').text()).toBe('rejects a bad password')
    expect(wrapper.find('.run-failure-location').text()).toBe('tests/generated/login.spec.ts:12')
    // Only the first line of a Playwright error - the rest is a stack trace.
    expect(wrapper.find('.run-failure-error').text()).toBe('Error: expected 200, got 401')
  })

  it('flags a log-parsed verdict as best-effort', () => {
    const wrapper = mountCard({ result: result({ source: 'log', partial: true }) })

    expect(wrapper.find('.run-result-hint').text()).toContain('可能不完整')
  })

  it('reports a timeout as a failure, not a pass', () => {
    const wrapper = mountCard({
      status: 'timed_out',
      result: result({ status: 'timed_out', counts: { total: 1, passed: 0, failed: 1, flaky: 0, skipped: 0 } }),
    })

    expect(wrapper.classes()).toContain('failed')
    expect(wrapper.find('.run-result-badge').text()).toBe('超时')
  })

  it('renders a green card from the row status when no result was parsed', () => {
    const wrapper = mountCard({ status: 'completed', exitCode: 0 })

    expect(wrapper.classes()).toContain('passed')
    expect(wrapper.find('.run-result-badge').text()).toBe('通过')
    expect(wrapper.find('.run-result-counts').exists()).toBe(false)
    expect(wrapper.find('.run-result-failures').exists()).toBe(false)
  })

  it('derives the duration from the timestamps when the result has none', () => {
    const wrapper = mountCard({
      status: 'completed',
      result: result({ status: 'passed', counts: { total: 1, passed: 1, failed: 0, flaky: 0, skipped: 0 }, duration_ms: 0, failed_tests: [] }),
      startedAt: '2026-09-15T10:00:00+00:00',
      completedAt: '2026-09-15T10:00:02+00:00',
    })

    expect(wrapper.text()).toContain('2.0s')
  })
})
