<template>
  <div class="project-tree">
    <div class="nav-heading">
      <span>{{ t('sidebar.projects.title') }}</span>
      <button class="add-project-btn" :title="t('sidebar.common.manageFavorites')" @click="showPicker = true">
        <Plus :size="14" />
      </button>
    </div>

    <div class="project-list">
      <div v-if="favoritesStore.resolvedFavoriteProjects.length === 0" class="empty-hint">
        {{ t('sidebar.projects.emptyHint') }}
      </div>
      <div
        v-for="project in favoritesStore.resolvedFavoriteProjects"
        :key="project.project_id"
        class="project-item"
        :class="{ active: projectStore.currentProject?.project_id === project.project_id }"
        @click="selectProject(project)"
      >
        <Folder :size="14" class="project-icon" />
        <span class="project-name">{{ project.display_name }}</span>
        <span v-if="project.category_label" class="project-category-tag">{{ project.category_label }}</span>
      </div>
    </div>

    <ProjectPickerDialog v-if="showPicker" @close="showPicker = false" />
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { Plus, Folder } from 'lucide-vue-next'
import { useProjectStore } from '@/stores/project'
import { useFavoritesStore } from '@/stores/favorites'
import type { Project } from '@/types'
import ProjectPickerDialog from './ProjectPickerDialog.vue'

const { t } = useI18n()
const projectStore = useProjectStore()
const favoritesStore = useFavoritesStore()
const router = useRouter()
const showPicker = ref(false)

function selectProject(project: Project) {
  // No early-return on the current project — after a browser back/refresh the
  // user can be stranded on /app while the current project is already set;
  // clicking it must still navigate into its chat page (setCurrentProject is
  // idempotent).
  projectStore.setCurrentProject(project)
  router.push(`/projects/${project.project_id}/chat`)
}
</script>
