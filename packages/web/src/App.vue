<template>
  <RouterView />
  <TourSpotlight />
  <WhatsNewDialog />
</template>

<script setup lang="ts">
import { RouterView } from 'vue-router'
import { onMounted } from 'vue'
import router from '@/router'
import { useAuthStore } from '@/stores/auth'
import { useTourStore } from '@/stores/tour'
import { withApi } from '@/utils/basePath'
import { RELEASES } from '@/tour/releases'
import { entriesToShow } from '@/tour/whatsNew'
import TourSpotlight from '@/components/tour/TourSpotlight.vue'
import WhatsNewDialog from '@/components/tour/WhatsNewDialog.vue'
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
        // Sequencing: a brand-new user gets the tour first — and finishing
        // the tour also marks last_seen_version, so the what's-new dialog
        // never fires on first login. Returning users only see the dialog
        // after a version bump.
        if (!tourStore.completed) {
          if (!document.querySelector('.modal-overlay')) {
            await tourStore.start(0, router)
          }
        } else if (entriesToShow(tourStore.lastSeenVersion, RELEASES).length > 0) {
          tourStore.whatsNewVisible = true
        }
      }
    }
  } catch {
    // ignore — router guard handles 401 redirect
  }
})
</script>
