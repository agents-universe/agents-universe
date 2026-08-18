import { describe, expect, it } from 'vitest'

import { renderKnowledgeMarkdown, renderMarkdown } from './markdown'

describe('renderKnowledgeMarkdown', () => {
  it('turns [[slug]] into a real knowledge-link anchor', () => {
    const html = renderKnowledgeMarkdown('See [[system/rules]] for details')
    expect(html).toContain('<a class="knowledge-link" data-slug="system/rules" href="#">system/rules</a>')
    // regression guard: html:false must not escape the injected anchor into
    // literal text (links were invisible and unclickable before)
    expect(html).not.toContain('&lt;a')
  })

  it('escapes slug attribute values', () => {
    const html = renderKnowledgeMarkdown('[[a"b&c]]')
    // escapeAttr output survives rendering: quotes/ampersands become entities
    expect(html).toContain('a&quot;b&amp;c')
    expect(html).not.toContain('data-slug="a"b&c"')
  })
})

describe('renderMarkdown', () => {
  it('marks mermaid code blocks for the client', () => {
    const html = renderMarkdown('```mermaid\ngraph TD\n```')
    expect(html).toContain('class="mermaid-block"')
    expect(html).toContain('data-code="graph%20TD"')
  })

  it('does not allow raw html', () => {
    const html = renderMarkdown('<script>alert(1)</script>')
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
  })
})
