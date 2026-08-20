/**
 * Tooltip layout for the tour spotlight.
 *
 * Pure geometry: TourSpotlight feeds in the anchor rect, the viewport and
 * the measured tooltip size, and gets back viewport-safe coordinates.
 * Sides are chosen by fit, and the final clamp is what guarantees the card
 * never leaves the viewport - for anchors too tall (or wide) for any side
 * (messages list, right panels, full pages) the tooltip overlaps the
 * anchor instead of disappearing off-screen.
 */

const GAP = 12 // tooltip <-> anchor gap
const MARGIN = 8 // viewport edge clearance

export interface Rect {
  top: number
  left: number
  right: number
  bottom: number
}

export type Placement = 'top' | 'bottom' | 'left' | 'right'

const OPPOSITE: Record<Placement, Placement> = {
  top: 'bottom',
  bottom: 'top',
  left: 'right',
  right: 'left',
}

const ALL_SIDES: Placement[] = ['bottom', 'top', 'right', 'left']

export function clamp(v: number, min: number, max: number): number {
  return Math.min(Math.max(v, min), Math.max(min, max))
}

export function computeTooltipPosition(
  anchor: Rect,
  viewport: { width: number; height: number },
  tooltip: { width: number; height: number },
  preferred: Placement = 'bottom',
): { left: number; top: number; placement: Placement } {
  const { width: vw, height: vh } = viewport
  const fits: Record<Placement, boolean> = {
    bottom: anchor.bottom + GAP + tooltip.height <= vh - MARGIN,
    top: anchor.top - GAP - tooltip.height >= MARGIN,
    right: anchor.right + GAP + tooltip.width <= vw - MARGIN,
    left: anchor.left - GAP - tooltip.width >= MARGIN,
  }

  // Preferred side when it fits, the opposite side next, then any side that
  // fits. If nothing fits the preferred side survives and the final clamp
  // keeps the card on screen, overlapping the anchor.
  let placement = preferred
  if (!fits[placement]) {
    placement = fits[OPPOSITE[preferred]]
      ? OPPOSITE[preferred]
      : ALL_SIDES.find((side) => fits[side]) ?? preferred
  }

  // Top-left corner of the tooltip for each placement.
  let left: number
  let top: number
  switch (placement) {
    case 'top':
      top = anchor.top - GAP - tooltip.height
      left = anchor.left
      break
    case 'right':
      top = anchor.top
      left = anchor.right + GAP
      break
    case 'left':
      top = anchor.top
      left = anchor.left - GAP - tooltip.width
      break
    default:
      top = anchor.bottom + GAP
      left = anchor.left
  }

  return {
    left: clamp(left, MARGIN, vw - MARGIN - tooltip.width),
    top: clamp(top, MARGIN, vh - MARGIN - tooltip.height),
    placement,
  }
}
