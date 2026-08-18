import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { UserTokenEntry } from '@/types'
import { userTokensApi } from '@/api/userTokens'

export const useUserTokensStore = defineStore('userTokens', () => {
  const tokens = ref<UserTokenEntry[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  // Seq guard: overlapping load()s (e.g. rapid WS refresh + manual open) must
  // not let a slow stale response clobber a fresher one.
  let loadSeq = 0

  async function load() {
    const seq = ++loadSeq
    loading.value = true
    error.value = null
    try {
      const list = await userTokensApi.list()
      if (seq === loadSeq) tokens.value = list
    } catch (e: unknown) {
      if (seq === loadSeq) error.value = e instanceof Error ? e.message : 'Failed to load user tokens'
    } finally {
      if (seq === loadSeq) loading.value = false
    }
  }

  async function upsert(
    serviceKey: string,
    data: { value?: string; display_name?: string; base_url?: string | null },
  ) {
    await userTokensApi.upsert(serviceKey, data)
    await load()
  }

  async function remove(serviceKey: string) {
    await userTokensApi.remove(serviceKey)
    tokens.value = tokens.value.filter((t) => t.service_key !== serviceKey)
  }

  function reset() {
    tokens.value = []
    loading.value = false
    error.value = null
    // Invalidate any in-flight load (same race as projectSecrets).
    loadSeq++
  }

  return { tokens, loading, error, load, upsert, remove, reset }
})
