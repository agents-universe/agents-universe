<template>
  <RouterView />
  <TourSpotlight />
</template>

<script setup lang="ts">
import { RouterView } from 'vue-router'
import { onMounted } from 'vue'
import router from '@/router'
import { useAuthStore } from '@/stores/auth'
import { useTourStore } from '@/stores/tour'
import { withApi } from '@/utils/basePath'
import TourSpotlight from '@/components/tour/TourSpotlight.vue'
import type { UserPreferences } from '@/api/preferences'

const authStore = useAuthStore()
const tourStore = useTourStore()

onMounted(async () => {
  try {
    const res = await fetch(withApi('/api/me'), { credentials: 'include' })
    if (res.ok) {
      const data = await res.json() as {
        user_id: string
        display_name: string
        active_users_count?: number
        preferences?: UserPreferences
      }
      authStore.setUser({ userId: data.user_id, displayName: data.display_name })
      if (data.active_users_count != null) {
        authStore.setActiveUsersCount(data.active_users_count)
      }
      const prefs = data.preferences
      if (prefs) {
        tourStore.setServerState(prefs)
        // A brand-new user gets the tour first; returning users who already
        // completed it see nothing on login.
        if (!tourStore.completed && !document.querySelector('.modal-overlay')) {
          await tourStore.start(0, router)
        }
      }
    }
  } catch {
    // ignore — router guard handles 401 redirect
  }
})
</script>
