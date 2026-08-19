<template>
  <div>
    <label class="input-label">{{ t('categoryPicker.label') }}</label>
    <div v-if="variant === 'cards'" class="category-card-grid">
      <button
        v-for="cat in categories"
        :key="cat.slug"
        type="button"
        class="category-card"
        :class="{ 'category-card-selected': modelValue === cat.slug }"
        :aria-pressed="modelValue === cat.slug"
        @click="emit('update:modelValue', cat.slug)"
      >
        <span class="category-card-name">{{ cat.label }}</span>
        <span class="category-card-desc">{{ cat.description }}</span>
        <span class="category-card-count">{{ t('categoryPicker.entriesCount', { count: cat.template_count }) }}</span>
      </button>
    </div>
    <select
      v-else
      class="input category-select"
      :value="modelValue"
      @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value)"
    >
      <option v-for="cat in categories" :key="cat.slug" :value="cat.slug">
        {{ cat.label }}{{ t('categoryPicker.entriesCountWrapped', { count: cat.template_count }) }}
      </option>
    </select>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { ProjectCategory } from '@/types'

const { t } = useI18n()

defineProps<{
  modelValue: string
  categories: ProjectCategory[]
  variant?: 'cards' | 'select'
}>()

const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
</script>

<style scoped>
.category-card-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 8px;
}

.category-card {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 10px 12px;
  background: var(--bg-tertiary);
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  color: var(--text-primary);
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s, background 0.15s;
}

.category-card:hover {
  background: var(--bg-secondary, var(--bg-tertiary));
}

.category-card-selected {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 8%, transparent);
}

.category-card-name {
  font-size: 13px;
  font-weight: 600;
}

.category-card-desc {
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.4;
}

.category-card-count {
  font-size: 11px;
  color: var(--text-muted);
}

.category-select {
  margin-top: 2px;
}
</style>
