export type WorkspaceNodeKind = 'file' | 'dir' | 'script' | 'playwright'

export interface WorkspaceTreeNode {
  key: string
  name: string
  path: string // relative to project root (dirs end without trailing slash)
  type: 'dir' | 'file'
  kind: WorkspaceNodeKind
  expanded: boolean
  selected: boolean
  children?: WorkspaceTreeNode[]
  runnable: boolean
  badge?: string
  loading?: boolean
}
