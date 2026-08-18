<template>
  <div class="memory-panel">
    <UserKeyVaultPanel />
    <ProjectSecretsPanel />
    <SessionMemoryList />
    <PersonalMemoryList />
    <EpisodicTimeline />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import ProjectSecretsPanel from './ProjectSecretsPanel.vue'
import SessionMemoryList from './SessionMemoryList.vue'
import PersonalMemoryList from './PersonalMemoryList.vue'
import EpisodicTimeline from './EpisodicTimeline.vue'
import UserKeyVaultPanel from './UserKeyVaultPanel.vue'
import { useMemoryData } from '@/composables/useMemoryData'
import { useProjectStore } from '@/stores/project'

const projectStore = useProjectStore()
const projectId = computed(() => projectStore.currentProject?.project_id)
useMemoryData(projectId)
</script>
