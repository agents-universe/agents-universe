# web Package

Vue 3 + TypeScript + Vite. Codex-style three-panel layout.

## Stack

- 框架: Vue 3.5 + `<script setup>` + TypeScript
- 状态管理: Pinia 2 (`stores/`)
- 路由: Vue Router 4 (`router/`)
- 构建: Vite 5 + @vitejs/plugin-vue
- 样式: Tailwind CSS 3 + 手写 CSS（`index.css` + `styles/layout.css`）
- Markdown: markdown-it + markdown-it-highlightjs
- 图标: lucide-vue-next
- 编辑器: CodeMirror 6（`Composer.vue`）
- 图表: mermaid + dompurify（`MermaidBlock.vue`）
- HTTP: 原生 fetch 封装（`api/client.ts`）

## Layout

```
AppLayout.vue
├── Left sidebar (collapsible)
│   ├── ProjectTree.vue      — 项目列表，点击切换
│   └── AgentSwitcher.vue    — 智能体列表，点击切换
│
├── Center panel
│   └── ChatPage.vue → ChatPanel.vue
│       ├── 消息列表 (MessageBubble.vue)
│       └── Composer.vue (底部固定)
│
└── Right panel (collapsible, tabbed)
    ├── Tab: 会话  — ConversationTreePanel.vue
    ├── Tab: 知识  — KnowledgePanel.vue
    └── Tab: 记忆  — MemoryPanel.vue
    (ContextMeter.vue 始终显示在顶部)
```

## State Management

Pinia stores in `stores/`. **不要用 Vue provide/inject** 做跨面板状态。

- `project.ts` — 当前工作区、项目、项目列表；切换项目时 reset 其他 stores
- `conversation.ts` — 消息、流式内容、Token 用量、任务列表、工具调用
- `knowledge.ts` — 知识文件、完整度、本轮加载、动态加载
- `agent.ts` — 智能体列表、当前智能体、模型配置
- `memory.ts` — 会话笔记、个人记忆、情节记忆
- `auth.ts` — 用户信息、认证状态

**项目切换**时 `projectStore.setCurrentProject()` 自动 reset conversation/knowledge/memory stores。

## WebSocket

`composables/useWebSocket.ts` 接收 `conversationId: Ref<string | null>`：
- `watch(conversationId)` 管理连接生命周期
- 指数退避重连（3 次，1s/2s/4s）
- 分发 ~20 种事件类型到 Pinia stores
- 返回 `{ send, abort, status }`

## Composer (`components/chat/composer/`)

使用 **CodeMirror 6** — 不是 `<textarea>`。关键行为：
- `Ctrl/Cmd + Enter` 发送；`Enter` 换行
- `@` 触发 `MentionPopup.vue`（搜索知识 + 个人记忆）
- `/` 触发 `SlashPopup.vue`（内置命令列表）

## API Layer

`api/` 为类型化 fetch 封装，统一使用 `apiFetch<T>`（`credentials: include`）。

## Token Meter

`ContextMeter.vue` 读取 `conversationStore` 的 `tokensUsed/tokenBudget`。颜色阈值：
- < 75%: 蓝色
- 75–90%: 琥珀色
- > 90%: 红色

## 独立运行

完全自包含在 `packages/web/`，不依赖 monorepo 其他包：
```bash
cd packages/web
npm install
npm run dev    # 开发服务器
npm run build  # 生产构建（类型检查 + vite build）
```
API 地址通过 `.env` 中的 `VITE_API_BASE_URL` 配置（默认 `http://127.0.0.1:8000`）。

## Testing

```bash
npm run test      # Vitest
npm run test:ui   # Vitest UI
npm run build     # vue-tsc + vite build
```
