<template>
  <div class="project-secrets-panel">
    <div class="section-header">
      <h4>{{ t('memoryPanels.projectSecrets') }}</h4>
      <button class="btn-sm" @click="showAddForm = !showAddForm">
        {{ showAddForm ? t('common.cancel') : t('memoryPanels.addToggle') }}
      </button>
    </div>

    <p class="hint">
      {{ t('memoryPanels.secretHintProject') }}
    </p>

    <div v-if="showAddForm" class="add-form">
      <input v-model="newKey.service_key" :placeholder="t('memoryPanels.serviceKeyPlaceholder')" class="input" />
      <input v-model="newKey.environment" :placeholder="t('memoryPanels.envPlaceholder')" class="input" />
      <input v-model="newKey.display_name" :placeholder="t('memoryPanels.displayNamePlaceholder')" class="input" />
      <input v-model="newKey.value" type="password" :placeholder="t('memoryPanels.secretValuePlaceholder')" class="input" autocomplete="off" />
      <button class="btn-primary" :disabled="!newKey.service_key || !newKey.value" @click="handleAdd">
        {{ t('common.save') }}
      </button>
    </div>

    <div v-if="store.loading" class="loading">{{ t('common.loading') }}</div>

    <div v-else-if="store.error" class="empty error-text">{{ store.error }}</div>

    <div v-else-if="store.secrets.length === 0" class="empty">
      {{ t('memoryPanels.noProjectSecrets') }}
    </div>

    <ul v-else class="secret-list">
      <li v-for="s in store.secrets" :key="s.secret_id" class="secret-item">
        <div class="secret-info">
          <span class="service-key">{{ s.service_key }}</span>
          <span v-if="s.environment" class="env-badge">{{ s.environment }}</span>
          <span v-if="s.key_hint" class="key-hint">{{ s.key_hint }}</span>
        </div>
        <div class="secret-meta">
          <span v-if="s.display_name" class="display-name">{{ s.display_name }}</span>
          <button class="btn-danger-sm" @click="handleDelete(s.secret_id)">{{ t('common.delete') }}</button>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useProjectSecretsStore } from '@/stores/projectSecrets'
import { useProjectStore } from '@/stores/project'

const { t } = useI18n()
const store = useProjectSecretsStore()
const projectStore = useProjectStore()
const projectId = computed(() => projectStore.currentProject?.project_id)

const showAddForm = ref(false)
const newKey = ref({ service_key: '', environment: '', display_name: '', value: '' })

watch(projectId, (id) => {
  if (id) {
    store.load(id)
  } else {
    store.reset()
  }
}, { immediate: true })

async function handleAdd() {
  if (!projectId.value || !newKey.value.service_key || !newKey.value.value) return
  try {
    await store.create(projectId.value, {
      service_key: newKey.value.service_key,
      environment: newKey.value.environment || undefined,
      display_name: newKey.value.display_name || undefined,
      value: newKey.value.value,
    })
    newKey.value = { service_key: '', environment: '', display_name: '', value: '' }
    showAddForm.value = false
  } catch (e) {
    window.alert(e instanceof Error ? e.message : t('memoryPanels.saveFailed'))
  }
}

async function handleDelete(secretId: string) {
  if (!projectId.value) return
  try {
    await store.remove(projectId.value, secretId)
  } catch (e) {
    window.alert(e instanceof Error ? e.message : t('memoryPanels.deleteFailed'))
  }
}
</script>

<style scoped>
.project-secrets-panel {
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
.secret-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.secret-item {
  padding: 0.4rem 0;
  border-bottom: 1px solid var(--color-border);
}
.secret-item:last-child {
  border-bottom: none;
}
.secret-info {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.service-key {
  font-size: 0.8rem;
  font-weight: 500;
  font-family: var(--font-mono);
}
.env-badge {
  font-size: 0.68rem;
  padding: 0.1rem 0.35rem;
  background: var(--color-bg-tertiary);
  border-radius: 3px;
  color: var(--color-text-secondary);
}
.key-hint {
  font-size: 0.72rem;
  color: var(--color-text-muted);
  font-family: var(--font-mono);
}
.secret-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 0.2rem;
}
.display-name {
  font-size: 0.72rem;
  color: var(--color-text-secondary);
}
</style>
