<template>
  <Teleport to="body">
    <div class="modal-overlay" @click.self="!deleting && emit('close')" @keydown.esc="!deleting && emit('close')">
      <div class="modal-dialog delete-project-dialog">
        <div class="modal-header">
          <div class="modal-header-left">
            <span class="modal-header-icon delete-icon"><Trash2 :size="18" /></span>
            <h3 class="modal-title">{{ t('deleteProjectDialog.title') }}</h3>
          </div>
          <button class="modal-close" :disabled="deleting" @click="emit('close')" :title="t('common.close')">
            <X :size="16" />
          </button>
        </div>

        <div class="delete-warning">
          <p class="delete-warning-title">{{ t('deleteProjectDialog.warningTitle') }}</p>
          <ul class="delete-warning-list">
            <li>{{ t('deleteProjectDialog.listConversations') }}</li>
            <li>{{ t('deleteProjectDialog.listKnowledge') }}</li>
            <li>{{ t('deleteProjectDialog.listMemories') }}</li>
            <li>{{ t('deleteProjectDialog.listScripts') }}</li>
            <li>{{ t('deleteProjectDialog.listSecrets') }}</li>
            <li>{{ t('deleteProjectDialog.listWorkspace') }}</li>
          </ul>
        </div>

        <div class="delete-confirm-section">
          <label class="delete-confirm-label">
            {{ t('deleteProjectDialog.confirmLabel') }}<span class="delete-slug-hint">{{ project.slug }}</span>
          </label>
          <input
            v-model="confirmInput"
            class="picker-search delete-confirm-input"
            :placeholder="project.slug"
            :disabled="deleting"
            @keydown.enter="handleDelete"
            ref="confirmRef"
          />
        </div>

        <p v-if="errorMsg" class="modal-error delete-error">{{ errorMsg }}</p>
        <p v-if="pendingMsg" class="delete-pending-msg">{{ pendingMsg }}</p>

        <div class="modal-footer">
          <button class="btn-ghost" :disabled="deleting" @click="emit('close')">{{ t('common.cancel') }}</button>
          <button
            class="btn-danger"
            :disabled="confirmInput !== project.slug || deleting"
            @click="handleDelete"
          >
            <span v-if="deleting" class="delete-spinner">…</span>
            <span v-else>{{ t('deleteProjectDialog.title') }}</span>
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, nextTick, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { Trash2, X } from 'lucide-vue-next'
import type { Project } from '@/types'
import { projectsApi } from '@/api/projects'
import { ApiError } from '@/api/client'

const { t } = useI18n()
const props = defineProps<{ project: Project }>()
const emit = defineEmits<{ close: []; deleted: [projectId: string] }>()

const confirmInput = ref('')
const deleting = ref(false)
const errorMsg = ref('')
const pendingMsg = ref('')
const confirmRef = ref<HTMLInputElement | null>(null)

onMounted(() => nextTick(() => confirmRef.value?.focus()))

async function handleDelete() {
  if (confirmInput.value !== props.project.slug || deleting.value) return
  deleting.value = true
  errorMsg.value = ''
  pendingMsg.value = ''
  try {
    await projectsApi.deleteProject(props.project.project_id, confirmInput.value)
    emit('deleted', props.project.project_id)
  } catch (e) {
    if (e instanceof ApiError) {
      if (e.code === 'PROJECT_DELETE_PENDING') {
        pendingMsg.value = t('deleteProjectDialog.pendingMsg', { id: e.deletionId ?? '' })
        // Treat as partial success — project is gone from DB
        emit('deleted', props.project.project_id)
        return
      }
      const codeMap: Record<string, string> = {
        PROJECT_HAS_CHILDREN: t('deleteProjectDialog.codeHasChildren'),
        PROJECT_HAS_RUNNING_WORK: t('deleteProjectDialog.codeRunningWork'),
        SLUG_CONFIRMATION_MISMATCH: t('deleteProjectDialog.codeSlugMismatch'),
        PROJECT_NOT_OWNER: t('deleteProjectDialog.codeNotOwner'),
        UNSAFE_PATH: t('deleteProjectDialog.codeUnsafePath'),
      }
      errorMsg.value = codeMap[e.code ?? ''] ?? e.message
    } else {
      errorMsg.value = e instanceof Error ? e.message : t('deleteProjectDialog.deleteFailed')
    }
    deleting.value = false
  }
}
</script>

<style scoped>
.delete-project-dialog {
  max-width: 460px;
}

.delete-icon {
  color: var(--color-error, #f87171);
}

.delete-warning {
  background: rgba(248, 113, 113, 0.06);
  border: 1px solid rgba(248, 113, 113, 0.18);
  border-radius: 6px;
  padding: 12px 14px;
  margin: 0 0 16px;
}

.delete-warning-title {
  font-size: 13px;
  color: var(--text-secondary);
  margin: 0 0 8px;
}

.delete-warning-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.8;
}

.delete-confirm-section {
  margin-bottom: 12px;
}

.delete-confirm-label {
  display: block;
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 6px;
}

.delete-slug-hint {
  font-family: monospace;
  background: var(--bg-tertiary);
  padding: 1px 5px;
  border-radius: 3px;
  color: var(--text-primary);
  margin-left: 4px;
}

.delete-confirm-input {
  width: 100%;
}

.delete-error {
  margin: 0 0 12px;
}

.delete-pending-msg {
  font-size: 12px;
  color: #fbbf24;
  background: rgba(251, 191, 36, 0.08);
  border: 1px solid rgba(251, 191, 36, 0.2);
  border-radius: 4px;
  padding: 8px 10px;
  margin: 0 0 12px;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 4px;
}

.btn-danger {
  background: var(--color-error, #dc2626);
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 6px 14px;
  font-size: 13px;
  cursor: pointer;
  transition: opacity 0.15s;
}

.btn-danger:hover:not(:disabled) {
  opacity: 0.85;
}

.btn-danger:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
</style>
