---
category: domain
slug: domain/migration-plan
tags: [migration, plan, cutover]
template_words: 58
title: 迁移改造计划
---

# 迁移改造计划

迁移的阶段、切换、并行、回滚与风险。逐要素对照细节在 [[technical/evolution-mapping]]，本文件管「什么时候、按什么顺序、出事怎么办」；对照表中状态为 `已定` 的行是排期输入。

## 目标与成功标准

- 业务目标：(to be filled: 为什么改造，做成什么样算赢)
- 完成定义：(to be filled: 全量切换、旧系统下线、数据封存各自的判定标准)
- (to be filled: 硬约束——截止日期、预算、停机窗口限制)

## 阶段划分

| 阶段 | 范围 | 入口/出口标准 | 时间窗 | 状态 |
|------|------|---------------|--------|------|
| (to be filled) | (to be filled) | (to be filled) | (to be filled) | (to be filled) |

- (to be filled: 阶段划分思路——按业务域分批/按流量分层/绞杀者路径)

## 切换策略

- (to be filled: 大爆炸/分批/绞杀者，选型理由)
- (to be filled: 流量切换步骤与观察指标——切多少、看什么、多久升到下一批)
- (to be filled: DNS/网关/客户端各层的切换动作与负责人)

## 并行运行与数据一致

- 并行期范围：(to be filled: 哪些流量双跑、持续多久)
- 对账方式：(to be filled: 对账口径与频率，逐表差异以 [[technical/evolution-mapping]] 数据表对照为清单)
- (to be filled: 双写期间冲突处理——以哪侧为准、如何补偿)

## 回滚方案

- 触发条件：(to be filled: 指标跌破什么值、谁有权叫停)
- 回滚步骤：(to be filled: 流量回切、配置回退、依赖服务回退的顺序)
- 数据回退：(to be filled: 并行期新侧写入的数据如何处理——这是最容易漏的一步)

## 风险与缓解

| 风险 | 影响 | 概率 | 缓解措施 | 负责人 |
|------|------|------|----------|--------|
| (to be filled) | (to be filled) | (to be filled) | (to be filled) | (to be filled) |

- (to be filled: 数据质量风险——清洗未完成就迁移；依赖风险——外部系统改造不同步)

## 进度与里程碑

| 里程碑 | 计划日期 | 实际日期 | 状态 |
|--------|----------|----------|------|
| (to be filled) | (to be filled) | (to be filled) | (to be filled) |

- 本表持续追加；变更在 [[system/history]] 留痕

## 相关方与分工

| 角色 | 人员 | 职责 |
|------|------|------|
| (to be filled) | (to be filled) | (to be filled) |

- (to be filled: 业务方签字人、旧系统维护方、新系统建设方的接口人)

## 学习入口

- [[domain/context]]
- [[technical/evolution-mapping]]
- [[technical/legacy-architecture]]
- [[technical/target-architecture]]
- [[technical/legacy-resources]]
- [[system/history]]
- (to be filled: 立项/评审文档、周报与例会纪要位置)
