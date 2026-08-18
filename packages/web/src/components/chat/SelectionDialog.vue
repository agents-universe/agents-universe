<template>
  <div class="selection-dialog">
    <p v-if="title" class="selection-title">{{ title }}</p>
    <p class="selection-question">{{ question }}</p>
    <p v-if="secret" class="secret-notice">
      该值不会发送给大模型，将直接加密保存到数据库，由服务端工具内部使用。
      <span v-if="saveToProjectSecrets">项目内有访问权限的用户也可通过对应工具间接使用。</span>
      <span v-if="saveToUserTokens">密钥将保存到你的用户仓库，跨项目可用。</span>
    </p>

    <!-- Selection mode (radio options) -->
    <template v-if="isSelectionMode">
      <div class="selection-options">
        <label
          v-for="opt in options"
          :key="opt.value"
          class="selection-option"
          :class="{ selected: selected === opt.value }"
        >
          <input type="radio" :value="opt.value" v-model="selected" />
          <span>
            <span class="option-label">{{ opt.label }}</span>
            <span v-if="opt.description" class="option-desc">{{ opt.description }}</span>
          </span>
        </label>

        <label v-if="allowOther" class="selection-option" :class="{ selected: selected === '__other__' }">
          <input type="radio" value="__other__" v-model="selected" />
          <span class="option-label">其他</span>
        </label>
      </div>

      <input
        v-if="selected === '__other__'"
        v-model="otherText"
        class="selection-other-input input"
        placeholder="请输入…"
      />
    </template>

    <!-- Text / Secret mode -->
    <template v-else>
      <input
        v-model="textValue"
        :type="secret ? 'password' : 'text'"
        class="text-input input"
        :placeholder="secret ? '请输入密钥值…' : '请输入…'"
        autocomplete="off"
        @keydown.enter="submit"
      />
    </template>

    <div class="selection-actions">
      <button class="btn-cancel" @click="cancel">取消</button>
      <button class="btn-primary" :disabled="!canSubmit || submitted" @click="submit">确认</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onUnmounted } from 'vue'

const props = withDefaults(defineProps<{
  promptId: string
  fieldKey: string
  question: string
  options?: Array<{ label: string; value: string; description?: string }>
  allowOther?: boolean
  kind?: 'selection' | 'text'
  title?: string
  message?: string
  secret?: boolean
  serviceKey?: string
  environment?: string
  saveToProjectSecrets?: boolean
  saveToUserTokens?: boolean
}>(), {
  allowOther: true,
  kind: 'selection',
  secret: false,
  saveToProjectSecrets: false,
  saveToUserTokens: false,
})

const emit = defineEmits<{
  resolve: [promptId: string, value: string, meta: { secret?: boolean; serviceKey?: string; environment?: string; saveToProjectSecrets?: boolean; saveToUserTokens?: boolean }]
  cancel: [promptId: string]
}>()

const isSelectionMode = computed(() => props.kind === 'selection' && props.options && props.options.length > 0)

const selected = ref('')
const otherText = ref('')
const textValue = ref('')
// Double-click guard: once the response frame is emitted, further clicks are
// swallowed until the button re-arms. On a successful send the parent
// unmounts this component before the timer fires; on a failed send the
// dialog stays open for retry, so the button must become clickable again.
const submitted = ref(false)
let rearmTimer: ReturnType<typeof setTimeout> | null = null

const canSubmit = computed(() => {
  if (isSelectionMode.value) {
    if (!selected.value) return false
    if (selected.value === '__other__') return otherText.value.trim().length > 0
    return true
  }
  return textValue.value.trim().length > 0
})

function submit() {
  if (submitted.value || !canSubmit.value) return
  let value: string
  if (isSelectionMode.value) {
    value = selected.value === '__other__' ? otherText.value.trim() : selected.value
  } else {
    value = textValue.value.trim()
  }
  submitted.value = true
  // ~800ms covers the browser double-click window; see the guard comment
  // above for why the button must re-arm.
  rearmTimer = setTimeout(() => { submitted.value = false }, 800)
  emit('resolve', props.promptId, value, {
    secret: props.secret || undefined,
    serviceKey: props.serviceKey,
    environment: props.environment,
    saveToProjectSecrets: props.saveToProjectSecrets || undefined,
    saveToUserTokens: props.saveToUserTokens || undefined,
  })
  // Intentionally NOT clearing textValue here: on a failed send the dialog
  // stays open for retry, and the typed value must survive. The parent
  // unmounts this component once the response frame actually left.
}

function cancel() {
  emit('cancel', props.promptId)
}

onUnmounted(() => {
  if (rearmTimer) clearTimeout(rearmTimer)
})
</script>

<style scoped>
.selection-title {
  font-weight: 600;
  font-size: 0.9rem;
  margin: 0 0 0.25rem;
}
.secret-notice {
  font-size: 0.75rem;
  color: var(--color-text-muted);
  background: var(--color-bg-tertiary);
  padding: 0.4rem 0.6rem;
  border-radius: 4px;
  margin: 0.4rem 0;
  line-height: 1.4;
}
.text-input {
  width: 100%;
  padding: 0.5rem;
  font-size: 0.85rem;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-bg-secondary);
  color: var(--color-text-primary);
  margin: 0.4rem 0;
}
.selection-actions {
  display: flex;
  gap: 0.5rem;
  justify-content: flex-end;
  margin-top: 0.5rem;
}
.btn-cancel {
  font-size: 0.8rem;
  padding: 0.35rem 0.75rem;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
}
</style>
