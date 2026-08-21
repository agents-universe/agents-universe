<template>
  <div class="app-layout">
    <!-- Mobile Top Bar (visible only on small screens) -->
    <div class="mobile-topbar">
      <button class="mobile-topbar-btn" @click="toggleLeft" :title="t('layout.menu')">
        <Menu :size="20" />
      </button>
      <span class="mobile-topbar-title"><Bot :size="16" class="sidebar-title-icon" /> Agents Universe</span>
      <button class="mobile-topbar-btn" @click="toggleRight" :title="t('layout.panel')">
        <PanelRight :size="20" />
      </button>
    </div>

    <!-- Mobile Backdrop — tap to close any open sidebar -->
    <Transition name="fade">
      <div
        v-if="showBackdrop"
        class="mobile-backdrop"
        @click="closeMobilePanels"
      />
    </Transition>

    <!-- Left Sidebar -->
    <aside class="sidebar-left" :class="{ collapsed: leftCollapsed }">
      <div class="sidebar-header">
        <span v-if="!leftCollapsed" class="sidebar-title"><Bot :size="16" class="sidebar-title-icon" /> Agents Universe</span>
        <button class="sidebar-toggle" @click="toggleLeft" title="Toggle sidebar">
          <ChevronLeft v-if="!leftCollapsed" :size="16" />
          <ChevronRight v-else :size="16" />
        </button>
      </div>
      <template v-if="!leftCollapsed">
        <ProjectTree />
        <AgentSwitcher />
        <SidebarFooter />
      </template>
    </aside>

    <!-- Center panel -->
    <main class="center-panel">
      <!-- Page-level navigation. The only way between chat / knowledge /
           scripts - hidden on non-project routes (/app, /settings). -->
      <nav v-if="pageSegment" class="center-topnav">
        <button
          v-for="nav in navTabs"
          :key="nav.id"
          class="center-tab"
          :class="{ active: pageSegment === nav.id }"
          :data-tour-target="`center-nav-${nav.id}`"
          @click="goToPage(nav.id)"
        ><component :is="nav.icon" :size="13" /> {{ nav.label }}</button>
        <!-- 压缩当前对话：与顶部页签同一行（右对齐），不单独占一行 -->
        <button
          v-if="pageSegment === 'chat' && convStore.messages.length > 0"
          class="compress-btn nav-compress"
          :disabled="isCompressDisabled"
          @click="handleCompress"
        >
          <Shrink :size="13" />
          <span>{{ compressing ? t('chatPanel.compressing') : t('chatPanel.compressContext') }}</span>
        </button>
      </nav>
      <div class="center-content">
        <Transition name="route-fade" mode="out-in">
          <RouterView />
        </Transition>
      </div>
    </main>

    <!-- Right Panel -->
    <aside class="panel-right" :class="{ collapsed: rightCollapsed }">
      <div class="panel-right-header">
        <div class="panel-tabs" v-if="!rightCollapsed">
          <button
            v-for="tab in tabs"
            :key="tab.id"
            class="panel-tab"
            :class="{ active: activeTab === tab.id }"
            :data-tour-target="`panel-tab-${tab.id}`"
            @click="activeTab = tab.id"
          ><component :is="tab.icon" :size="13" /> {{ tab.label }}</button>
        </div>
        <button class="sidebar-toggle" @click="toggleRight" title="Toggle panel">
          <ChevronRight v-if="!rightCollapsed" :size="16" />
          <ChevronLeft v-else :size="16" />
        </button>
      </div>

      <template v-if="!rightCollapsed">
        <ContextMeter />
        <component
          :is="panelComponent"
          :key="activeTab"
          v-bind="panelProps"
          @new-conversation="handleNewConversation"
        />
      </template>
    </aside>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterView, useRouter, useRoute } from 'vue-router'
import { ChevronLeft, ChevronRight, Bot, Menu, PanelRight, MessageSquare, BookOpen, Brain, Terminal, Shrink } from 'lucide-vue-next'
import { useProjectStore } from '@/stores/project'
import { useAgentStore } from '@/stores/agent'
import { useConversationStore } from '@/stores/conversation'
import { conversationsApi } from '@/api/conversations'
import { projectsApi } from '@/api/projects'
import ProjectTree from '@/components/sidebar/ProjectTree.vue'
import AgentSwitcher from '@/components/sidebar/AgentSwitcher.vue'
import SidebarFooter from '@/components/sidebar/SidebarFooter.vue'
import ContextMeter from '@/components/knowledge/ContextMeter.vue'
import ConversationTreePanel from '@/components/conversations/ConversationTreePanel.vue'
import KnowledgePanel from '@/components/knowledge/KnowledgePanel.vue'
import MemoryPanel from '@/components/memory/MemoryPanel.vue'
import { closeAllConnections } from '@/composables/useWebSocket'
import { invalidateLatestConversation } from '@/pages/ChatPage.vue'
import { useProjectData } from '@/composables/useProjectData'

useProjectData()

/* ── Sidebar state ────────────────────────────────────────────── */
const MOBILE_BREAKPOINT = 1024
const leftCollapsed = ref(false)
const rightCollapsed = ref(false)
const isMobile = ref(false)
const activeTab = ref<'conversations' | 'knowledge' | 'memory'>('conversations')

/** Sync isMobile flag and auto-collapse sidebars when entering mobile. */
function checkMobile() {
  const wasMobile = isMobile.value
  isMobile.value = window.innerWidth <= MOBILE_BREAKPOINT
  // When transitioning to mobile, collapse both sidebars so the center panel is visible.
  if (!wasMobile && isMobile.value) {
    leftCollapsed.value = true
    rightCollapsed.value = true
  }
}

/** Update --app-height CSS var from visualViewport for accurate mobile height (keyboard support). */
function updateAppHeight() {
  if (window.visualViewport) {
    document.documentElement.style.setProperty(
      '--app-height',
      `${window.visualViewport.height}px`,
    )
  }
}

/** Backdrop is visible on mobile when at least one sidebar is open. */
const showBackdrop = computed(() =>
  isMobile.value && (!leftCollapsed.value || !rightCollapsed.value),
)

/** Toggle left sidebar — on mobile, ensure mutual exclusion with right. */
function toggleLeft() {
  if (isMobile.value) {
    if (leftCollapsed.value) {
      leftCollapsed.value = false
      rightCollapsed.value = true
    } else {
      leftCollapsed.value = true
    }
  } else {
    leftCollapsed.value = !leftCollapsed.value
  }
}

/** Toggle right sidebar — on mobile, ensure mutual exclusion with left. */
function toggleRight() {
  if (isMobile.value) {
    if (rightCollapsed.value) {
      rightCollapsed.value = false
      leftCollapsed.value = true
    } else {
      rightCollapsed.value = true
    }
  } else {
    rightCollapsed.value = !rightCollapsed.value
  }
}

/** Close both sidebars (used by backdrop tap and route change on mobile). */
function closeMobilePanels() {
  leftCollapsed.value = true
  rightCollapsed.value = true
}

onMounted(() => {
  checkMobile()
  window.addEventListener('resize', checkMobile)
  updateAppHeight()
  window.visualViewport?.addEventListener('resize', updateAppHeight)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', checkMobile)
  window.visualViewport?.removeEventListener('resize', updateAppHeight)
})

/* ── Auto-close sidebars on navigation (mobile) ───────────────── */
const route = useRoute()
watch(() => route.fullPath, () => {
  if (isMobile.value) closeMobilePanels()
})

/* ── Panel tabs & content ─────────────────────────────────────── */
const { t } = useI18n()
const tabs = computed(() => [
  { id: 'conversations' as const, label: t('layout.tabConversations'), icon: MessageSquare },
  { id: 'knowledge' as const, label: t('layout.tabKnowledge'), icon: BookOpen },
  { id: 'memory' as const, label: t('layout.tabMemory'), icon: Brain },
])

const projectStore = useProjectStore()
const agentStore = useAgentStore()
const convStore = useConversationStore()
const router = useRouter()

/* ── Center top navigation ─────────────────────────────────────── */
// /projects/{pid}/(chat|knowledge|scripts) page segment; non-project routes
// (/app, /settings) have no page nav.
const pageSegment = computed(() => {
  const m = route.path.match(/^\/projects\/[^/]+\/(chat|knowledge|scripts)(?:\/|$)/)
  return m ? (m[1] as 'chat' | 'knowledge' | 'scripts') : null
})

const navTabs = computed(() => [
  { id: 'chat' as const, label: t('layout.tabConversations'), icon: MessageSquare },
  { id: 'knowledge' as const, label: t('layout.tabKnowledge'), icon: BookOpen },
  { id: 'scripts' as const, label: t('layout.tabScripts'), icon: Terminal },
])

// Compress lives on the top nav row (right-aligned) rather than on its own
// line under the chat panel; it targets the active conversation.
const compressing = ref(false)
const isCompressDisabled = computed(
  () => convStore.isStreaming || convStore.isThinking || compressing.value,
)

async function handleCompress() {
  const id = convStore.conversationId
  if (!id || compressing.value) return
  if (!window.confirm(t('chatPanel.compressConfirm'))) return
  compressing.value = true
  try {
    const res = await conversationsApi.compress(id)
    convStore.loadHistory(res.messages, id)
  } catch (e) {
    window.alert(e instanceof Error ? e.message : t('chatPanel.compressFailed'))
  } finally {
    compressing.value = false
  }
}

function goToPage(segment: 'chat' | 'knowledge' | 'scripts') {
  const pid = route.params.projectId
  if (!pid) return
  // No query forwarding: ?new=1/?onboarding=1 belong to specific entry flows
  // and must not leak into a tab switch (a stale ?new=1 would silently reset
  // the conversation). The mobile sidebar auto-close is handled by the
  // existing route.fullPath watcher below.
  router.push(`/projects/${pid}/${segment}`)
}

/* ── Route param ↔ project store sync ─────────────────────────── */
// ProjectTree pushes both together, but direct URL navigation / back-forward
// (same route record, component reused) changes only route.params.projectId.
// Without this, pages reading currentProject (ChatPage, ScriptExecutorPage)
// keep showing the OLD project while the URL says otherwise, and
// KnowledgeBrowserPage could even save edits into the wrong project.
const routePid = computed(() => route.params.projectId as string | undefined)
let pidSyncSeq = 0
watch(routePid, async (pid) => {
  if (!pid) return
  if (projectStore.currentProject?.project_id === pid) return
  const seq = ++pidSyncSeq
  let list = projectStore.projects
  if (list.length === 0) {
    try {
      list = await projectsApi.getProjects()
      projectStore.setProjects(list)
    } catch {
      return // auth/network errors surface via the route guard; leave as-is
    }
  }
  if (seq !== pidSyncSeq) return
  const match = list.find((p) => p.project_id === pid)
  if (match) projectStore.setCurrentProject(match)
}, { immediate: true })

const currentProjectId = computed(() => projectStore.currentProject?.project_id)
const currentAgentSlug = computed(() => agentStore.currentAgent?.slug)

const panelComponent = computed(() => {
  switch (activeTab.value) {
    case 'knowledge': return KnowledgePanel
    case 'memory': return MemoryPanel
    default: return ConversationTreePanel
  }
})

const panelProps = computed(() => {
  switch (activeTab.value) {
    case 'knowledge': return { projectId: currentProjectId.value }
    case 'memory': return {}
    default: return { projectId: currentProjectId.value, agentSlug: currentAgentSlug.value }
  }
})

function handleNewConversation() {
  closeAllConnections()
  convStore.reset()
  // Invalidate any in-flight latest-conversation load: its response would
  // otherwise resurrect the old conversation over the fresh empty state
  // . The ?new=1 marker stops the (possibly re-mounted) ChatPage
  // from auto-restoring the latest conversation after a non-chat-page click.
  invalidateLatestConversation()
  const id = projectStore.currentProject?.project_id
  if (id) router.push(`/projects/${id}/chat?new=1`)
}
</script>
