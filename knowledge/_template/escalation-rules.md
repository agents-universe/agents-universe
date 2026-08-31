---
category: domain
slug: domain/escalation-rules
tags: [escalation, customer-service, handoff]
template_words: 33
title: 转人工规则
---

# 转人工规则

项目级转人工配置。通用触发条件由技能 support/escalation 定义，本文件记录本项目的具体阈值、通道与指标目标。

## 转人工触发条件

- **用户明确要求转人工** — (to be filled: 识别口径与引导话术)
- **低置信度** — 知识库无答案或答案相互矛盾：(to be filled: 置信度阈值)
- **高风险问题** — 支付纠纷、账户安全、法律、合规类：(to be filled: 具体范围)
- **情绪恶化** — 用户明确不满或投诉：(to be filled: 判断标准)
- **重复失败** — 同一问题解答多次仍不解决：(to be filled: 失败次数阈值)
- **对话无进展** — 多轮仍未推进：(to be filled: 轮次阈值)

## 人工通道与联系方式

(to be filled: 转人工渠道、入口、工作时间、响应时限)

未配置人工通道时：如实告知用户当前无人接听，建议留言或稍后再试；不得虚构通道。

## 交接信息要求

转人工时输出以下信息（有则填，无则省略）：

1. 对话摘要（2-3 句）
2. 客户意图
3. 已尝试的解答
4. 情绪状态
5. 相关信息（订单号、账号等）

## 服务指标目标

- 首次响应准确率 ≥ 85%
- 转人工率 20%–40%
- CSAT ≥ 4.0

(to be filled: 项目实际目标与考核口径)

## Related Knowledge

- [[domain/faq]]
- [[domain/service-policies]]
- [[system/history]]
