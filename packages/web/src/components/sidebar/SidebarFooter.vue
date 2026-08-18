<template>
  <div class="sidebar-footer">
    <div class="user-info">
      <span class="user-avatar">{{ initials }}</span>
      <span class="user-name">{{ authStore.displayName || '用户' }}</span>
      <button class="settings-btn" title="密钥配置" @click="goSettings">
        <Settings :size="12" />
      </button>
      <button class="logout-btn" title="退出登录" @click="handleLogout">
        <LogOut :size="12" />
      </button>
    </div>
    <span v-if="authStore.activeUsersCount > 1" class="active-users-hint">
      {{ authStore.activeUsersCount }} 人在线
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { Settings, LogOut } from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth'
import { withApi } from '@/utils/basePath'

const authStore = useAuthStore()
const router = useRouter()

const initials = computed(() => {
  const name = authStore.displayName || ''
  if (!name) return '?'
  return name.slice(0, 2).toUpperCase()
})

function goSettings() {
  router.push('/settings/tokens')
}

function handleLogout() {
  authStore.logout()
  window.location.href = withApi('/auth/logout')
}
</script>
