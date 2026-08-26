import { onBeforeUnmount, onMounted, type Ref } from 'vue'

/**
 * Calls onCompleteClickOutside only for a complete outside click — both the
 * press and the release land outside `el`.
 *
 * Why not a `click` event: browsers dispatch `click` on the deepest common
 * ancestor of the press/release targets, so pressing inside a dialog and
 * releasing outside it still yields a click whose target is outside — and
 * wrongly dismisses the dialog. Tracking mousedown (state only) + mouseup
 * (verdict) ignores drags that started inside.
 *
 * directHit = true is for fullscreen backdrops (modal-overlay): "outside"
 * means the event target IS `el` itself, since every other pixel on screen
 * belongs to the dialog content.
 */
export function useClickOutside(
  el: Ref<HTMLElement | null | undefined>,
  onCompleteClickOutside: () => void,
  directHit = false,
) {
  let pressOutside = false

  const isOutside = (e: MouseEvent) => {
    const target = e.target as Node
    return directHit ? target === el.value : !!el.value && !el.value.contains(target)
  }
  const onMouseDown = (e: MouseEvent) => { pressOutside = isOutside(e) }
  const onMouseUp = (e: MouseEvent) => {
    if (pressOutside && isOutside(e)) onCompleteClickOutside()
    pressOutside = false
  }

  onMounted(() => {
    document.addEventListener('mousedown', onMouseDown)
    document.addEventListener('mouseup', onMouseUp)
  })
  onBeforeUnmount(() => {
    document.removeEventListener('mousedown', onMouseDown)
    document.removeEventListener('mouseup', onMouseUp)
  })
}
