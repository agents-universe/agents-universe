<template>
  <Teleport to="body">
    <div class="modal-overlay" @click.self="emit('close')" @keydown.esc="emit('close')">
      <div class="modal-dialog project-settings-dialog">
        <div class="modal-header">
          <div class="modal-header-left">
            <span class="modal-header-icon"><Settings :size="18" /></span>
            <h3 class="modal-title">{{ t('projectSettingsDialog.title') }}</h3>
          </div>
          <button class="modal-close" @click="emit('close')" :title="t('common.close')">
            <X :size="16" />
          </button>
        </div>

        <div class="settings-section">
          <h4 class="settings-section-title">{{ t('projectSettingsDialog.accessTitle') }}</h4>
          <p class="hint">{{ t('projectSettingsDialog.accessHint') }}</p>
          <div v-if="project.is_owner" class="visibility-toggle">
            <button
              :class="['toggle-btn', { active: localVisibility === 'public' }]"
              :disabled="saving"
              @click="handleVisibility('public')"
            >{{ t('projectSettingsDialog.public') }}</button>
            <button
              :class="['toggle-btn', { active: localVisibility === 'private' }]"
              :disabled="saving"
              @click="handleVisibility('private')"
            >{{ t('projectSettingsDialog.private') }}</button>
          </div>
          <p v-else class="hint">{{ t('projectSettingsDialog.ownerOnlyHint') }}</p>
          <p v-if="visibilityError" class="error-text">{{ visibilityError }}</p>
        </div>

        <div v-if="project.can_manage" class="settings-section">
          <div class="section-header">
            <h4 class="settings-section-title">{{ t('projectSettingsDialog.membersTitle') }}</h4>
            <button class="btn-sm" @click="showAddForm = !showAddForm">
              {{ showAddForm ? t('common.cancel') : t('projectSettingsDialog.addMember') }}
            </button>
          </div>
          <p class="hint">
            {{ t('projectSettingsDialog.membersHint') }}
          </p>

          <div v-if="showAddForm" class="add-form">
            <input
              v-model="newUserId"
              :placeholder="t('projectSettingsDialog.userIdPlaceholder')"
              class="input"
              autocomplete="off"
              @keydown.enter="handleAdd"
            />
            <button class="btn-primary" :disabled="!newUserId.trim() || adding" @click="handleAdd">
              {{ adding ? t('common.adding') : t('common.add') }}
            </button>
          </div>
          <p v-if="memberError" class="error-text">{{ memberError }}</p>

          <div v-if="membersStore.loading" class="loading">{{ t('common.loading') }}</div>
          <p v-else-if="membersStore.error" class="error-text">{{ membersStore.error }}</p>
          <ul v-else class="member-list">
            <li v-if="project.created_by" class="member-item">
              <span class="member-user">{{ project.created_by }}</span>
              <span class="owner-badge">{{ t('projectSettingsDialog.createdBy') }}</span>
            </li>
            <li v-for="m in membersStore.members" :key="m.user_id" class="member-item">
              <span class="member-user">{{ m.user_id }}</span>
              <span class="member-meta">{{ t('projectSettingsDialog.addedBy', { user: m.added_by }) }}</span>
              <button
                class="btn-danger-sm"
                :disabled="removing === m.user_id"
                @click="handleRemove(m.user_id)"
              >{{ t('projectSettingsDialog.remove') }}</button>
            </li>
          </ul>
        </div>

        <div class="modal-footer">
          <button class="btn-ghost" @click="emit('close')">{{ t('common.close') }}</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Settings, X } from 'lucide-vue-next'
import type { Project } from '@/types'
import { projectsApi } from '@/api/projects'
import { ApiError } from '@/api/client'
import { useProjectStore } from '@/stores/project'
import { useProjectMembersStore } from '@/stores/projectMembers'

const props = defineProps<{ project: Project }>()
const emit = defineEmits<{ close: []; changed: [] }>()

const { t } = useI18n()
const projectStore = useProjectStore()
const membersStore = useProjectMembersStore()

const localVisibility = ref<'public' | 'private'>(props.project.visibility)
const saving = ref(false)
const visibilityError = ref('')
const showAddForm = ref(false)
const newUserId = ref('')
const adding = ref(false)
const removing = ref('')
const memberError = ref('')

watch(() => props.project.visibility, (v) => { localVisibility.value = v })

watch(() => props.project.project_id, (id) => {
  if (id) membersStore.load(id)
}, { immediate: true })

const CODE_MAP: Record<string, string> = {
  MEMBER_EXISTS: t('projectSettingsDialog.codeMemberExists'),
  MEMBER_IS_OWNER: t('projectSettingsDialog.codeMemberIsOwner'),
  MEMBER_NOT_FOUND: t('projectSettingsDialog.codeMemberNotFound'),
  INVALID_USER_ID: t('projectSettingsDialog.codeInvalidUserId'),
  PROJECT_NOT_MEMBER: t('projectSettingsDialog.codeNotMember'),
  PROJECT_PRIVATE: t('projectSettingsDialog.codePrivateNoAccess'),
  PROJECT_NOT_OWNER: t('projectSettingsDialog.codeNotOwner'),
}

function messageOf(e: unknown, fallback: string): string {
  if (e instanceof ApiError && e.code && CODE_MAP[e.code]) return CODE_MAP[e.code]
  return e instanceof Error ? e.message : fallback
}

async function handleVisibility(v: 'public' | 'private') {
  if (saving.value || v === localVisibility.value) return
  saving.value = true
  visibilityError.value = ''
  try {
    const updated = await projectsApi.updateProjectVisibility(props.project.project_id, v)
    localVisibility.value = updated.visibility
    projectStore.patchProject(updated)
    emit('changed')
  } catch (e) {
    visibilityError.value = messageOf(e, t('projectSettingsDialog.visibilityFailed'))
  } finally {
    saving.value = false
  }
}

async function handleAdd() {
  const uid = newUserId.value.trim()
  if (!uid || adding.value) return
  adding.value = true
  memberError.value = ''
  try {
    await membersStore.add(props.project.project_id, uid)
    newUserId.value = ''
    showAddForm.value = false
  } catch (e) {
    memberError.value = messageOf(e, t('projectSettingsDialog.addFailed'))
  } finally {
    adding.value = false
  }
}

async function handleRemove(userId: string) {
  if (removing.value) return
  removing.value = userId
  memberError.value = ''
  try {
    await membersStore.remove(props.project.project_id, userId)
  } catch (e) {
    memberError.value = messageOf(e, t('projectSettingsDialog.removeFailed'))
  } finally {
    removing.value = ''
  }
}
</script>

<style scoped>
.project-settings-dialog {
  max-width: 480px;
}

.settings-section {
  margin-bottom: 20px;
}

.settings-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
  margin: 0 0 8px;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.hint {
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.5;
  margin: 0 0 10px;
}

.visibility-toggle {
  display: flex;
  gap: 8px;
}

.toggle-btn {
  flex: 1;
  padding: 8px 0;
  font-size: 13px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s;
}

.toggle-btn:hover:not(:disabled) {
  border-color: var(--color-accent);
}

.toggle-btn.active {
  background: var(--color-accent);
  border-color: var(--color-accent);
  color: #fff;
}

.toggle-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.error-text {
  font-size: 12px;
  color: var(--color-error, #e53e3e);
  margin: 8px 0 0;
}

.add-form {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.input {
  flex: 1;
  padding: 6px 10px;
  font-size: 13px;
  font-family: var(--font-mono);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-bg-secondary);
  color: var(--text-primary);
}

.btn-sm {
  font-size: 12px;
  padding: 3px 8px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}

.btn-primary {
  font-size: 13px;
  padding: 6px 14px;
  border: none;
  border-radius: 4px;
  background: var(--color-accent);
  color: #fff;
  cursor: pointer;
  white-space: nowrap;
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-danger-sm {
  font-size: 12px;
  padding: 3px 8px;
  border: 1px solid var(--color-danger, #e53e3e);
  border-radius: 3px;
  background: transparent;
  color: var(--color-danger, #e53e3e);
  cursor: pointer;
  white-space: nowrap;
}

.btn-danger-sm:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.loading {
  font-size: 12px;
  color: var(--text-muted);
  text-align: center;
  padding: 12px 0;
}

.member-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.member-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid var(--color-border);
}

.member-item:last-child {
  border-bottom: none;
}

.member-user {
  font-family: var(--font-mono);
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.member-meta {
  font-size: 11px;
  color: var(--text-muted);
  flex-shrink: 0;
}

.owner-badge {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--color-accent);
  color: #fff;
  flex-shrink: 0;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  margin-top: 4px;
}

.btn-ghost {
  font-size: 13px;
  padding: 6px 14px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}
</style>
