<template>
  <Teleport to="body">
    <div class="modal-overlay" @click.self="emit('close')">
      <div class="modal-dialog create-project-dialog">
        <div class="modal-header">
          <div class="modal-header-left">
            <span class="modal-header-icon">
              <FolderPlus :size="18" />
            </span>
            <h3 class="modal-title">{{ t('createProjectDialog.title') }}</h3>
          </div>
          <button class="modal-close" @click="emit('close')" :title="t('common.close')">
            <X :size="16" />
          </button>
        </div>

        <p class="modal-hint">{{ t('createProjectDialog.hint') }}</p>

        <div class="modal-body">
          <label class="input-label" for="project-name">{{ t('createProjectDialog.nameLabel') }}</label>
          <input
            id="project-name"
            v-model="name"
            class="input"
            :placeholder="t('createProjectDialog.namePlaceholder')"
            autofocus
            @keydown.enter="create"
            @keydown.esc="emit('close')"
          />
          <div v-if="categories.length" class="category-picker-block">
            <CategoryPicker v-model="selectedCategory" :categories="categories" />
          </div>
          <p v-if="error" class="modal-error">{{ error }}</p>
        </div>

        <div class="modal-actions">
          <button class="btn-ghost" @click="emit('close')">{{ t('common.cancel') }}</button>
          <button
            class="btn-primary"
            :disabled="!name.trim() || loading"
            @click="create"
          >
            {{ loading ? t('common.creating') : t('common.createProject') }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { FolderPlus, X } from 'lucide-vue-next'
import { useProjectStore } from '@/stores/project'
import { projectsApi } from '@/api/projects'
import type { ProjectCategory } from '@/types'
import CategoryPicker from './CategoryPicker.vue'

const emit = defineEmits<{ close: [] }>()

const { t } = useI18n()
const projectStore = useProjectStore()
const router = useRouter()
const name = ref('')
const loading = ref(false)
const error = ref('')
const categories = ref<ProjectCategory[]>([])
const selectedCategory = ref('software')

onMounted(async () => {
  try {
    categories.value = await projectsApi.getCategories()
    if (!categories.value.some(c => c.slug === selectedCategory.value)) {
      selectedCategory.value = categories.value[0]?.slug ?? 'software'
    }
  } catch {
    // 静默降级:保持仅 software 默认分类
  }
})

async function create() {
  if (!name.value.trim() || loading.value) return

  loading.value = true
  error.value = ''
  try {
    const project = await projectsApi.createProject(name.value.trim(), selectedCategory.value)
    projectStore.addProject(project)
    projectStore.setCurrentProject(project)
    router.push(`/projects/${project.project_id}/chat`)
    emit('close')
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('common.createFailed')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.category-picker-block {
  margin-top: 14px;
}
</style>
