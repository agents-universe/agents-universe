---
category: technical
slug: technical/evolution-mapping
tags: [mapping, diff, migration]
template_words: 107
title: 新旧差异对照
---

# 新旧差异对照

新旧系统逐要素对照的**权威矩阵**，迁移正确性靠它保证：先定对照、再实施、实施后标状态。本文件以**旧要素为行**；新旧两侧的 schema 与契约细节以各自条目（架构/数据/接口）为权威，本表只记映射与处理策略。

## 对照总览

- 覆盖范围：(to be filled: 功能/页面/接口/数据表/任务哪些已纳入对照)
- 状态取值：`待定`（还没决定怎么对）/ `已定`（策略已评审）/ `已迁移`（新侧已验证）
- 维护规则：对照状态变更即在 [[system/history]] 追加一条；迁移排期以本表 `已定` 行为输入（见 [[domain/migration-plan]]）

## 功能与页面对照

| 旧功能/页面 | 新功能/页面 | 差异(保留/合并/下线/新增) | 处理策略 | 状态 |
|-------------|-------------|---------------------------|----------|------|
| (to be filled) | (to be filled) | (to be filled) | (to be filled) | (to be filled) |

- 旧侧清单依据 [[technical/legacy-architecture]] 与页面盘点；新侧依据 [[technical/target-architecture]]

## 业务流程对照

| 旧流程 | 新流程 | 处理策略(保留/优化/下线/新增) | 状态 |
|--------|--------|-------------------------------|------|
| (to be filled) | (to be filled) | (to be filled) | (to be filled) |

- 两侧流程细节分别见 [[domain/legacy-business-process]] 与 [[domain/target-business-process]]

## 接口对照

| 旧接口 | 新接口 | 变化(路径/字段/语义) | 兼容策略 | 状态 |
|--------|--------|----------------------|----------|------|
| (to be filled) | (to be filled) | (to be filled) | (to be filled) | (to be filled) |

- 旧侧行依据 [[technical/legacy-api-map]]，新侧契约见 [[technical/target-api-map]]

## 数据表对照

| 旧表 | 新表 | 变化 | 迁移方式(双写/ETL/重建) | 状态 |
|------|------|------|--------------------------|------|
| (to be filled) | (to be filled) | (to be filled) | (to be filled) | (to be filled) |

- 旧表特征与约束见 [[technical/legacy-data-model]]

## 定时任务与批处理对照

| 旧任务 | 新任务 | 调度变化 | 状态 |
|--------|--------|----------|------|
| (to be filled) | (to be filled) | (to be filled) | (to be filled) |

- (to be filled: 调度平台是否更换、依赖的输入输出表)

## 差异决策记录

| 决策 | 理由 | 影响面 | 日期 |
|------|------|--------|------|
| (to be filled) | (to be filled) | (to be filled) | (to be filled) |

- (to be filled: 与 [[domain/migration-plan]] 阶段的对应关系——哪个阶段消化哪些差异)

## 学习入口

- [[technical/legacy-architecture]]
- [[technical/legacy-data-model]]
- [[technical/legacy-api-map]]
- [[technical/target-architecture]]
- [[technical/target-api-map]]
- [[domain/legacy-business-process]]
- [[domain/target-business-process]]
- [[domain/migration-plan]]
