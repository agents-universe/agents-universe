import { describe, it, expect } from 'vitest'
import { inferTier } from './modelTier'

/**
 * Keep in sync with the Python original tests in
 * packages/api/tests/test_model_tier.py.
 */

describe('inferTier — Anthropic', () => {
  it('maps the four Claude families', () => {
    expect(inferTier('anthropic', 'claude-haiku-4-5')).toBe('low')
    expect(inferTier('anthropic', 'claude-sonnet-5')).toBe('mid')
    expect(inferTier('anthropic', 'claude-opus-5')).toBe('high')
    expect(inferTier('anthropic', 'claude-fable-5')).toBe('high')
    expect(inferTier('anthropic', 'claude-3-5-sonnet')).toBe('mid')
  })
})

describe('inferTier — OpenAI', () => {
  it('maps the 5.6 generation tiers', () => {
    expect(inferTier('openai', 'gpt-5.6-luna')).toBe('low')
    expect(inferTier('openai', 'gpt-5.6-terra')).toBe('mid')
    expect(inferTier('openai', 'gpt-5.6-sol')).toBe('high')
    expect(inferTier('openai', 'gpt-5.6-sol-pro')).toBe('high')
  })

  it('maps legacy suffixes and reasoners', () => {
    expect(inferTier('openai', 'gpt-4o')).toBe('mid')
    expect(inferTier('openai', 'gpt-4o-mini')).toBe('low')
    expect(inferTier('openai', 'gpt-5.4-nano')).toBe('low')
    expect(inferTier('openai', 'o3-mini')).toBe('low')
  })

  it('weakest matching keyword wins in compound names', () => {
    expect(inferTier('openai', 'gpt-4o-mini')).toBe('low')
    expect(inferTier('openai', 'gpt-4o-pro')).toBe('high')
  })
})

describe('inferTier — Gemini', () => {
  it('flash is the balanced tier (unlike DeepSeek/Qwen/Doubao)', () => {
    expect(inferTier('google_gemini', 'gemini-3.1-pro')).toBe('high')
    expect(inferTier('google_gemini', 'gemini-3.6-flash')).toBe('mid')
    expect(inferTier('google_gemini', 'gemini-3.5-flash-lite')).toBe('low')
  })
})

describe('inferTier — DeepSeek / Qwen (flash = budget tier)', () => {
  it('deepseek flash resolves low', () => {
    expect(inferTier('openai', 'deepseek-v4-pro')).toBe('high')
    expect(inferTier('openai', 'deepseek-v4-flash')).toBe('low')
    expect(inferTier('openai', 'deepseek-reasoner')).toBe('high')
    expect(inferTier('openai', 'deepseek-chat')).toBe('mid')
  })

  it('qwen flash resolves low', () => {
    expect(inferTier('openai', 'qwen3.7-max')).toBe('high')
    expect(inferTier('openai', 'qwen3.7-plus')).toBe('mid')
    expect(inferTier('openai', 'qwen3.6-flash')).toBe('low')
    expect(inferTier('openai', 'qwen-turbo')).toBe('low')
    expect(inferTier('openai', 'qwen-3.5-omni')).toBe('mid')
  })
})

describe('inferTier — Chinese domestic brands (conservative mid default)', () => {
  it('brand defaults and explicit keywords', () => {
    expect(inferTier('openai', 'kimi-k2-thinking')).toBe('high')
    expect(inferTier('openai', 'kimi-k3')).toBe('mid')
    expect(inferTier('openai', 'glm-5.3')).toBe('high')
    expect(inferTier('openai', 'glm-5.3-air')).toBe('low')
    expect(inferTier('openai', 'glm-5.2')).toBe('mid')
    expect(inferTier('openai', 'glm-4-air')).toBe('low')
    expect(inferTier('openai', 'doubao-seed-2.1-pro')).toBe('high')
    expect(inferTier('openai', 'doubao-seed-2.1-turbo')).toBe('low')
    expect(inferTier('openai', 'hunyuan-hy3')).toBe('mid')
    expect(inferTier('openai', 'minimax-m1')).toBe('mid')
  })
})

describe('inferTier — Azure / unknown / case', () => {
  it('azure deployment names never infer', () => {
    expect(inferTier('azure_openai', 'gpt-4o-mini-deployment')).toBeNull()
    expect(inferTier('azure_openai', 'claude-sonnet')).toBeNull()
  })

  it('is case-insensitive', () => {
    expect(inferTier('openai', 'GPT-4O-MINI')).toBe('low')
    expect(inferTier('anthropic', 'Claude-Opus-5')).toBe('high')
  })

  it('llama 70b is mid', () => {
    expect(inferTier('openai', 'llama-3.3-70b')).toBe('mid')
  })

  it.each(['my-custom-model', 'gpt-5.6'])('returns null for unrecognized %s', (modelId) => {
    expect(inferTier('openai', modelId)).toBeNull()
  })
})
