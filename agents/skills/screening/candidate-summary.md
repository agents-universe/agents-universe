---
slug: "screening/candidate-summary"
description:
  zh-CN: "简历初筛汇总报告模板：概览→评分表→shortlist→待确认项→附件路径清单，写入 reports/screening-{date}.md 并 deliver_file 交付"
  en-US: "Screening summary report template: overview → score table → shortlist → open questions → attachment path list; written to reports/screening-{date}.md and delivered via deliver_file"
type: "template"
triggers:
  - "screening report"
  - "candidate summary"
  - "初筛报告"
  - "候选人汇总"
  - "汇总报告"
tools:
  - filesystem
  - deliver_file
---

# Skill: Candidate Summary (Template)

Produces the final screening report from a scored batch. Scoring itself follows `screening/resume-screening` — this skill only owns the output contract.

## Trigger Conditions

- The user asks for a screening report / summary / shortlist write-up.
- The `resume-initial-screening` workflow reaches its reporting step.

## Template

Write the report to `reports/screening-{YYYY-MM-DD}.md` (ASCII filename; date = today), then `deliver_file` it. Fill every section; write `—` for an empty section rather than deleting it.

```markdown
# 简历初筛报告 — {date}

## 概览
- 岗位: {role}
- 收件来源: {mailbox slug}（{n} 封候选邮件，{m} 份简历）
- 评分标准: {rubric source — knowledge file / user-confirmed / defaults}
- 批次时间范围: {since} → {now}

## 评分表
| 候选人 | 技能匹配 | 经验深度 | 学历 | 语言 | 总分 | 关键依据 |
|---|---|---|---|---|---|---|
| 张三 | 5 | 4 | 4 | 3 | 4.0 | "精通 Python…" |

## Shortlist
1. {name} — {overall} — {one-line reason}
2. ...

## 待确认项
- {open question for the user / hiring manager}
- ...

## 附件路径
- {name}: .tmp/mail/{cid}/{file}（已解析 / 未解析:原因）

## 未读取/未处理
- {file or mail} — {reason: unreadable | encrypted | skipped | duplicate}
```

## Rules

- Every 总分 cell is the mean from `screening/resume-screening`; every 关键依据 is a verbatim quote (or `[inferred]` marker) — no new scoring happens in this skill.
- Attachment paths are copied exactly from intake/doc_reader results so the user can open them.
- No protected attributes anywhere in the report (see the anti-bias guardrails).
- The delivered report must not contain mailbox passwords or raw credential-bearing errors — those are redacted upstream; do not reintroduce them.
- Chat reply after delivery: shortlist names + total count + the download link quoted verbatim from `deliver_file`, not the full report body.
