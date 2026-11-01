import { apiFetch } from './client'

const enc = encodeURIComponent

/** One node of the merged project graph. Code nodes carry a repo and are
 * namespaced `{repo}:{graph_json_id}`; knowledge pages are `d:{slug}` with
 * repo=null and type="module". */
export interface GraphNodeDto {
  id: string
  repo: string | null
  type: string
  name: string
  lang?: string
  line?: number
  lines?: number
  slug?: string
  category?: string
  summary?: string
  /** Present on neighbors responses: how this node connects to the queried one. */
  direction?: string | null
  via?: string | null
  depth?: number
}

export interface GraphEdgeDto {
  src: string
  dst: string
  type: string
}

export interface GraphRepoStatus {
  name: string
  has_graph: boolean
  built_at?: number
  head_sha?: string
  stats?: Record<string, number>
}

export interface ProjectGraph {
  needs_build: boolean
  generated_at: number
  level: string
  truncated: boolean
  no_repos: boolean
  repos: GraphRepoStatus[]
  counts: { nodes: number; edges: number; docs: number; repos: number }
  nodes: GraphNodeDto[]
  edges: GraphEdgeDto[]
}

export interface GraphNeighbors {
  node: GraphNodeDto
  neighbors: GraphNodeDto[]
  count: number
  edges: GraphEdgeDto[]
  truncated?: boolean
}

export interface GraphBuildResult {
  repos: Array<GraphRepoStatus & Record<string, unknown>>
}

export const projectGraphApi = {
  getGraph: (
    projectId: string,
    params?: { level?: string; maxNodes?: number },
    signal?: AbortSignal,
  ) => {
    const query = new URLSearchParams()
    if (params?.level) query.set('level', params.level)
    if (params?.maxNodes) query.set('max_nodes', String(params.maxNodes))
    const qs = query.toString()
    return apiFetch<ProjectGraph>(
      `/api/projects/${enc(projectId)}/graph${qs ? `?${qs}` : ''}`,
      { signal },
    )
  },

  getNeighbors: (projectId: string, node: string, depth = 1, signal?: AbortSignal) =>
    apiFetch<GraphNeighbors>(
      `/api/projects/${enc(projectId)}/graph/neighbors?node=${enc(node)}&depth=${depth}`,
      { signal },
    ),

  build: (projectId: string, force = false) =>
    apiFetch<GraphBuildResult>(`/api/projects/${enc(projectId)}/graph/build`, {
      method: 'POST',
      body: JSON.stringify({ force }),
    }),
}
