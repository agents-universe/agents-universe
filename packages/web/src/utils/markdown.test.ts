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

  it('highlights registered languages and leaves unknown languages as text', () => {
    const highlighted = renderMarkdown('```python\nprint("ready")\n```')
    const plain = renderMarkdown('```unknown-language\nprint("ready")\n```')
    expect(highlighted).toContain('<span class="hljs-built_in">print</span>')
    expect(plain).toContain('print(&quot;ready&quot;)')
    expect(plain).not.toContain('<span class="hljs-')
  })

  it.each([
    // shell/console are the "Shell Session" grammar — content is prompt lines.
    ['shell', '$ echo hello'],
    ['console', '$ export PATH=$PATH:/usr'],
    ['xml', '<root attr="1"/>'],
    ['go', 'func main() {}'],
    ['java', 'class Demo {}'],
    ['cpp', 'int main() { return 0; }'],
  ])('highlights %s fences', (lang, code) => {
    // These were silently plain text when only the default subset was
    // registered against the tree-shaken highlight.js bundle.
    const html = renderMarkdown('```' + lang + '\n' + code + '\n```')
    expect(html).toContain('<span class="hljs-')
  })

  it('resolves the html alias to the xml grammar', () => {
    const html = renderMarkdown('```html\n<div class="x"></div>\n```')
    expect(html).toContain('<span class="hljs-tag"')
  })

  it('does not allow raw html', () => {
    const html = renderMarkdown('<script>alert(1)</script>')
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
  })
})
