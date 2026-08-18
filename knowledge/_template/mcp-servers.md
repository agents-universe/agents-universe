---
category: integrations
slug: integrations/mcp-servers
tags: [integration, mcp, tools]
title: MCP Server Integrations
---

# MCP Server Integrations

Defines how agents connect to external **MCP (Model Context Protocol)** servers and use their tools.

When an agent declares `mcp` (or `mcp:<slug>`) in its `tools:` list, the framework reads the MCP server registry, connects to each enabled server, discovers its tools, and injects them at runtime as `mcp__<server_slug>__<tool_name>`.

## Two-Tier Registry

MCP servers have two tiers, merged at runtime with the same shadowing rule as agents/skills/workflows:

- **全局服务器** — stored in the `mcp_servers` table (`project_id` NULL), managed in **Settings → 外部集成 → MCP 服务器**（本页表单 + 测试连接）。跨项目生效，密钥从当前用户的**用户密钥**（user_tokens）解析。
- **项目服务器** — defined in **this file** (`integrations/mcp-servers.md`)；每次对话开始时与列表接口读取时懒同步进 `mcp_servers` 表（项目行），密钥从**项目密钥**（project_secrets）解析，缺省回退用户密钥。
- **同 slug 遮蔽** — 项目条目与全局条目同名（按运行时规范化 slug）时，**项目条目覆盖全局条目**，与项目 agent/skill/workflow 遮蔽规则一致。
- 项目级文件的变更由外部系统集成专家写入，保存后下次对话/列表即生效，无需重启。

**Security rules:**
- Never store plaintext secrets here or in any knowledge/memory - use only `secret_ref` values.
- Secrets are stored as project secrets (or user tokens), resolved server-side during connection.
- Never put credentials in `headers` - use `auth.secret_ref` instead.
- Tools with `destructiveHint` (or matching `require_confirmation`) trigger a user confirmation prompt before execution.
- `redact_response` redacts only dict *fields* with sensitive names (token/secret/password...) in tool results; free-form text content is passed through as-is, so avoid returning secrets from MCP tools in text form.

## Server Configuration Template

Copy this block per MCP server:

```yaml
servers:
  - slug: <unique-slug>            # e.g. github-copilot; tools become mcp__<sanitized>__*
    name: <Human-readable name>
    enabled: false                 # set to true when ready to connect (placeholder server stays disabled)
    transport: auto                # auto (streamable_http -> SSE fallback) | streamable_http | sse | stdio (reserved)
    url: https://<mcp-server>/mcp
    allowed_hosts:                 # optional; when set, the URL host must match one entry
      - <mcp-server>
    allow_private_network: false   # set true for internal MCP servers (bypasses private-IP block)
    auth:
      type: bearer                 # bearer | header | none (default none)
      secret_ref: mcp:<slug>       # service_key in project_secrets / user_tokens
      secret_scope: project        # project (default, falls back to user_tokens) | user
      header_name: Authorization   # for type=header; bearer is always Authorization
      value_template: "Bearer {secret}"  # optional; bearer defaults to "Bearer {secret}"
    headers:                       # non-sensitive extra headers (sensitive names are rejected)
      X-Team: platform
    connect_timeout_seconds: 10
    call_timeout_seconds: 60       # per call_tool; hard cap 300
    tools:
      allowlist: ["search_*"]      # optional glob patterns; empty = allow all
      denylist: ["delete_*"]       # deny takes precedence over allow
      require_confirmation: ["delete_*"]  # glob; matched tools always prompt
    require_confirmation_for_write: true  # auto-confirm when tool annotations set destructiveHint
    redact_response: true          # redact sensitive FIELDS (token/secret/password keys) in JSON-ish
                                   # results; free-form text content is passed through as-is
```

## Usage Rules

1. **Enable per agent**: add `mcp` (all enabled servers) or `mcp:<slug>` (specific server) to the agent's `tools:` frontmatter.
2. **Tool naming**: discovered tools appear as `mcp__<server>__<tool>` - the prefix avoids collisions with built-in tools.
3. **Secret management**: store the MCP server's API key/token in **Project Secrets** (Settings panel) using the `secret_ref` value (e.g. `mcp:github-copilot`) as the service key.
4. **Tool filtering**: use `allowlist`/`denylist` with glob patterns to control which tools are exposed to the agent.
5. **Write protection**: tools that modify state should have `require_confirmation` entries or rely on `require_confirmation_for_write: true` (which checks the server's `destructiveHint` annotation).
6. **Troubleshooting**:
   - Server not connecting? Check the `warning` event in the chat (MCP failures never block the conversation).
   - Tool not appearing? Verify `enabled: true`, check `allowlist`/`denylist` patterns.
   - Auth failing? Confirm the secret is stored with the exact `secret_ref` key in Project Secrets.
