import { createRouter, createWebHistory } from 'vue-router'
import { basePath, withApi } from '@/utils/basePath'

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
      path: '/settings/publishes',
      component: () => import('@/pages/PublishesPage.vue'),
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
