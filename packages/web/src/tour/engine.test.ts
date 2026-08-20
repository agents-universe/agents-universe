import { afterEach, describe, expect, it, vi } from 'vitest'
import { resolveRoute, waitForRoute, waitForSelector } from './engine'

afterEach(() => {
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('waitForSelector', () => {
  it('resolves when the element appears mid-poll', async () => {
    vi.useFakeTimers()
    const p = waitForSelector('.x', 2000)
    document.body.innerHTML = '<div class="x"></div>'
    vi.advanceTimersByTime(60)
    await expect(p).resolves.toBeInstanceOf(Element)
  })

  it('resolves null on timeout without throwing', async () => {
    vi.useFakeTimers()
    const p = waitForSelector('.never', 1000)
    vi.advanceTimersByTime(1100)
    await expect(p).resolves.toBeNull()
  })
})

describe('resolveRoute', () => {
  it('substitutes the project id', () => {
    expect(resolveRoute('/projects/{projectId}/chat', 'p-1')).toBe('/projects/p-1/chat')
  })

  it('returns null without a project id', () => {
    expect(resolveRoute('/projects/{projectId}/chat', null)).toBeNull()
  })
})

describe('waitForRoute', () => {
  it('returns immediately when the path already matches', async () => {
    await expect(waitForRoute('/b', () => '/b')).resolves.toBe(true)
  })

  it('waits until the path changes', async () => {
    vi.useFakeTimers()
    let path = '/a'
    const p = waitForRoute('/b', () => path)
    vi.advanceTimersByTime(50)
    path = '/b'
    vi.advanceTimersByTime(40)
    await expect(p).resolves.toBe(true)
  })

  it('resolves false on timeout', async () => {
    vi.useFakeTimers()
    const p = waitForRoute('/b', () => '/a', 1000)
    await vi.advanceTimersByTimeAsync(1100)
    await expect(p).resolves.toBe(false)
  })
})
