// ── Auth ──────────────────────────────────────────────────────────────────────

export interface User {
  userId: string
  displayName: string
}

// ── Project ───────────────────────────────────────────────────────────────────

export interface Project {
  project_id: string
  slug: string
  display_name: string
  parent_id: string | null
  fs_path: string | null
  can_delete: boolean
  category: string
  category_label?: string
  created_by: string | null
  visibility: 'public' | 'private'
  is_owner: boolean
  can_manage: boolean
}

export interface ProjectMember {
  user_id: string
  added_by: string | null
  created_at: string | null
}

export interface ProjectCategory {
  slug: string
  label: string
  description: string
  template_count: number
}

// ── Agent ─────────────────────────────────────────────────────────────────────

export interface EquippedItem {
  slug: string
  description: string
}

export interface ModelConfig {
  config_id: string
  provider: string
  model_id: string
  key_hint: string | null
  base_url: string | null
  url_mode: 'base_url' | 'full_url'
  /** Auto-route tier; null = not part of auto routing until the user assigns one. */
  complexity_tier: 'low' | 'mid' | 'high' | null
  /** Stored context-window override (tokens); null = use the name-matched default. */
  context_window: number | null
  /** Name-matched window the runtime uses when context_window is null. */
  default_context_window: number | null
  is_system: boolean
}

export interface AgentInfo {
  agent_id: string
  slug: string
  label: string
  description: string
  category: string
  /** Non-null when this agent is scoped to a single project. */
  project_id?: string | null
  skills: EquippedItem[]
  workflows: EquippedItem[]
  /** Tool names declared in the agent frontmatter (includes mcp / mcp:<slug> markers). */
  tools?: string[]
}

// ── Conversation ──────────────────────────────────────────────────────────────

export interface Annotation {
  type: 'box' | 'circle' | 'label' | 'arrow'
  x: number
  y: number
  w?: number
  h?: number
  label?: string
  color?: string
}

export interface ImageRecord {
  id: string
  url: string
  alt: string
  width?: number
  height?: number
  annotations?: Annotation[]
}

export interface AttachmentRecord {
  id: string
  url: string
  name: string
  media_type: string
  size: number
  width?: number
  height?: number
}

export interface ToolCallRecord {
  callId: string
  tool: string
  input: Record<string, unknown>
  output?: Record<string, unknown>
  status: 'preparing' | 'running' | 'done' | 'error' | 'interrupted'
  taskId?: string
  currentStep?: string
  nextStep?: string
}

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'tool'
  content: string
  /** Slug of the agent that produced this message (differs from the
   *  conversation default on @-mention turns). */
  agentSlug?: string
  toolCalls?: ToolCallRecord[]
  images?: ImageRecord[]
  attachments?: AttachmentRecord[]
  knowledgeLoaded?: string[]
  modelTier?: string
  modelName?: string
  isError?: boolean
  /** Partial assistant output cut short by an in-flight user injection. */
  interrupted?: boolean
  timestamp: number
}

export interface SelectionPrompt {
  promptId: string
  fieldKey: string
  question: string
  options: Array<{ label: string; value: string; description?: string }>
  allowOther?: boolean
  kind?: 'selection' | 'text'
  title?: string
  message?: string
  secret?: boolean
  taskId?: string
  serviceKey?: string
  environment?: string
  saveToProjectSecrets?: boolean
  saveToUserTokens?: boolean
}

export interface AgentTask {
  id: string
  title: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped'
  modelTier?: string
  /** Model that actually executed this subtask (task_started event). */
  modelName?: string
  summary?: string
  error?: string
  currentStep?: string
  nextStep?: string
  progressCompleted?: number
  progressTotal?: number
  dependsOn?: string[]
}

export interface ContextUsage {
  staticFiles: number
  dynamicFiles: number
  deferredFiles: number
  overflowFiles: number
  conversationHistoryTokens: number
  pendingTaskTokens: number
  totalBudget: number
}

export interface DbMessage {
  message_id: string
  role: string
  content: string
  /** Agent that produced (assistant) / was addressed by (user) this message;
   *  set on @-mention turns that ran a different agent. */
  agent_slug?: string | null
  /** Model that actually produced this reply (auto routing resolves one per
   *  turn; null on legacy rows written before the column existed). */
  model_name?: string | null
  tool_calls: Array<{
    call_id: string
    tool: string
    input: Record<string, unknown>
    output?: Record<string, unknown>
    status: string
    task_id?: string | null
  }>
  images?: ImageRecord[] | null
  attachments?: AttachmentRecord[] | null
  interrupted?: boolean
  created_at: string
}

export interface CompressResult {
  summary: string
  deleted_count: number
  kept_count: number
  messages: DbMessage[]
}

export interface DbTask {
  task_id: string
  title: string
  status: string
  estimated_complexity?: string
  result_summary?: string
  error_message?: string
  current_step?: string | null
  next_step?: string | null
  progress_completed?: number | null
  progress_total?: number | null
  depends_on?: string[]
}

// ── Knowledge ─────────────────────────────────────────────────────────────────

export interface KnowledgeItem {
  knowledge_id: string
  slug: string
  title: string
  category: string
  completeness_score: number
  tags: string[]
  word_count: number
  knowledge_level: 'index' | 'root' | 'detail' | 'auto'
  parent_slug: string | null
  children_slugs: string[]
  summary: string
  depth: number
}

export interface KnowledgeChildItem {
  slug: string
  title: string
  summary: string
  has_children: boolean
  depth: number
}

export interface KnowledgeAncestor {
  slug: string
  title: string
}

export interface DynamicLoadedItem {
  slug: string
  boundToTask: string | null
}

export type CategoryCompleteness = Record<string, number>

// ── Memory ────────────────────────────────────────────────────────────────────

export interface SessionNote {
  note: string
  timestamp: number
}

export interface PersonalMemory {
  memory_id: string
  content: string
  tags: string[]
  created_by: string
  created_at: string | null
  updated_at: string | null
  project_id: string | null
}

export interface EpisodicMemory {
  episode_id: string
  conversation_id: string
  summary: string
  key_findings: string[]
  open_questions: string[]
  generated_by: string | null
  created_at: string | null
}

// ── WebSocket ─────────────────────────────────────────────────────────────────

export type WsStatus = 'connecting' | 'connected' | 'disconnected' | 'failed'

export interface WsMessage {
  type: string
  [key: string]: unknown
}

// ── Conversation list ─────────────────────────────────────────────────────────

export interface ConversationItem {
  conversation_id: string
  title: string | null
  agent_id: string | null
  agent_slug: string | null
  token_budget: number
  tokens_used: number
  message_count: number
  active_task_count: number
  total_task_count: number
  is_running?: boolean
  created_at: string
  updated_at?: string | null
  has_running_tasks?: boolean
}

// ── Project Secrets ──────────────────────────────────────────────────────────

export interface ProjectSecret {
  secret_id: string
  service_key: string
  environment: string | null
  secret_name: string
  display_name: string | null
  key_hint: string | null
  created_by: string
  updated_at: string | null
}

// ── User Key Vault ────────────────────────────────────────────────────────────

export interface UserTokenEntry {
  service_key: string
  display_name: string | null
  key_hint: string | null
  base_url: string | null
  model_id: string | null
}
