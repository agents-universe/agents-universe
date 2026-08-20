<template>
  <Teleport to="body">
    <div v-if="tour.isActive" class="tour-layer" :class="{ 'tour-layer--dim': isCenterStep }">
      <template v-if="!isCenterStep">
        <!-- Four backdrop segments around the hole; block clicks outside it -->
        <div v-if="hole" class="tour-backdrop" :style="segmentStyle('top')" />
        <div v-if="hole" class="tour-backdrop" :style="segmentStyle('bottom')" />
        <div v-if="hole" class="tour-backdrop" :style="segmentStyle('left')" />
        <div v-if="hole" class="tour-backdrop" :style="segmentStyle('right')" />
        <!-- The hole itself lets clicks pass through to the anchored element -->
        <div v-if="hole" class="tour-hole" :style="holeStyle()" />
        <div v-if="hole" ref="tooltipEl" class="tour-tooltip" :style="tooltipStyle()">
          <TourTooltipCard :step="step!" />
        </div>
        <!-- Target vanished (e.g. the create-project dialog closed) - keep the tooltip reachable -->
        <div v-else class="tour-tooltip tour-tooltip--center">
          <TourTooltipCard :step="step!" />
        </div>
      </template>
      <div v-else class="tour-center-card">
        <TourTooltipCard :step="step!" />
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useTourStore } from '@/stores/tour'
import TourTooltipCard from './TourTooltipCard.vue'
import { computeTooltipPosition, type Placement, type Rect } from '@/tour/position'
import type { TourStep } from '@/tour/steps'

const tour = useTourStore()

const PADDING = 4 // highlight border inset around the anchor
// Fallbacks for the first frame, before the rendered card can be measured
const TOOLTIP_W = 340
const TOOLTIP_H_EST = 170

const hole = ref<DOMRect | null>(null)
const tooltipEl = ref<HTMLElement | null>(null)
const tooltipPos = ref<{ left: number; top: number } | null>(null)

const step = computed<TourStep | null>(() => tour.currentStep())
const isCenterStep = computed(() => step.value?.center ?? false)

/* ── measurement ─────────────────────────────────────────────── */
function measure() {
  const s = step.value
  if (!s || s.center) {
    hole.value = null
    tooltipPos.value = null
    return
  }
  const el = document.querySelector(s.target ?? '')
  if (!el) {
    hole.value = null
    tooltipPos.value = null
    return
  }
  ensureVisible(el)
  hole.value = el.getBoundingClientRect()
  positionTooltip()
}

/** Keep the anchor within the viewport before measuring. */
function ensureVisible(el: Element) {
  const r = el.getBoundingClientRect()
  const vw = window.innerWidth
  const vh = window.innerHeight
  if (r.top < 0 || r.bottom > vh || r.left < 0 || r.right > vw) {
    el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'smooth' })
    // smooth scroll settles asynchronously - re-measure after it lands.
    window.setTimeout(measure, 350)
  }
}

function onScrollOrResize() {
  measure()
}

/* ── geometry ────────────────────────────────────────────────── */
function segmentStyle(side: 'top' | 'bottom' | 'left' | 'right') {
  const r = hole.value
  if (!r) return {}
  const vw = window.innerWidth
  const vh = window.innerHeight
  const common = { top: 0, bottom: 0, left: 0, right: 0 }
  switch (side) {
    case 'top':
      return { ...common, bottom: 'auto', height: `${Math.max(0, r.top - PADDING)}px` }
    case 'bottom':
      return { ...common, top: 'auto', height: `${Math.max(0, vh - r.bottom - PADDING)}px` }
    case 'left':
      return {
        ...common,
        right: 'auto',
        width: `${Math.max(0, r.left - PADDING)}px`,
        top: `${Math.max(0, r.top - PADDING)}px`,
        height: `${r.height + 2 * PADDING}px`,
      }
    case 'right':
      return {
        ...common,
        left: 'auto',
        width: `${Math.max(0, vw - r.right - PADDING)}px`,
        top: `${Math.max(0, r.top - PADDING)}px`,
        height: `${r.height + 2 * PADDING}px`,
      }
  }
}

function holeStyle() {
  const r = hole.value
  if (!r) return {}
  const el = document.querySelector(step.value?.target ?? '')
  const radius = el ? getComputedStyle(el).borderRadius : '8px'
  return {
    top: `${r.top - PADDING}px`,
    left: `${r.left - PADDING}px`,
    width: `${r.width + 2 * PADDING}px`,
    height: `${r.height + 2 * PADDING}px`,
    borderRadius: radius,
  }
}

/** Position the tooltip from the measured anchor rect and the card's real size. */
function positionTooltip() {
  const r = hole.value
  if (!r) return
  // Called post-render, so the card size is real; the estimates only cover
  // the first frame of a center -> anchored transition, where the card has
  // not rendered yet - re-run once it exists.
  const hasCard = tooltipEl.value != null
  const size = {
    width: tooltipEl.value?.offsetWidth || TOOLTIP_W,
    height: tooltipEl.value?.offsetHeight || TOOLTIP_H_EST,
  }
  const anchor: Rect = { top: r.top, left: r.left, right: r.right, bottom: r.bottom }
  const pos = computeTooltipPosition(
    anchor,
    { width: window.innerWidth, height: window.innerHeight },
    size,
    (step.value?.placement ?? 'bottom') as Placement,
  )
  tooltipPos.value = { left: pos.left, top: pos.top }
  if (!hasCard) nextTick(positionTooltip)
}

function tooltipStyle() {
  const pos = tooltipPos.value
  if (!pos) return { left: '50%', top: '50%', transform: 'translate(-50%, -50%)' }
  return { left: `${pos.left}px`, top: `${pos.top}px` }
}

/* ── lifecycle ───────────────────────────────────────────────── */
// Post-flush: the new step's card content is in the DOM, so its measured
// height reflects the text actually being shown.
watch(() => tour.stepIndex, measure, { flush: 'post' })
watch(() => tour.isActive, measure, { flush: 'post' })

onMounted(() => {
  measure()
  window.addEventListener('scroll', onScrollOrResize, { capture: true, passive: true })
  window.addEventListener('resize', onScrollOrResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('scroll', onScrollOrResize, { capture: true })
  window.removeEventListener('resize', onScrollOrResize)
})
</script>
