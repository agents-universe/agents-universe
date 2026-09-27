/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_PATH?: string
  readonly VITE_BASE_PATH?: string
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}

declare module 'markdown-it-highlightjs/core' {
  import type MarkdownIt from 'markdown-it'
  const plugin: (
    md: MarkdownIt,
    options?: {
      hljs?: unknown
      auto?: boolean
      code?: boolean
      ignoreIllegals?: boolean
    },
  ) => void
  export default plugin
}
