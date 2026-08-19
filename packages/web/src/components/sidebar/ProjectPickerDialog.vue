<template>
  <Teleport to="body">
    <div class="modal-overlay" @click.self="emit('close')" @keydown.esc="emit('close')">
      <div class="modal-dialog picker-dialog">
        <!-- Header -->
        <div class="modal-header">
          <div class="modal-header-left">
            <span class="modal-header-icon"><FolderHeart :size="18" /></span>
            <h3 class="modal-title">{{ t('projectPicker.title') }}</h3>
          </div>
          <button class="modal-close" @click="emit('close')" :title="t('common.close')">
            <X :size="16" />
          </button>
        </div>

        <p class="modal-hint">{{ t('projectPicker.hint') }}</p>

        <!-- Search -->
        <div class="picker-search-wrapper">
          <Search :size="14" class="picker-search-icon" />
          <input
            v-model="search"
            class="picker-search"
            :placeholder="t('projectPicker.searchPlaceholder')"
            autofocus
            @keydown.esc="emit('close')"
          />
        </div>

        <!-- Project List -->
        <div class="picker-list">
          <div
            v-for="project in filteredProjects"
            :key="project.project_id"
            class="picker-item"
            :class="{ favorited: favoritesStore.isProjectFavorited(project.project_id) }"
            :title="t('sidebar.common.toggleFavorite')"
            @click="favoritesStore.toggleProjectFavorite(project.project_id)"
          >
            <Folder :size="14" class="picker-item-icon" />
            <span class="picker-item-name">{{ project.display_name }}</span>
            <span v-if="project.category_label" class="picker-category-tag">{{ project.category_label }}</span>
            <button
              class="picker-star"
              :class="{ favorited: favoritesStore.isProjectFavorited(project.project_id) }"
              :title="t('sidebar.common.favoriteToSidebar')"
              @click.stop="favoritesStore.toggleProjectFavorite(project.project_id)"
            >
              <Star :size="14" />
            </button>
            <button
              class="picker-settings-btn"
              :class="{ 'picker-settings-btn--hidden': !project.can_manage }"
              :title="t('projectPicker.projectSettingsTitle')"
              @click.stop="project.can_manage && (settingsProject = project)"
            >
              <Settings :size="14" />
            </button>
            <button
              class="picker-delete-btn"
              :class="{ 'picker-delete-btn--hidden': !project.can_delete }"
              :title="t('projectPicker.deleteTitle')"
              @click.stop="project.can_delete && openDeleteDialog(project)"
            >
              <Trash2 :size="14" />
            </button>
          </div>
          <div v-if="filteredProjects.length === 0" class="picker-empty">
            {{ t('projectPicker.noMatches') }}
          </div>
        </div>

        <!-- Create project inline -->
        <div v-if="!showCreateForm" class="picker-footer">
          <button class="btn-ghost picker-create-btn" @click="showCreateForm = true">
            <Plus :size="14" /> {{ t('projectPicker.newProject') }}
          </button>
        </div>
        <div v-else class="picker-create-form">
          <div class="picker-create-row">
            <input
              v-model="newName"
              class="picker-search"
              :placeholder="t('projectPicker.namePlaceholder')"
              @keydown.enter="createProject"
              @keydown.esc="showCreateForm = false"
              ref="createInput"
            />
            <button
              class="btn-primary btn-sm"
              :disabled="!newName.trim() || creating"
              @click="createProject"
            >
              {{ creating ? '…' : t('common.create') }}
            </button>
          </div>
          <div v-if="categories.length" class="picker-create-category">
            <CategoryPicker
              v-model="newCategory"
              variant="select"
              :categories="categories"
            />
          </div>
          <p v-if="createError" class="modal-error">{{ createError }}</p>
        </div>
      </div>
    </div>
  </Teleport>

  <DeleteProjectDialog
    v-if="deletingProject"
    :project="deletingProject"
    @close="deletingProject = null"
    @deleted="onProjectDeleted"
  />

  <ProjectSettingsDialog
    v-if="settingsProject"
    :project="settingsProject"
    @close="settingsProject = null"
    @changed="projectStore.refreshProjects()"
  />
</template>

<script setup lang="ts">
import { ref, computed, nextTick, watch, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { Folder, FolderHeart, Star, Search, Plus, X, Trash2, Settings } from 'lucide-vue-next'
import { useProjectStore } from '@/stores/project'
import { useFavoritesStore } from '@/stores/favorites'
import { useAgentStore } from '@/stores/agent'
import { projectsApi } from '@/api/projects'
import type { Project, ProjectCategory } from '@/types'
import { CUSTOMIZATION_EXPERT_SLUG } from '@/utils/onboarding'
import DeleteProjectDialog from './DeleteProjectDialog.vue'
import ProjectSettingsDialog from './ProjectSettingsDialog.vue'
import CategoryPicker from './CategoryPicker.vue'

const emit = defineEmits<{ close: [] }>()

const { t } = useI18n()
const projectStore = useProjectStore()
const favoritesStore = useFavoritesStore()
const agentStore = useAgentStore()
const router = useRouter()

const search = ref('')
const showCreateForm = ref(false)
const newName = ref('')
const creating = ref(false)
const createError = ref('')
const createInput = ref<HTMLInputElement | null>(null)
const deletingProject = ref<Project | null>(null)
const settingsProject = ref<Project | null>(null)
const categories = ref<ProjectCategory[]>([])
const newCategory = ref('software')

onMounted(async () => {
  // 每次打开都从 API 拉取最新项目列表(store 内为会话级缓存,不刷新页面不会更新)
  await projectStore.refreshProjects()
  try {
    categories.value = await projectsApi.getCategories()
  } catch {
    // 静默降级:保持仅 software 默认分类
  }
})

function openDeleteDialog(project: Project) {
  deletingProject.value = project
}

async function onProjectDeleted(projectId: string) {
  deletingProject.value = null
  const wasCurrent = await projectStore.clearProject(projectId)
  favoritesStore.removeProjectFavorite(projectId)

  if (wasCurrent) {
    // Pick fallback: remaining favorited → first in list → /app
    const remaining = projectStore.projects
    const favFirst = favoritesStore.resolvedFavoriteProjects[0]
    const next = favFirst ?? remaining[0] ?? null
    if (next) {
      projectStore.setCurrentProject(next)
      router.push(`/projects/${next.project_id}/chat`)
      emit('close')
    } else {
      router.push('/app')
      emit('close')
    }
  }
}

const filteredProjects = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return projectStore.projects
  return projectStore.projects.filter(p =>
    p.display_name.toLowerCase().includes(q)
  )
})

watch(showCreateForm, (v) => {
  if (v) nextTick(() => createInput.value?.focus())
})

async function createProject() {
  if (!newName.value.trim() || creating.value) return
  creating.value = true
  createError.value = ''
  try {
    const project = await projectsApi.createProject(newName.value.trim(), newCategory.value)
    projectStore.addProject(project)
    favoritesStore.toggleProjectFavorite(project.project_id)
    projectStore.setCurrentProject(project)
    // 「自定义」分类:自动路由到项目定制专家
    if (project.category === 'other') {
      try {
        await agentStore.fetchAgents(project.project_id)
        const expert = agentStore.agents.find(a => a.slug === CUSTOMIZATION_EXPERT_SLUG && !a.project_id)
        if (expert) agentStore.setCurrentAgent(expert)
      } catch {
        // 非致命:保留默认 agent
      }
    }
    const query = project.onboarding_recommended || project.category === 'other' ? '?onboarding=1' : ''
    router.push(`/projects/${project.project_id}/chat${query}`)
    emit('close')
  } catch (e) {
    createError.value = e instanceof Error ? e.message : t('common.createFailed')
  } finally {
    creating.value = false
  }
}
</script>
