<template>
  <Teleport to="body">
    <div class="modal-overlay" @click.self="emit('close')">
      <div class="modal-dialog create-project-dialog">
        <div class="modal-header">
          <div class="modal-header-left">
            <span class="modal-header-icon">
              <FolderPlus :size="18" />
            </span>
            <h3 class="modal-title">新建项目</h3>
          </div>
          <button class="modal-close" @click="emit('close')" title="关闭">
            <X :size="16" />
          </button>
        </div>

        <p class="modal-hint">创建一个新的工作项目以组织对话与知识条目</p>

        <div class="modal-body">
          <label class="input-label" for="project-name">项目名称</label>
          <input
            id="project-name"
            v-model="name"
            class="input"
            placeholder="例如：季度报告分析"
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
          <button class="btn-ghost" @click="emit('close')">取消</button>
          <button
            class="btn-primary"
            :disabled="!name.trim() || loading"
            @click="create"
          >
            {{ loading ? '创建中…' : '创建项目' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { FolderPlus, X } from 'lucide-vue-next'
import { useProjectStore } from '@/stores/project'
import { useAgentStore } from '@/stores/agent'
import { projectsApi } from '@/api/projects'
import type { ProjectCategory } from '@/types'
import { CUSTOMIZATION_EXPERT_SLUG } from '@/utils/onboarding'
import CategoryPicker from './CategoryPicker.vue'

const emit = defineEmits<{ close: [] }>()

const projectStore = useProjectStore()
const agentStore = useAgentStore()
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
    // 「其他」分类:自动路由到项目定制专家
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
    error.value = e instanceof Error ? e.message : '创建失败'
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
