import { describe, expect, it } from 'vitest'
import { computeTooltipPosition, type Rect } from './position'

const viewport = { width: 1440, height: 900 }
const tooltip = { width: 340, height: 180 }

describe('computeTooltipPosition', () => {
  it('places the tooltip below the anchor when there is room', () => {
    const pos = computeTooltipPosition({ top: 100, left: 200, right: 500, bottom: 200 }, viewport, tooltip)
    expect(pos.placement).toBe('bottom')
    expect(pos.top).toBe(212) // anchor.bottom + 12 gap
    expect(pos.left).toBe(200)
  })

  it('flips above the anchor when there is no room below but room above', () => {
    const pos = computeTooltipPosition({ top: 600, left: 200, right: 500, bottom: 860 }, viewport, tooltip)
    expect(pos.placement).toBe('top')
    expect(pos.top + tooltip.height).toBe(588) // anchor.top - 12 gap
  })

  it('clamps a tall anchor into the viewport instead of flipping off-screen', () => {
    // messages-list / right-panel / full-page anchors: no room above or below
    const anchor: Rect = { top: 48, left: 300, right: 1100, bottom: 760 }
    const pos = computeTooltipPosition(anchor, viewport, tooltip)
    expect(pos.top).toBeGreaterThanOrEqual(8)
    expect(pos.top + tooltip.height).toBeLessThanOrEqual(viewport.height - 8)
  })

  it('flips a near-top anchor that prefers top back down', () => {
    const pos = computeTooltipPosition({ top: 20, left: 200, right: 500, bottom: 120 }, viewport, tooltip, 'top')
    expect(pos.placement).toBe('bottom')
    expect(pos.top).toBe(132)
  })

  it('flips right/left placements when there is no room on the preferred side', () => {
    const right = computeTooltipPosition({ top: 100, left: 1200, right: 1400, bottom: 200 }, viewport, tooltip, 'right')
    expect(right.placement).toBe('left')
    expect(right.left + tooltip.width).toBe(1188) // anchor.left - 12 gap

    const left = computeTooltipPosition({ top: 100, left: 40, right: 240, bottom: 200 }, viewport, tooltip, 'left')
    expect(left.placement).toBe('right')
    expect(left.left).toBe(252)
  })

  it('never leaves the viewport, whatever the anchor and sizes', () => {
    for (const anchor of [
      { top: -200, left: -100, right: 200, bottom: 0 },
      { top: 0, left: 0, right: 1440, bottom: 900 }, // full viewport
      { top: 700, left: 1300, right: 1500, bottom: 950 }, // bottom-right corner, overflowing
      { top: 400, left: -50, right: 300, bottom: 500 },
    ] as Rect[]) {
      for (const preferred of ['top', 'bottom', 'left', 'right'] as const) {
        const pos = computeTooltipPosition(anchor, viewport, tooltip, preferred)
        expect(pos.left).toBeGreaterThanOrEqual(8)
        expect(pos.left + tooltip.width).toBeLessThanOrEqual(viewport.width - 8)
        expect(pos.top).toBeGreaterThanOrEqual(8)
        expect(pos.top + tooltip.height).toBeLessThanOrEqual(viewport.height - 8)
      }
    }
  })
})
