<template>
  <RouterView />
</template>

<script setup lang="ts">
import { RouterView } from 'vue-router'
import { onMounted } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { withApi } from '@/utils/basePath'

const authStore = useAuthStore()

onMounted(async () => {
  try {
    const res = await fetch(withApi('/api/me'), { credentials: 'include' })
    if (res.ok) {
      const data = await res.json() as { user_id: string; display_name: string; active_users_count?: number }
      authStore.setUser({ userId: data.user_id, displayName: data.display_name })
      if (data.active_users_count != null) {
        authStore.setActiveUsersCount(data.active_users_count)
      }
    }
  } catch {
    // ignore — router guard handles 401 redirect
  }
})
</script>
