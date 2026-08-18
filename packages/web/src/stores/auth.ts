import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useAuthStore = defineStore('auth', () => {
  const userId = ref<string | null>(null)
  const displayName = ref<string | null>(null)
  const isAuthenticated = ref(false)
  const activeUsersCount = ref(0)

  function setUser(user: { userId: string; displayName: string }) {
    userId.value = user.userId
    displayName.value = user.displayName
    isAuthenticated.value = true
  }

  function setActiveUsersCount(count: number) {
    activeUsersCount.value = count
  }

  function logout() {
    userId.value = null
    displayName.value = null
    isAuthenticated.value = false
  }

  return { userId, displayName, isAuthenticated, activeUsersCount, setUser, setActiveUsersCount, logout }
})
