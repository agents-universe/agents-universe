---
slug: "screening/resume-screening"
description:
  zh-CN: "简历初筛评分：按项目知识/用户确认的 rubric 从技能匹配、经验深度、学历、语言四维 1–5 分打分，每分必须引用简历原文，缺口标 [inferred]，只评岗位相关项不用受保护特征"
  en-US: "Resume screening scores 1–5 across skill fit, experience depth, education and language per the project/user rubric; every score cites resume text, gaps are marked [inferred], only job-related criteria count — never protected attributes"
triggers:
  - "screen resumes"
  - "resume screening"
  - "shortlist"
  - "简历"
  - "resume"
  - "简历筛选"
  - "初筛"
  - "筛选简历"
  - "给候选人打分"
tools:
  - doc_reader
  - filesystem
  - knowledge_rw
  - user_confirm
---

# Skill: Resume Screening

Scoring rules for candidate resumes. Parsed resume text comes from `doc_reader` (workspace or `.tmp/mail/{cid}/` paths produced by the email intake step).

## Trigger Conditions

- The user asks to screen / score / rank / shortlist resumes.
- The intake step has produced a batch of parsed resumes and the workflow reaches evaluation.

## Standard Sources (priority order)

1. **Project knowledge** — an existing rubric or job description in `knowledge/` (read with `knowledge_rw` first if the project has one).
2. **User-confirmed rubric** — if none exists, draft the dimensions and thresholds, confirm with `user_confirm`, then apply them to the whole batch consistently.
3. **This skill's defaults** — only when the user explicitly says to use defaults without confirmation.

Never change the rubric mid-batch. If a resume is unreadable (scanned image PDF, corrupt file), mark it `unreadable` and move on — do not guess at its contents.

## Scoring Dimensions (defaults, 1–5 each)

| Dimension | What it measures |
|---|---|
| Skill fit | Required skills/tools from the job description present in the resume |
| Experience depth | Relevant scope, ownership, progression — not just years |
| Education | Degree / field match against the requirement |
| Language | Language proficiency stated in the resume |

- **Evidence rule**: every score cites a verbatim quote from the resume (short snippet is enough). A score without a quote is invalid — mark `[inferred]` when the resume does not state the fact and you are inferring it.
- **Gaps**: missing information is `[inferred]` or `not stated`, never a silent assumption. Absent required evidence caps the dimension at 3.
- **Aggregation**: overall = mean of the four dimensions, shown to 1 decimal. Shortlist threshold defaults to ≥ 3.5 or the user's stated cutoff.

## Anti-bias Guardrails

- Score only job-related criteria listed in the rubric.
- Never use or mention protected attributes: age, gender, nationality/ethnicity, marital status, political/religious affiliation, health status, disability, photograph appearance, or name-based origin guesses.
- If the resume leads with such attributes, ignore them entirely and do not note them in the report.
- A gap in information is scored as a gap, not as a negative on the person.

## Output Format (per candidate)

```
### <name> — overall 4.0
- Skill fit: 5 — "精通 Python 与分布式系统，主导过日均 10 亿消息的管道"
- Experience depth: 4 — "3 年后端，独立负责计费服务" [inferred: seniority not stated]
- Education: 4 — "计算机科学 本科"
- Language: 3 — "CET-6"
- Gaps: no salary expectations; employment gap 2024-01→2024-06 unexplained
```

End with a shortlist table (name, overall, one-line reason) and an `unreadable` list if any.

## Error Handling

- Resume file not found or expired (upload-store path) → ask the user to re-attach; email-downloaded paths under `.tmp/mail/` persist for the conversation and should be re-used instead.
- Encrypted PDF → report "PDF is encrypted" and ask the user for an unlocked copy.
- Rubric disagreement → re-confirm with `user_confirm` before rescoring the batch.
