---
category: technical
slug: technical/legacy-resources
tags: [legacy, resources, repos, integration]
template_words: 133
title: 旧系统资源
---

# 旧系统资源

旧系统各类资源的接入清单：代码仓库、文档、CI/CD、数据库等。每项资源登记「地址 + 接入方式 + 凭据引用」，让智能体能直接连上去干活；本文件是接入索引，资源内容本身沉淀到各自链接的知识条目。

## 接入原则

- 凭据一律用 secret_ref 引用 project_secrets 或 user_tokens，**禁止明文写入本文件或任何知识文件**
- 每接入一项资源，在对应表里补一行：地址、接入方式、凭据引用、负责人
- 接不上的资源标注原因（权限/网络/无凭据），迁移排期时作为前置依赖

## 代码仓库

| 仓库 | git 主机(服务) | clone 名 | 默认分支 | 接入方式 | 备注 |
|------|----------------|----------|----------|----------|------|
| (to be filled) | (to be filled: Settings→Integrations 的 git 服务) | (to be filled: → `repos/{name}`) | (to be filled) | (to be filled) | (to be filled) |

- 接入方式写法：git 仓库 → `git_repo operation=clone`，克隆后自动构建代码图谱（`repo_graph`）；超大仓（>3000 源文件）需手动 `repo_graph build`
- 非 git 源码树：文件放入工作区后 shell `git init/add/commit`，再 `repo_graph build`（`repository_path` 指向该目录）
- 长期依赖的仓库按 git-repo-reader skill 约定另建 `technical/repo-{name}` 明细条目；本表保持索引

## 文档与知识源

| 资源 | 类型 | 地址/位置 | 接入方式 | 备注 |
|------|------|-----------|----------|------|
| (to be filled) | (to be filled: Confluence/wiki/共享盘) | (to be filled) | (to be filled) | (to be filled) |

- Confluence → `confluence` 工具 + confluence-reader skill；任意网页/URL → knowledge-ingestion 工作流（`web_fetch` 抓取后 `knowledge_rw` 入库）
- (to be filled: 需求文档、设计文档、运维手册、培训材料分别在哪)

## CI/CD 与制品库

| 系统 | 用途 | API 端点 | 接入方式 | 凭据引用 |
|------|------|----------|----------|----------|
| (to be filled) | (to be filled) | https://ci.example.com | (to be filled: custom-api + api_request) | (to be filled: secret_ref) |

- 流水线、制品、发布单统一走 `integrations/custom-api` 目录登记 + `api_request` 调用，凭据放 project_secrets

## 数据库与数据源

| 实例 | 类型 | 用途 | 接入方式 | 凭据引用 |
|------|------|----------|----------|----------|
| (to be filled) | (to be filled: MySQL/PG/SQLServer) | (to be filled) | (to be filled: data-source-onboarding 工作流) | (to be filled: `db:{name}:dsn`) |

- 表结构与数据特征见 [[technical/legacy-data-model]]；连接串经 data-source-onboarding 工作流收集进 project_secrets，明文不入知识

## 其他资源

- 工单/缺陷系统：(to be filled: Jira 项目键或同类系统地址，接入方式 jira 工具或 custom-api)
- 内部网关：(to be filled: 网关地址与路由配置位置，网关明细可经「复制系统模板」拉取 `technical/kong-map`)
- 监控告警/日志平台：(to be filled: 地址、查询方式)
- (to be filled: 其他——制品库、消息平台、第三方供应商门户)

## 学习入口

- [[technical/legacy-architecture]]
- [[technical/legacy-data-model]]
- [[technical/legacy-api-map]]
- [[integrations/custom-api]]
- [[integrations/mcp-servers]]
- [[integrations/skill-sources]]
- [[domain/migration-plan]]
