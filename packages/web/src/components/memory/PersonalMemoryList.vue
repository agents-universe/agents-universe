<template>
  <div class="memory-section">
    <div class="section-label">
      个人记忆
      <button class="icon-btn" title="添加" @click="showAdd = true">+</button>
    </div>

    <div v-if="showAdd" class="memory-add-form">
      <textarea v-model="newContent" class="input memory-textarea" placeholder="记忆内容…" rows="3" />
      <input v-model="newTags" class="input" placeholder="标签（逗号分隔）" />
      <div class="memory-add-actions">
        <button class="btn-ghost" @click="showAdd = false">取消</button>
        <button class="btn-primary" :disabled="!newContent.trim()" @click="addMemory">保存</button>
      </div>
    </div>

    <div v-if="error" class="memory-error">{{ error }}</div>
    <div v-if="!memoryStore.personalMemories.length" class="memory-empty">暂无记忆</div>
    <div
      v-for="mem in memoryStore.personalMemories"
      :key="mem.memory_id"
      class="personal-memory-item"
    >
      <p class="personal-memory-content">{{ mem.content }}</p>
      <div class="personal-memory-meta">
        <span v-for="tag in mem.tags" :key="tag" class="memory-tag">{{ tag }}</span>
        <button class="icon-btn memory-archive" title="归档" @click="archive(mem.memory_id)">🗑</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useMemoryStore } from '@/stores/memory'
import { useProjectStore } from '@/stores/project'
import { memoriesApi } from '@/api/memories'

const memoryStore = useMemoryStore()
const projectStore = useProjectStore()

const showAdd = ref(false)
const newContent = ref('')
const newTags = ref('')
const error = ref<string | null>(null)

async function addMemory() {
  const pid = projectStore.currentProject?.project_id
  if (!pid || !newContent.value.trim()) return
  const tags = newTags.value.split(',').map((t) => t.trim()).filter(Boolean)
  try {
    const mem = await memoriesApi.createPersonal(pid, newContent.value.trim(), tags)
    // the request was for the project at call time — if the user
    // switched projects while it was in flight, the response must not leak
    // into the new project's list.
    if (projectStore.currentProject?.project_id !== pid) return
    memoryStore.addPersonalMemory(mem)
    newContent.value = ''
    newTags.value = ''
    showAdd.value = false
    error.value = null
  } catch (e) {
    error.value = e instanceof Error ? e.message : '添加记忆失败'
    console.error('Failed to add memory', e)
  }
}

async function archive(id: string) {
  const pid = projectStore.currentProject?.project_id
  if (!pid) return
  try {
    await memoriesApi.archivePersonal(pid, id)
    memoryStore.archivePersonalMemory(id)
    error.value = null
  } catch (e) {
    error.value = e instanceof Error ? e.message : '归档记忆失败'
    console.error('Failed to archive memory', e)
  }
}
</script>

<style scoped>
.memory-error {
  font-size: 0.78rem;
  color: var(--color-danger, #e53e3e);
  padding: 0.5rem 0;
  text-align: center;
}
</style>
