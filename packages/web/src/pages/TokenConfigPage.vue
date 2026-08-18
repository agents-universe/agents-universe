<template>
  <div class="token-config-page">
    <TokenConfigModal @close="close" />
  </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import TokenConfigModal from '@/components/settings/TokenConfigModal.vue'

const router = useRouter()

// A direct visit/refresh of /settings/tokens leaves no in-app history —
// router.back() would exit the app entirely (or do nothing).
function close() {
  if (router.options.history.state.back !== null) {
    router.back()
  } else {
    router.push('/app')
  }
}
</script>
