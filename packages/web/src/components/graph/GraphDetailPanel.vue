<template>
  <aside class="graph-detail">
    <div class="graph-detail-head">
      <span class="graph-detail-title">{{ t('graphPage.detailTitle') }}</span>
      <button class="graph-detail-close" :title="t('common.close')" @click="emit('close')">
        <X :size="14" />
      </button>
    </div>

    <p v-if="!node" class="graph-detail-hint">{{ t('graphPage.selectHint') }}</p>

    <template v-else>
      <div class="graph-detail-name" :title="node.name">{{ node.name }}</div>
      <div class="graph-detail-badges">
        <span class="graph-badge badge-type">{{ typeLabel(node.type) }}</span>
        <span v-if="node.repo" class="graph-badge badge-repo">{{ node.repo }}</span>
        <span v-if="node.lang" class="graph-badge badge-lang">{{ node.lang }}</span>
      </div>

      <dl class="graph-detail-fields">
        <template v-if="node.slug">
          <div>
            <dt>{{ t('graphPage.detailSlug') }}</dt>
            <dd>{{ node.slug }}</dd>
          </div>
          <div v-if="node.category">
            <dt>{{ t('graphPage.detailCategory') }}</dt>
            <dd>{{ node.category }}</dd>
          </div>
        </template>
        <template v-else>
          <div v-if="node.id.startsWith(`${node.repo}:f:`)">
            <dt>{{ t('graphPage.detailPath') }}</dt>
            <dd class="mono">{{ node.name }}</dd>
          </div>
          <div v-if="node.line">
            <dt>{{ t('graphPage.detailLine', { line: node.line }) }}</dt>
          </div>
          <div v-if="node.lines">
            <dt>{{ t('graphPage.detailLines') }}</dt>
            <dd>{{ node.lines }}</dd>
          </div>
        </template>
      </dl>

      <p v-if="node.summary" class="graph-detail-summary">{{ node.summary }}</p>

      <div class="graph-detail-neighbors">
        <div class="graph-detail-neighbors-head">{{ t('graphPage.neighbors') }}</div>
        <p v-if="neighbors.length === 0" class="graph-detail-hint">
          {{ t('graphPage.neighborsEmpty') }}
        </p>
        <ul v-else class="graph-neighbor-list">
          <li v-for="neighbor in neighbors" :key="neighbor.id">
            <button type="button" :title="neighbor.via ?? ''" @click="emit('focus', neighbor.id)">
              <span class="graph-neighbor-dot" :style="{ background: dotColor(neighbor.type) }" />
              <span class="graph-neighbor-name">{{ neighbor.name }}</span>
              <span v-if="neighbor.via" class="graph-neighbor-via">{{ neighbor.via }}</span>
            </button>
          </li>
        </ul>
      </div>
    </template>
  </aside>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { X } from 'lucide-vue-next'
import type { GraphNodeDto } from '@/api/projectGraph'

defineProps<{
  node: GraphNodeDto | null
  neighbors: GraphNodeDto[]
}>()

const emit = defineEmits<{
  close: []
  focus: [nodeId: string]
}>()

const { t } = useI18n()

const TYPE_I18N: Record<string, string> = {
  file: 'graphPage.typeFile',
  class: 'graphPage.typeClass',
  function: 'graphPage.typeFunction',
  symbol: 'graphPage.typeSymbol',
  table: 'graphPage.typeTable',
  view: 'graphPage.typeView',
  column: 'graphPage.typeColumn',
  procedure: 'graphPage.typeProcedure',
  trigger: 'graphPage.typeTrigger',
  module: 'graphPage.typeModule',
}

function typeLabel(type: string): string {
  const key = TYPE_I18N[type]
  return key ? t(key) : type
}

const DOT_COLOR: Record<string, string> = {
  file: '#60a5fa',
  table: '#fbbf24',
  view: '#fcd34d',
  procedure: '#fcd34d',
  trigger: '#fcd34d',
  module: '#c4b5fd',
  class: '#93c5fd',
  function: '#93c5fd',
  symbol: '#9ca3af',
  column: '#fcd34d',
}

function dotColor(type: string): string {
  return DOT_COLOR[type] ?? '#9ca3af'
}
</script>

<style scoped>
.graph-detail {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 280px;
  padding: 12px;
  border-left: 1px solid var(--border-color, #1f2937);
  background: var(--bg-primary, #0f1424);
  overflow-y: auto;
}

.graph-detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.graph-detail-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary, #9ca3af);
}

.graph-detail-close {
  display: flex;
  padding: 2px;
  border: none;
  background: none;
  color: var(--text-secondary, #9ca3af);
  cursor: pointer;
}

.graph-detail-hint {
  font-size: 12px;
  color: var(--text-secondary, #6b7280);
}

.graph-detail-name {
  font-size: 13px;
  font-weight: 600;
  word-break: break-all;
}

.graph-detail-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.graph-badge {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
}

.badge-type {
  background: #1e3a5f;
  color: #93c5fd;
}

.badge-repo {
  background: #3b2a63;
  color: #ddd6fe;
}

.badge-lang {
  background: #3f3f26;
  color: #d9f99d;
}

.graph-detail-fields {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  font-size: 12px;
}

.graph-detail-fields dt {
  color: var(--text-secondary, #6b7280);
}

.graph-detail-fields dd {
  margin: 0;
  word-break: break-all;
}

.mono {
  font-family: ui-monospace, monospace;
}

.graph-detail-summary {
  font-size: 12px;
  color: var(--text-secondary, #9ca3af);
}

.graph-detail-neighbors-head {
  margin-bottom: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary, #9ca3af);
}

.graph-neighbor-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.graph-neighbor-list button {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 3px 6px;
  border: none;
  border-radius: 4px;
  background: none;
  color: var(--text-primary, #e5e7eb);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}

.graph-neighbor-list button:hover {
  background: var(--bg-hover, #1f2937);
}

.graph-neighbor-dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.graph-neighbor-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.graph-neighbor-via {
  flex: none;
  font-size: 10px;
  color: var(--text-secondary, #6b7280);
}
</style>
