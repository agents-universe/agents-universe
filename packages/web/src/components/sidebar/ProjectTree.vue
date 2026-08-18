<template>
  <div class="project-tree">
    <div class="nav-heading">
      <span>项目</span>
      <button class="add-project-btn" title="管理收藏" @click="showPicker = true">
        <Plus :size="14" />
      </button>
    </div>

    <div class="project-list">
      <div v-if="favoritesStore.resolvedFavoriteProjects.length === 0" class="empty-hint">
        点击 + 收藏项目
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
import { useRouter } from 'vue-router'
import { Plus, Folder } from 'lucide-vue-next'
import { useProjectStore } from '@/stores/project'
import { useFavoritesStore } from '@/stores/favorites'
import type { Project } from '@/types'
import ProjectPickerDialog from './ProjectPickerDialog.vue'

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
