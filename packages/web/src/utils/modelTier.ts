/**
 * Complexity-tier inference for user model configs.
 *
 * Best-effort default for the auto-route tier assignment: model_id keywords
 * are matched against the naming conventions of the major providers (as of
 * 2026-08). The result is a starting value the user can override in Settings;
 * unrecognized models get null and are simply not part of auto routing until
 * the user assigns a tier.
 *
 * Keep the keyword tables here and the Python original in
 * packages/api/src/api/services/model_tier.py in sync.
 */

export type ComplexityTier = 'low' | 'mid' | 'high'

// Vendor-specific override BEFORE the generic scan: on DeepSeek/Qwen/Doubao,
// "flash" is the budget tier (deepseek-v4-flash, qwen3.6-flash replacing the
// retired turbo); on Gemini "flash" is the balanced tier (handled below).
const FLASH_IS_BUDGET_BRANDS = ['deepseek', 'qwen', 'doubao']

// Generic keyword tables — matched token-set intersection, low → high → mid
// priority so compound names resolve to the weakest matching tier
// (gpt-4o-mini → mini beats 4o; gpt-5.6-sol-pro → pro beats nothing else).
const LOW_KEYWORDS = [
  'haiku', 'luna', 'mini', 'nano', 'lite', 'small', 'turbo', 'air',
  '1b', '3b', '7b', '8b',
]
const HIGH_KEYWORDS = [
  'fable', 'sol', 'opus', 'pro', 'ultra', 'max', 'premium',
  'reasoner', 'thinking', 'large',
]
const MID_KEYWORDS = [
  'terra', 'flash', 'sonnet', '4o', 'o1', 'o3', 'o4', 'plus', 'medium', '70b',
]
// Conservative default tier for Chinese domestic brands whose version naming
// carries no tier signal (kimi-k3, glm-5.2, ...): treat as balanced; the user
// can reassign in Settings.
const BRAND_DEFAULT_MID = ['deepseek', 'qwen', 'kimi', 'glm', 'doubao', 'hunyuan', 'minimax']

// GLM-5.3 is the flagship (1M context); earlier GLM lines carry no tier
// signal. Runs after the keyword scans so budget names (glm-5.3-air) still
// resolve low.
const GLM_VERSION = /^glm[-_]?(\d+(?:\.\d+)*)/

function tokens(modelId: string): Set<string> {
  return new Set(modelId.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean))
}

/** Brand detection tolerates glued version digits (qwen3.6 → "qwen3"). */
function hasBrand(tokens: Set<string>, brand: string): boolean {
  for (const t of tokens) {
    if (t === brand || t.startsWith(brand)) return true
  }
  return false
}

export function inferTier(provider: string, modelId: string): ComplexityTier | null {
  // Deployment names carry no tier semantics.
  if (provider === 'azure_openai') return null

  const m = modelId.toLowerCase()
  const ts = tokens(m)
  if (ts.size === 0) return null

  if (ts.has('flash') && FLASH_IS_BUDGET_BRANDS.some((b) => hasBrand(ts, b))) {
    return 'low'
  }

  for (const keyword of LOW_KEYWORDS) {
    if (ts.has(keyword)) return 'low'
  }
  for (const keyword of HIGH_KEYWORDS) {
    if (ts.has(keyword)) return 'high'
  }
  for (const keyword of MID_KEYWORDS) {
    if (ts.has(keyword)) return 'mid'
  }
  const glmVer = GLM_VERSION.exec(m)
  if (glmVer && parseFloat(glmVer[1]) >= 5.3) return 'high'
  if (BRAND_DEFAULT_MID.some((b) => hasBrand(ts, b))) return 'mid'
  return null
}
