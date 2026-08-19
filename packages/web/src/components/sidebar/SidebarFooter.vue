<template>
  <div class="sidebar-footer">
    <div class="user-info">
      <span class="user-avatar">{{ initials }}</span>
      <span class="user-name">{{ authStore.displayName || t('common.user') }}</span>
      <button
        class="settings-btn"
        :title="t('sidebar.footer.switchLanguage')"
        @click="toggleLocale"
      >
        <Languages :size="12" />
      </button>
      <button class="settings-btn" :title="t('sidebar.footer.tokenSettings')" @click="goSettings">
        <Settings :size="12" />
      </button>
      <button class="logout-btn" :title="t('sidebar.footer.logout')" @click="handleLogout">
        <LogOut :size="12" />
      </button>
    </div>
    <span v-if="authStore.activeUsersCount > 1" class="active-users-hint">
      {{ t('sidebar.footer.activeUsers', { count: authStore.activeUsersCount }) }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Settings, LogOut, Languages } from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth'
import { withApi } from '@/utils/basePath'
import { setLocale, type AppLocale } from '@/i18n'

const authStore = useAuthStore()
const router = useRouter()
const { t, locale } = useI18n()

const initials = computed(() => {
  const name = authStore.displayName || ''
  if (!name) return '?'
  return name.slice(0, 2).toUpperCase()
})

function toggleLocale() {
  const next: AppLocale = locale.value === 'zh-CN' ? 'en-US' : 'zh-CN'
  setLocale(next)
}

function goSettings() {
  router.push('/settings/tokens')
}

function handleLogout() {
  authStore.logout()
  window.location.href = withApi('/auth/logout')
}
</script>
