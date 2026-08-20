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
        <div v-if="hole" class="tour-tooltip" :style="tooltipStyle()">
          <TourTooltipCard :step="step!" />
        </div>
        <!-- Target vanished (e.g. the create-project dialog closed) — keep the tooltip reachable -->
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
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useTourStore } from '@/stores/tour'
import TourTooltipCard from './TourTooltipCard.vue'
import type { TourStep } from '@/tour/steps'

const tour = useTourStore()

const PADDING = 4 // highlight border inset around the anchor
const GAP = 12 // tooltip ↔ anchor gap
const TOOLTIP_W = 340 // used for viewport clamping (max-width 360 minus padding)
const TOOLTIP_H_EST = 170 // left/right vertical clamp estimate

const hole = ref<DOMRect | null>(null)

const step = computed<TourStep | null>(() => tour.currentStep())
const isCenterStep = computed(() => step.value?.center ?? false)

/* ── measurement ─────────────────────────────────────────────── */
function measure() {
  const s = step.value
  if (!s || s.center) {
    hole.value = null
    return
  }
  const el = document.querySelector(s.target ?? '')
  if (!el) {
    hole.value = null
    return
  }
  ensureVisible(el)
  hole.value = el.getBoundingClientRect()
}

/** Keep the anchor within the viewport before measuring. */
function ensureVisible(el: Element) {
  const r = el.getBoundingClientRect()
  const vw = window.innerWidth
  const vh = window.innerHeight
  if (r.top < 0 || r.bottom > vh || r.left < 0 || r.right > vw) {
    el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'smooth' })
    // smooth scroll settles asynchronously — re-measure after it lands.
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

function clamp(v: number, min: number, max: number) {
  return Math.min(Math.max(v, min), Math.max(min, max))
}

function tooltipStyle() {
  const r = hole.value
  const vw = window.innerWidth
  const vh = window.innerHeight
  const pref = step.value?.placement ?? 'bottom'
  if (!r) return { left: '50%', top: '50%', transform: 'translate(-50%, -50%)' }

  let placement = pref
  if (placement === 'bottom' && r.bottom + GAP + TOOLTIP_H_EST > vh - 8) placement = 'top'
  else if (placement === 'top' && r.top - GAP - TOOLTIP_H_EST < 8) placement = 'bottom'
  else if (placement === 'right' && r.right + GAP + TOOLTIP_W > vw - 8) placement = 'left'
  else if (placement === 'left' && r.left - GAP - TOOLTIP_W < 8) placement = 'right'

  const left = clamp(r.left, 8, vw - 8 - TOOLTIP_W)
  const top = clamp(r.top, 8, vh - 8 - TOOLTIP_H_EST)
  switch (placement) {
    case 'top':
      return { left: `${left}px`, top: `${r.top - GAP}px`, transform: 'translateY(-100%)' }
    case 'right':
      return { left: `${r.right + GAP}px`, top: `${top}px` }
    case 'left':
      return { left: `${r.left - GAP}px`, top: `${top}px`, transform: 'translateX(-100%)' }
    default:
      return { left: `${left}px`, top: `${r.bottom + GAP}px` }
  }
}

/* ── lifecycle ───────────────────────────────────────────────── */
watch(() => tour.stepIndex, measure)
watch(() => tour.isActive, measure)

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
