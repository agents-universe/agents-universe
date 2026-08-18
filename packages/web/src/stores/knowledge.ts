import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { KnowledgeItem, DynamicLoadedItem, CategoryCompleteness } from '@/types'

export const useKnowledgeStore = defineStore('knowledge', () => {
  const items = ref<KnowledgeItem[]>([])
  const completeness = ref<CategoryCompleteness>({})
  const loadedThisTurn = ref<string[]>([])
  const dynamicallyLoaded = ref<DynamicLoadedItem[]>([])
  const refreshToken = ref(0)

  function setItems(list: KnowledgeItem[]) {
    items.value = list
  }

  function setCompleteness(c: CategoryCompleteness) {
    completeness.value = c
  }

  function setLoadedThisTurn(slugs: string[]) {
    loadedThisTurn.value = slugs
  }

  function addDynamicLoad(item: DynamicLoadedItem) {
    const idx = dynamicallyLoaded.value.findIndex((d) => d.slug === item.slug)
    if (idx >= 0) {
      dynamicallyLoaded.value[idx] = item
    } else {
      dynamicallyLoaded.value.push(item)
    }
  }

  function removeDynamicLoad(slug: string) {
    dynamicallyLoaded.value = dynamicallyLoaded.value.filter((d) => d.slug !== slug)
  }

  function clearDynamicLoads() {
    dynamicallyLoaded.value = []
  }

  function triggerRefresh() {
    refreshToken.value++
  }

  function reset() {
    items.value = []
    completeness.value = {}
    loadedThisTurn.value = []
    dynamicallyLoaded.value = []
    refreshToken.value = 0
  }

  return {
    items,
    completeness,
    loadedThisTurn,
    dynamicallyLoaded,
    refreshToken,
    setItems,
    setCompleteness,
    setLoadedThisTurn,
    addDynamicLoad,
    removeDynamicLoad,
    clearDynamicLoads,
    triggerRefresh,
    reset,
  }
})
