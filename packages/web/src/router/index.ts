import { createRouter, createWebHistory } from 'vue-router'
import { basePath, withApi } from '@/utils/basePath'
import { useProjectStore } from '@/stores/project'

const router = createRouter({
  history: createWebHistory(basePath || '/'),
  routes: [
    {
      path: '/app',
      component: () => import('@/layouts/AppLayout.vue'),
      children: [
        {
          path: '',
          component: () => import('@/pages/EmptyProjectPage.vue'),
        },
      ],
    },
    {
      path: '/projects/:projectId',
      component: () => import('@/layouts/AppLayout.vue'),
      children: [
        {
          path: '',
          redirect: (to) => ({ path: `/projects/${to.params.projectId}/chat` }),
        },
        {
          path: 'chat',
          component: () => import('@/pages/ChatPage.vue'),
        },
        {
          path: 'workspace',
          component: () => import('@/pages/WorkspacePage.vue'),
        },
        {
          path: 'publishes',
          component: () => import('@/pages/PublishesPage.vue'),
        },
        // Knowledge and scripts were merged into the unified workspace tab —
        // keep the old URLs working via redirect so bookmarks/links survive.
        {
          path: 'knowledge',
          redirect: (to) => ({ path: `/projects/${to.params.projectId}/workspace` }),
        },
        {
          path: 'scripts',
          redirect: (to) => ({ path: `/projects/${to.params.projectId}/workspace` }),
        },
      ],
    },
    {
      path: '/settings/tokens',
      component: () => import('@/pages/TokenConfigPage.vue'),
    },
    {
      // Publish management moved into the project layout as a top-level tab.
      // Old bookmarks/links land on the current project's publishes tab.
      // vue-router resolves `redirect` synchronously, so the project list is
      // only validated when it is already loaded; on a cold load we trust the
      // saved project id, and a stale id degrades like any other dead deep
      // link (pick a project in the sidebar).
      path: '/settings/publishes',
      redirect: () => {
        const store = useProjectStore()
        const wanted = store.currentProject?.project_id ?? store.getSavedProjectId()
        if (store.projects.length > 0) {
          const match = store.projects.find(p => p.project_id === wanted) ?? store.projects[0]
          return { path: `/projects/${match.project_id}/publishes` }
        }
        return wanted ? { path: `/projects/${wanted}/publishes` } : '/app'
      },
    },
    {
      // Embedded agent-service page. Outside AppLayout on purpose: it is a
      // self-contained chat for one published agent, not a project workspace.
      path: '/p/:publishId',
      component: () => import('@/pages/PublishPage.vue'),
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/app',
    },
  ],
})

// Auth guard — only checks /api/me on first navigation, then trusts the session
let authChecked = false

router.beforeEach(async (_to, _from, next) => {
  if (authChecked) { next(); return }
  try {
    const res = await fetch(withApi('/api/me'), { credentials: 'include' })
    if (res.status === 401) {
      next(false)
      window.location.href = withApi('/auth/login')
      return
    }
    authChecked = true
    next()
  } catch {
    // Network error: allow navigation, backend will handle auth
    authChecked = true
    next()
  }
})

export default router
