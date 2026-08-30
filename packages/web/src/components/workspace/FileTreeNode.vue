<template>
  <div
    class="file-tree-node"
    :style="{ paddingLeft: `${depth * 14 + 6}px` }"
    :class="{ active: node.selected }"
  >
    <div class="file-tree-row" @click="handleClick">
      <ChevronRight
        v-if="node.type === 'dir'"
        :size="13"
        class="tree-chevron"
        :class="{ expanded: node.expanded, spacer: !node.children?.length }"
      />
      <span v-else class="tree-chevron spacer" />
      <component :is="nodeIcon" :size="14" class="tree-icon" :class="node.type" />
      <span class="tree-label">{{ node.name }}</span>
      <span v-if="node.badge" class="tree-badge">{{ node.badge }}</span>
      <Play v-if="node.runnable" :size="12" class="tree-run-icon" />
    </div>
    <div v-if="node.type === 'dir' && node.expanded && node.children?.length" class="tree-children">
      <FileTreeNode
        v-for="child in node.children"
        :key="child.key"
        :node="child"
        :depth="depth + 1"
        @select="(n) => emit('select', n)"
        @toggle="(n) => emit('toggle', n)"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ChevronRight, Folder, FolderOpen, FileText, FileCode, File, FlaskConical, Play, Terminal } from 'lucide-vue-next'
import type { WorkspaceTreeNode } from '@/types/workspace'

const props = defineProps<{
  node: WorkspaceTreeNode
  depth: number
}>()
const emit = defineEmits<{
  select: [node: WorkspaceTreeNode]
  toggle: [node: WorkspaceTreeNode]
}>()

const nodeIcon = computed(() => {
  if (props.node.type === 'dir') {
    return props.node.expanded ? FolderOpen : Folder
  }
  if (props.node.kind === 'script') return Terminal
  if (props.node.kind === 'playwright') return FlaskConical
  if (props.node.name.endsWith('.md')) return FileText
  if (props.node.name.endsWith('.py') || props.node.name.endsWith('.sh') || props.node.name.endsWith('.ts')) return FileCode
  return File
})

function handleClick() {
  if (props.node.type === 'dir') {
    emit('toggle', props.node)
  } else {
    emit('select', props.node)
  }
}
</script>
