---
category: technical
slug: technical/target-api-map
tags: [api, design, target-state]
template_words: 61
title: 新系统接口
---

# 新系统接口

新接口契约的**权威文件**，以新接口为行。旧接口盘点在 [[technical/legacy-api-map]]；迁移视角的新旧逐条对照矩阵在 [[technical/evolution-mapping]]——本文件记「新接口长什么样」，对照表记「旧的怎么变过来」，互为指针不重复记事实。

## 接口设计原则

- (to be filled: 风格——REST/RPC/GraphQL，资源与命名约定)
- (to be filled: 版本策略、兼容性承诺、废弃流程)
- (to be filled: 鉴权方式、错误码体系、分页与幂等约定)

## 新接口目录

| METHOD/路径 | 用途 | 服务 | 状态 | 对应旧接口 |
|-------------|------|------|------|------------|
| (to be filled) | (to be filled) | (to be filled) | (to be filled: 设计中/已实现) | (to be filled: legacy-api-map 行) |

- (to be filled: 分组方式——按服务/按业务域)
- 接口量大时按两级结构拆明细：本文件作索引，详情建 `technical/api/{service}` 文件（规则见 knowledge-manager skill）
- 「对应旧接口」列填旧接口路径，完整兼容语义在 [[technical/evolution-mapping]] 接口对照表

## 鉴权与网关

- (to be filled: 鉴权来源与令牌模型、网关路由、限流与超时)
- (to be filled: 内外部接口的边界与暴露策略)

## 旧接口替代关系

| 旧接口 | 新接口 | 兼容策略 |
|--------|--------|----------|
| (to be filled) | (to be filled) | (to be filled: 直接替换/过渡期双活/适配层) |

- 本表只记替代关系摘要；逐条字段与语义变化以 [[technical/evolution-mapping]] 为准
- 旧接口的消费方清单见 [[technical/legacy-api-map]]

## 变更记录

| 日期 | 变更 | 评审 |
|------|------|------|
| (to be filled) | (to be filled) | (to be filled) |

- 契约变更必须同步 evolution-mapping 对照状态，并在 [[system/history]] 追加一条

## 学习入口

- [[technical/target-architecture]]
- [[technical/legacy-api-map]]
- [[technical/evolution-mapping]]
- [[domain/migration-plan]]
- [[integrations/custom-api]]
- (to be filled: OpenAPI 生成物、契约测试仓库、API 评审文档)
