import { onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useProjectStore } from '@/stores/project'
import { useFavoritesStore } from '@/stores/favorites'
import { projectsApi } from '@/api/projects'

export function useProjectData() {
  const store = useProjectStore()
  const favoritesStore = useFavoritesStore()
  const router = useRouter()
  const route = useRoute()

  onMounted(async () => {
    if (store.projects.length > 0) return

    try {
      const projects = await projectsApi.getProjects()
      store.setProjects(projects)

      if (projects.length === 0) return

      const routeProjectId = route.params.projectId as string | undefined

      if (routeProjectId) {
        const match = projects.find(p => p.project_id === routeProjectId)
        if (match && !store.currentProject) {
          store.setCurrentProject(match)
        }
      } else if (route.path === '/app') {
        const savedId = store.getSavedProjectId()
        const saved = savedId ? projects.find(p => p.project_id === savedId) : null
        const favorited = favoritesStore.resolvedFavoriteProjects
        const target = saved ?? (favorited.length > 0 ? favorited[0] : projects[0])
        store.setCurrentProject(target)
        router.push(`/projects/${target.project_id}/chat`)
      }
    } catch (e) {
      console.error('Failed to load project data', e)
    }
  })
}
