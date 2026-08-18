import MarkdownIt from 'markdown-it'
import hljs from 'markdown-it-highlightjs'

const md = new MarkdownIt({ html: false, linkify: true, typographer: true })
  .use(hljs, { auto: true })

const mdKnowledge = new MarkdownIt({ html: false, linkify: true, typographer: true, breaks: true })
  .use(hljs, { auto: true })

// Open external links in a new tab: a plain <a> would navigate the SPA away
// mid-stream and lose the editor draft and in-flight streaming state.
// knowledge-link anchors are in-app navigation and keep default behavior.
function openExternalLinks(instance: MarkdownIt) {
  const defaultLinkOpen = instance.renderer.rules.link_open
  instance.renderer.rules.link_open = (tokens, idx, options, env, self) => {
    const token = tokens[idx]
    if (!token.attrGet('class')?.includes('knowledge-link')) {
      token.attrSet('target', '_blank')
      token.attrSet('rel', 'noopener noreferrer')
    }
    // link_open is not pre-defined in this markdown-it version — fall back
    // to the stock renderToken behavior.
    return defaultLinkOpen
      ? defaultLinkOpen(tokens, idx, options, env, self)
      : self.renderToken(tokens, idx, options)
  }
}

openExternalLinks(md)
openExternalLinks(mdKnowledge)

// Mark mermaid blocks in knowledge renderer too
const defaultFenceKnowledge = mdKnowledge.renderer.rules.fence!
mdKnowledge.renderer.rules.fence = (tokens, idx, options, env, self) => {
  const token = tokens[idx]
  if (token.info.trim() === 'mermaid') {
    const code = token.content.trim()
    return `<pre class="mermaid-block" data-code="${encodeURIComponent(code)}"></pre>`
  }
  return defaultFenceKnowledge(tokens, idx, options, env, self)
}

// Mark mermaid blocks for the MessageBubble component to detect
const defaultFence = md.renderer.rules.fence!
md.renderer.rules.fence = (tokens, idx, options, env, self) => {
  const token = tokens[idx]
  if (token.info.trim() === 'mermaid') {
    const code = token.content.trim()
    return `<pre class="mermaid-block" data-code="${encodeURIComponent(code)}"></pre>`
  }
  return defaultFence(tokens, idx, options, env, self)
}

// Turn [[slug]] into real anchor tokens instead of injecting raw HTML into the
// source. mdKnowledge has html: false, so injected <a> tags were escaped and
// v-html rendered them as literal text (links invisible AND unclickable).
// Emitting tokens keeps html: false — raw HTML in knowledge content stays
// escaped — while producing working anchors.
mdKnowledge.inline.ruler.push('knowledge_links', (state, silent) => {
  const pos = state.pos
  if (state.src.charCodeAt(pos) !== 0x5b || state.src.charCodeAt(pos + 1) !== 0x5b) return false
  const end = state.src.indexOf(']]', pos + 2)
  if (end < 0 || end === pos + 2) return false
  const slug = state.src.slice(pos + 2, end)
  if (!silent) {
    const open = state.push('link_open', 'a', 1)
    open.attrs = [['class', 'knowledge-link'], ['data-slug', slug], ['href', '#']]
    const label = state.push('text', '', 0)
    label.content = slug
    state.push('link_close', 'a', -1)
  }
  state.pos = end + 2
  return true
})

export function renderMarkdown(src: string): string {
  return md.render(src)
}

// Render [[slug]] cross-links
export function renderKnowledgeMarkdown(src: string, onSlugClick?: (slug: string) => void): string {
  void onSlugClick // handled by click delegation in component
  return mdKnowledge.render(src)
}
