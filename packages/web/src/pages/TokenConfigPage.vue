<template>
  <div class="token-config-page">
    <TokenConfigModal :target-config-id="targetConfigId" @close="close" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import TokenConfigModal from '@/components/settings/TokenConfigModal.vue'

const route = useRoute()
const router = useRouter()

// ?config=<id> (from the context_exceeded bubble's action button) makes the
// modal scroll to and focus that model row's context-size input.
const targetConfigId = computed(() =>
  typeof route.query.config === 'string' ? route.query.config : null,
)

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
