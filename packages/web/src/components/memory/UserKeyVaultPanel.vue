<template>
  <div class="user-key-vault-panel">
    <div class="section-header">
      <h4>用户密钥</h4>
      <button class="btn-sm" @click="showAddForm = !showAddForm">
        {{ showAddForm ? '取消' : '+ 添加' }}
      </button>
    </div>

    <p class="hint">
      密钥跨项目跟随用户，不会发送给大模型；保存后仅由对应工具在服务端内部读取使用。
    </p>

    <div v-if="showAddForm" class="add-form">
      <input v-model="newKey.service_key" placeholder="service_key (如 myapi:token)" class="input" />
      <input v-model="newKey.display_name" placeholder="显示名称 (可选)" class="input" />
      <input v-model="newKey.base_url" placeholder="base_url (可选)" class="input" />
      <input v-model="newKey.value" type="password" placeholder="密钥值" class="input" autocomplete="off" />
      <button class="btn-primary" :disabled="!newKey.service_key || !newKey.value" @click="handleAdd">
        保存
      </button>
    </div>

    <div v-if="store.loading" class="loading">加载中...</div>

    <div v-else-if="store.error" class="empty error-text">{{ store.error }}</div>

    <div v-else-if="store.tokens.length === 0" class="empty">
      暂无用户密钥
    </div>

    <ul v-else class="token-list">
      <li v-for="t in store.tokens" :key="t.service_key" class="token-item">
        <div class="token-info">
          <span class="service-key">{{ t.service_key }}</span>
          <span v-if="t.key_hint" class="key-hint">{{ t.key_hint }}</span>
        </div>
        <div class="token-meta">
          <span v-if="t.display_name" class="display-name">{{ t.display_name }}</span>
          <span v-if="t.base_url" class="base-url">{{ t.base_url }}</span>
          <button class="btn-danger-sm" @click="handleDelete(t.service_key)">删除</button>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useUserTokensStore } from '@/stores/userTokens'

const store = useUserTokensStore()

const showAddForm = ref(false)
const newKey = ref({ service_key: '', display_name: '', base_url: '', value: '' })

onMounted(() => {
  store.load()
})

async function handleAdd() {
  if (!newKey.value.service_key || !newKey.value.value) return
  try {
    await store.upsert(newKey.value.service_key, {
      value: newKey.value.value,
      display_name: newKey.value.display_name || undefined,
      base_url: newKey.value.base_url || null,
    })
    newKey.value = { service_key: '', display_name: '', base_url: '', value: '' }
    showAddForm.value = false
  } catch (e) {
    window.alert(e instanceof Error ? e.message : '保存失败，请重试')
  }
}

async function handleDelete(serviceKey: string) {
  try {
    await store.remove(serviceKey)
  } catch (e) {
    window.alert(e instanceof Error ? e.message : '删除失败，请重试')
  }
}
</script>

<style scoped>
.user-key-vault-panel {
  padding: 0.75rem 0;
}
.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.5rem;
}
.section-header h4 {
  margin: 0;
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.hint {
  font-size: 0.72rem;
  color: var(--color-text-muted);
  margin: 0 0 0.5rem;
  line-height: 1.4;
}
.add-form {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  margin-bottom: 0.75rem;
  padding: 0.5rem;
  border: 1px solid var(--color-border);
  border-radius: 6px;
}
.input {
  padding: 0.35rem 0.5rem;
  font-size: 0.8rem;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-bg-secondary);
  color: var(--color-text-primary);
}
.btn-sm {
  font-size: 0.72rem;
  padding: 0.2rem 0.5rem;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
}
.btn-primary {
  font-size: 0.8rem;
  padding: 0.35rem 0.75rem;
  border: none;
  border-radius: 4px;
  background: var(--color-accent);
  color: white;
  cursor: pointer;
}
.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-danger-sm {
  font-size: 0.7rem;
  padding: 0.15rem 0.4rem;
  border: 1px solid var(--color-danger, #e53e3e);
  border-radius: 3px;
  background: transparent;
  color: var(--color-danger, #e53e3e);
  cursor: pointer;
}
.loading, .empty {
  font-size: 0.8rem;
  color: var(--color-text-muted);
  text-align: center;
  padding: 1rem 0;
}
.error-text {
  color: var(--color-danger, #e53e3e);
}
.token-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.token-item {
  padding: 0.4rem 0;
  border-bottom: 1px solid var(--color-border);
}
.token-item:last-child {
  border-bottom: none;
}
.token-info {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.service-key {
  font-size: 0.8rem;
  font-weight: 500;
  font-family: var(--font-mono);
}
.key-hint {
  font-size: 0.72rem;
  color: var(--color-text-muted);
  font-family: var(--font-mono);
}
.token-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 0.2rem;
  gap: 0.4rem;
}
.display-name {
  font-size: 0.72rem;
  color: var(--color-text-secondary);
}
.base-url {
  font-size: 0.68rem;
  color: var(--color-text-muted);
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
