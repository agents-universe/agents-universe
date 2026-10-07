---
category: technical
slug: technical/legacy-api-map
tags: [legacy, api, inventory]
template_words: 53
title: 旧系统接口
---

# 旧系统接口

旧系统**自身对外**的接口面盘点（外部系统依赖见 [[technical/legacy-architecture]]，新接口契约见 [[technical/target-api-map]]）。本文件是 [[technical/evolution-mapping]] 接口对照的旧侧行依据：每登记一个在用接口，对照表才有可引用的行。

## 接口分组清单

按业务域分组，每组一张表；一行一个接口：

| METHOD/路径 | 功能 | 消费方 | 认证方式 | 是否仍在用 |
|-------------|------|--------|----------|------------|
| (to be filled) | (to be filled) | (to be filled) | (to be filled) | (to be filled) |
| (to be filled) | (to be filled) | (to be filled) | (to be filled) | (to be filled) |

- (to be filled: 分组方式——按服务/按业务域/按网关路由前缀)
- 接口量大时按两级结构拆明细：本文件作索引，详情建 `technical/api/{service}` 文件（规则见 knowledge-manager skill）

## 认证与网关

- (to be filled: 鉴权来源——Session/JWT/签名/内网免鉴权，网关与路由前缀)
- (to be filled: 限流、IP 白名单、重试与超时约定)
- 内部网关路由明细如经「复制系统模板」拉取后维护在 `technical/kong-map`

## 废弃与影子接口

| 接口 | 废弃时间 | 替代接口 | 残留调用方 | 处理策略 |
|------|----------|----------|------------|----------|
| (to be filled) | (to be filled) | (to be filled) | (to be filled) | (to be filled) |

- (to be filled: 文档缺失但线上仍在用的「影子接口」——从网关日志/代码扫描发现的项)
- 每个废弃决策同步登记到 [[technical/evolution-mapping]] 的接口对照表

## 学习入口

- [[technical/legacy-architecture]]
- [[technical/target-api-map]]
- [[technical/evolution-mapping]]
- [[technical/legacy-resources]]
- [[integrations/custom-api]]
- [[domain/migration-plan]]
- (to be filled: OpenAPI/Swagger 地址、网关配置仓库、抓包或日志来源)
