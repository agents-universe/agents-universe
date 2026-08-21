<template>
  <div class="mermaid-diagram" @click="openFullscreen">
    <div v-if="!rendered && !error" class="mermaid-loading">{{ t('mermaid.rendering') }}</div>
    <div v-if="error" class="mermaid-error">
      <div class="mermaid-error-head">{{ t('mermaid.renderFailed') }}</div>
      <pre class="mermaid-error-msg">{{ errorMsg }}</pre>
      <div class="mermaid-source">
        <div class="mermaid-source-label">{{ t('mermaid.sourceLabel') }}</div>
        <pre class="mermaid-source-code"><code>{{ code }}</code></pre>
      </div>
    </div>
    <div ref="containerEl" />
  </div>

  <!-- Fullscreen viewer -->
  <Teleport to="body">
    <div v-if="fullscreen" class="mermaid-modal-overlay" @click.self="fullscreen = false">
      <div class="mermaid-modal-content" @wheel.prevent="onWheel" @mousedown="startDrag">
        <div ref="fsEl" class="mermaid-modal-svg" v-html="svgHtml" />
      </div>
      <div class="mermaid-modal-controls">
        <button @click="zoomIn">+</button>
        <button @click="resetZoom">{{ scaleLabel }}</button>
        <button @click="zoomOut">−</button>
        <button @click="fullscreen = false">✕</button>
      </div>
    </div>
  </Teleport>
</template>

<script lang="ts">
// mermaid.render's id must be unique across the whole document. A counter
// inside <script setup> is per-instance: two diagrams mounted in the same
// millisecond would collide on the same id and clobber each other's
// temporary container (the error-path cleanup uses getElementById).
let mermaidRenderSeq = 0
</script>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{ code: string }>()

const { t } = useI18n()

const containerEl = ref<HTMLElement | null>(null)
const fsEl = ref<HTMLElement | null>(null)
const svgHtml = ref('')
const rendered = ref(false)
const error = ref(false)
const errorMsg = ref('')
const fullscreen = ref(false)

let naturalWidth = 0  // pixel width of SVG as rendered in the bubble
const scaleRef = ref(1)
const scaleLabel = computed(() => `${Math.round(scaleRef.value * 100)}%`)
let scale = 1
let tx = 0
let ty = 0
let dragging = false
let dragStart = { x: 0, y: 0 }

let renderCounter = 0

async function render() {
  error.value = false
  errorMsg.value = ''
  rendered.value = false

  await nextTick()
  if (!containerEl.value) return

  const code = props.code?.trim()
  if (!code) {
    error.value = true
    errorMsg.value = t('mermaid.emptyCode')
    return
  }

  renderCounter++
  const run = renderCounter
  const id = `mermaid-${Date.now()}-${++mermaidRenderSeq}`

  try {
    const { default: mermaid } = await import('mermaid')
    mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'strict' })

    const { svg } = await mermaid.render(id, code)
    // A newer render (code prop changed while this one was in flight) must
    // not be clobbered by this stale result.
    if (run !== renderCounter) return
    if (containerEl.value) {
      containerEl.value.innerHTML = svg
      // Trim excess whitespace by fitting viewBox to actual content bounds
      const svgEl = containerEl.value.querySelector('svg')
      if (svgEl) {
        try {
          const bbox = (svgEl as SVGSVGElement).getBBox()
          const pad = 8
          svgEl.setAttribute('viewBox', `${bbox.x - pad} ${bbox.y - pad} ${bbox.width + pad * 2} ${bbox.height + pad * 2}`)
          svgEl.removeAttribute('width')
          svgEl.removeAttribute('height')
          svgEl.style.width = '100%'
          svgEl.style.height = 'auto'
          svgEl.style.maxWidth = ''
        } catch {
          // getBBox may fail on hidden elements; leave SVG as-is
        }
      }
      svgHtml.value = containerEl.value.innerHTML
      rendered.value = true
    }
  } catch (e) {
    if (run !== renderCounter) return
    error.value = true
    errorMsg.value = e instanceof Error ? e.message : String(e)
    if (containerEl.value) {
      containerEl.value.innerHTML = ''
    }
    // mermaid renders error diagrams into a temporary container appended to
    // <body> — remove only THIS render's artifact. A body-wide selector
    // like body > [id*="mermaid-"] would also match other instances'
    // in-flight temporary containers and break their concurrent renders.
    const orphan = document.getElementById('d' + id)
    if (orphan) orphan.remove()
  }
}

function openFullscreen() {
  if (!rendered.value) return
  // Capture the SVG's actual rendered pixel width before opening the modal
  const inlineSvg = containerEl.value?.querySelector('svg')
  naturalWidth = inlineSvg ? inlineSvg.getBoundingClientRect().width : 0
  fullscreen.value = true
  scale = 1
  tx = 0
  ty = 0
  // Set SVG width in modal to match inline size, then apply transform
  requestAnimationFrame(() => {
    const modalSvg = fsEl.value?.querySelector('svg') as SVGSVGElement | null
    if (modalSvg && naturalWidth > 0) {
      modalSvg.style.width = `${naturalWidth}px`
      modalSvg.style.height = 'auto'
    }
    applyTransform()
  })
}

onMounted(render)
watch(() => props.code, render)

function onWheel(e: WheelEvent) {
  scale = Math.min(4, Math.max(0.25, scale - e.deltaY * 0.001))
  applyTransform()
}

function zoomIn() {
  scale = Math.min(4, scale * 1.3)
  applyTransform()
}

function zoomOut() {
  scale = Math.max(0.25, scale / 1.3)
  applyTransform()
}

function resetZoom() {
  scale = 1
  tx = 0
  ty = 0
  const modalSvg = fsEl.value?.querySelector('svg') as SVGSVGElement | null
  if (modalSvg && naturalWidth > 0) {
    modalSvg.style.width = `${naturalWidth}px`
    modalSvg.style.height = 'auto'
  }
  applyTransform()
}

function startDrag(e: MouseEvent) {
  dragging = true
  dragStart = { x: e.clientX - tx, y: e.clientY - ty }
  window.addEventListener('mousemove', onDrag)
  window.addEventListener('mouseup', stopDrag)
}

function onDrag(e: MouseEvent) {
  if (!dragging) return
  tx = e.clientX - dragStart.x
  ty = e.clientY - dragStart.y
  applyTransform()
}

function stopDrag() {
  dragging = false
  window.removeEventListener('mousemove', onDrag)
  window.removeEventListener('mouseup', stopDrag)
}

// If the modal closes (or the component unmounts) mid-drag, the window
// listeners added in startDrag would leak and keep transforming a detached
// element — remove them on teardown.
onUnmounted(stopDrag)

function applyTransform() {
  if (fsEl.value) {
    fsEl.value.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`
  }
  scaleRef.value = scale
}
</script>
