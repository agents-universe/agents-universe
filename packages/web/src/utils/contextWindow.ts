/**
 * Name-matched context-window inference for user model configs.
 *
 * Best-effort default for the Settings → AI Models context-window field: the
 * provider would use this value at runtime when the config leaves the field
 * empty. Matching the same naming conventions as the Python originals:
 *   packages/agent-core/src/agent_core/providers/{openai,anthropic_claude,google_gemini}.py
 *   (_context_window) and packages/agent-core/src/agent_core/providers/registry.py
 *   (default_context_window) — keep all four in sync. Unrecognized models get
 *   the provider's conservative fallback, not null: unlike tier routing, a
 *   window is always needed for the compression budget.
 */

const OPENAI_FALLBACK = 128_000

/** Same table as openai.py::_context_window (shared by openai + azure_openai). */
function openaiWindow(modelId: string): number {
  const m = modelId.toLowerCase()
  if (m.startsWith('gpt-5')) return 1_000_000
  if (['o1', 'o2', 'o3', 'o4'].some((p) => m.startsWith(p))) return 200_000
  // GLM-5.3 is the flagship (1M context); earlier GLM lines get the fallback.
  const glmVer = /^glm[-_]?(\d+(?:\.\d+)*)/.exec(m)
  if (glmVer && parseFloat(glmVer[1]) >= 5.3) return 1_000_000
  if (m.includes('gemini')) return 1_000_000
  return OPENAI_FALLBACK
}

/** Same table as anthropic_claude.py::_context_window. */
function anthropicWindow(modelId: string): number {
  const m = modelId.toLowerCase()
  const flagship = [
    'claude-fable-5',
    'claude-mythos-5',
    'claude-opus-5',
    'claude-opus-4-',
    'claude-sonnet-5',
    'claude-sonnet-4-6',
  ]
  if (flagship.some((name) => m.includes(name))) return 1_000_000
  return 200_000
}

/** Same table as google_gemini.py::_context_window. */
function geminiWindow(modelId: string): number {
  if (modelId.toLowerCase().includes('ultra')) return 32_768
  return 1_048_576
}

export function inferContextWindow(provider: string, modelId: string): number {
  if (provider === 'anthropic') return anthropicWindow(modelId)
  if (provider === 'google_gemini') return geminiWindow(modelId)
  return openaiWindow(modelId) // openai + azure_openai
}
