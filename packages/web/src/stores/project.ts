import { defineStore } from 'pinia'
import { ref } from 'vue'
import { projectsApi } from '@/api/projects'
import type { Project } from '@/types'

const STORAGE_KEY = 'agents-universe:currentProjectId'

export const useProjectStore = defineStore('project', () => {
  const currentProject = ref<Project | null>(null)
  const projects = ref<Project[]>([])

  function setCurrentProject(project: Project | null) {
    const changed = project?.project_id !== currentProject.value?.project_id
    currentProject.value = project

    if (project) {
      try {
        localStorage.setItem(STORAGE_KEY, project.project_id)
      } catch { /* storage unavailable — selection survives in memory only */ }
    } else {
      try { localStorage.removeItem(STORAGE_KEY) } catch { /* ignore */ }
    }

    if (changed && project !== null) {
      // Close live WS connections too: an open socket from the previous
      // project's conversation would keep rebuilding the reset runtime from
      // streamed events (messages pile up unseen). Dynamic import avoids the
      // project → websocket → conversation → project cycle.
      import('@/composables/useWebSocket').then(({ closeAllConnections }) => closeAllConnections())
      import('./conversation').then(({ useConversationStore }) => useConversationStore().reset())
      import('./knowledge').then(({ useKnowledgeStore }) => useKnowledgeStore().reset())
      import('./memory').then(({ useMemoryStore }) => useMemoryStore().reset())
      import('./projectSecrets').then(({ useProjectSecretsStore }) => useProjectSecretsStore().reset())
    }
  }

  function setProjects(list: Project[]) {
    projects.value = list
  }

  /** 从 API 重新拉取项目列表,覆盖会话内缓存 */
  let projectListSeq = 0
  async function refreshProjects() {
    const seq = ++projectListSeq
    try {
      const list = await projectsApi.getProjects()
      // a stale in-flight refresh must not clobber newer local
      // mutations (e.g. a project created while the request was pending).
      if (seq !== projectListSeq) return
      projects.value = list
      // 同步当前项目对象(如被重命名/更新);id 未变,不会触发其他 store 重置
      if (currentProject.value) {
        const fresh = list.find(p => p.project_id === currentProject.value!.project_id)
        if (fresh) currentProject.value = fresh
      }
    } catch (e) {
      console.error('Failed to refresh projects', e)
    }
  }

  function addProject(project: Project) {
    projects.value.push(project)
  }

  /** 用最新数据替换同 id 项目(可见性切换后立即生效,无需等列表刷新) */
  function patchProject(project: Project) {
    const idx = projects.value.findIndex(p => p.project_id === project.project_id)
    if (idx >= 0) projects.value[idx] = project
    if (currentProject.value?.project_id === project.project_id) {
      currentProject.value = project
    }
  }

  function removeProject(projectId: string): boolean {
    const existed = projects.value.some(project => project.project_id === projectId)
    projects.value = projects.value.filter(project => project.project_id !== projectId)
    return existed
  }

  async function clearProject(projectId: string): Promise<boolean> {
    const wasCurrent = currentProject.value?.project_id === projectId
    removeProject(projectId)
    if (!wasCurrent) return false

    try {
      localStorage.removeItem(STORAGE_KEY)
    } catch { /* ignore storage failures */ }
    // Close the deleted project's live WS connections like setCurrentProject
    // does: an open socket would keep rebuilding the reset runtime from
    // streamed events (messages pile up unseen) and hold a connection slot.
    import('@/composables/useWebSocket').then(({ closeAllConnections }) => closeAllConnections())
    const [{ useConversationStore }, { useKnowledgeStore }, { useMemoryStore }, { useProjectSecretsStore }] = await Promise.all([
      import('./conversation'),
      import('./knowledge'),
      import('./memory'),
      import('./projectSecrets'),
    ])
    const conversationStore = useConversationStore()
    conversationStore.clearProjectStorage(projectId)
    conversationStore.reset()
    useKnowledgeStore().reset()
    useMemoryStore().reset()
    useProjectSecretsStore().reset()
    currentProject.value = null
    return true
  }

  // Backwards-compatible name for callers handling a completed deletion.
  const clearDeletedProject = clearProject

  function getSavedProjectId(): string | null {
    try {
      return localStorage.getItem(STORAGE_KEY)
    } catch { return null }
  }

  return {
    currentProject,
    projects,
    setCurrentProject,
    setProjects,
    refreshProjects,
    addProject,
    patchProject,
    removeProject,
    clearProject,
    clearDeletedProject,
    getSavedProjectId,
  }
})
