<template>
  <div class="file-tree">
    <TreeNode
      v-for="node in nodes"
      :key="node.key"
      :node="node"
      :depth="0"
      @select="(n) => emit('select', n)"
      @toggle="(n) => emit('toggle', n)"
    />
  </div>
</template>

<script setup lang="ts">
import TreeNode from './FileTreeNode.vue'
import type { WorkspaceTreeNode } from '@/types/workspace'

defineProps<{ nodes: WorkspaceTreeNode[] }>()
const emit = defineEmits<{
  select: [node: WorkspaceTreeNode]
  toggle: [node: WorkspaceTreeNode]
}>()
</script>
