<template>
  <div class="user-key-vault-panel">
    <div class="section-header">
      <h4>{{ t('memoryPanels.userKeys') }}</h4>
      <button class="btn-sm" @click="showAddForm = !showAddForm">
        {{ showAddForm ? t('common.cancel') : t('memoryPanels.addToggle') }}
      </button>
    </div>

    <p class="hint">
      {{ t('memoryPanels.secretHintUser') }}
    </p>

    <div v-if="showAddForm" class="add-form">
      <input v-model="newKey.service_key" :placeholder="t('memoryPanels.userServiceKeyPlaceholder')" class="input" />
      <input v-model="newKey.display_name" :placeholder="t('memoryPanels.displayNamePlaceholder')" class="input" />
      <input v-model="newKey.base_url" :placeholder="t('memoryPanels.baseUrlPlaceholder')" class="input" />
      <input v-model="newKey.value" type="password" :placeholder="t('memoryPanels.secretValuePlaceholder')" class="input" autocomplete="off" />
      <button class="btn-primary" :disabled="!newKey.service_key || !newKey.value" @click="handleAdd">
        {{ t('common.save') }}
      </button>
    </div>

    <div v-if="store.loading" class="loading">{{ t('common.loading') }}</div>

    <div v-else-if="store.error" class="empty error-text">{{ store.error }}</div>

    <div v-else-if="store.tokens.length === 0" class="empty">
      {{ t('memoryPanels.noUserKeys') }}
    </div>

    <ul v-else class="token-list">
      <li v-for="token in store.tokens" :key="token.service_key" class="token-item">
        <div class="token-info">
          <span class="service-key">{{ token.service_key }}</span>
          <span v-if="token.key_hint" class="key-hint">{{ token.key_hint }}</span>
        </div>
        <div class="token-meta">
          <span v-if="token.display_name" class="display-name">{{ token.display_name }}</span>
          <span v-if="token.base_url" class="base-url">{{ token.base_url }}</span>
          <button class="btn-danger-sm" @click="handleDelete(token.service_key)">{{ t('common.delete') }}</button>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useUserTokensStore } from '@/stores/userTokens'

const { t } = useI18n()
const store = useUserTokensStore()

const showAddForm = ref(false)
const newKey = ref({ service_key: '', display_name: '', base_url: '', value: '' })

onMounted(() => {
  store.load()
})

async function handleAdd() {
  if (!newKey.value.service_key || !newKey.value.value) return
  try {
    await store.upsert(newKey.value.service_key, {
      value: newKey.value.value,
      display_name: newKey.value.display_name || undefined,
      base_url: newKey.value.base_url || null,
    })
    newKey.value = { service_key: '', display_name: '', base_url: '', value: '' }
    showAddForm.value = false
  } catch (e) {
    window.alert(e instanceof Error ? e.message : t('memoryPanels.saveFailed'))
  }
}

async function handleDelete(serviceKey: string) {
  try {
    await store.remove(serviceKey)
  } catch (e) {
    window.alert(e instanceof Error ? e.message : t('memoryPanels.deleteFailed'))
  }
}
</script>

<style scoped>
.user-key-vault-panel {
  padding: 0.75rem 0;
}
.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.5rem;
}
.section-header h4 {
  margin: 0;
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.hint {
  font-size: 0.72rem;
  color: var(--color-text-muted);
  margin: 0 0 0.5rem;
  line-height: 1.4;
}
.add-form {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  margin-bottom: 0.75rem;
  padding: 0.5rem;
  border: 1px solid var(--color-border);
  border-radius: 6px;
}
.input {
  padding: 0.35rem 0.5rem;
  font-size: 0.8rem;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-bg-secondary);
  color: var(--color-text-primary);
}
.btn-sm {
  font-size: 0.72rem;
  padding: 0.2rem 0.5rem;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
}
.btn-primary {
  font-size: 0.8rem;
  padding: 0.35rem 0.75rem;
  border: none;
  border-radius: 4px;
  background: var(--color-accent);
  color: white;
  cursor: pointer;
}
.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-danger-sm {
  font-size: 0.7rem;
  padding: 0.15rem 0.4rem;
  border: 1px solid var(--color-danger, #e53e3e);
  border-radius: 3px;
  background: transparent;
  color: var(--color-danger, #e53e3e);
  cursor: pointer;
}
.loading, .empty {
  font-size: 0.8rem;
  color: var(--color-text-muted);
  text-align: center;
  padding: 1rem 0;
}
.error-text {
  color: var(--color-danger, #e53e3e);
}
.token-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.token-item {
  padding: 0.4rem 0;
  border-bottom: 1px solid var(--color-border);
}
.token-item:last-child {
  border-bottom: none;
}
.token-info {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.service-key {
  font-size: 0.8rem;
  font-weight: 500;
  font-family: var(--font-mono);
}
.key-hint {
  font-size: 0.72rem;
  color: var(--color-text-muted);
  font-family: var(--font-mono);
}
.token-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 0.2rem;
  gap: 0.4rem;
}
.display-name {
  font-size: 0.72rem;
  color: var(--color-text-secondary);
}
.base-url {
  font-size: 0.68rem;
  color: var(--color-text-muted);
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
