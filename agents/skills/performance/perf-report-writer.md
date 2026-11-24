---
slug: "performance/perf-report-writer"
description:
  zh-CN: "将压测报告写入 performance/perf-report-{date}.md（固定 9 节：摘要、范围方法、场景、SLA、结果分位、瓶颈分析、建议、附录），再经 perf-report-pdf 转 PDF 并双双 deliver_file 交付"
  en-US: "Write the stress-test report to performance/perf-report-{date}.md (fixed 9 sections: summary, scope/methodology, profile, SLA, percentile results, bottleneck analysis, recommendations, appendix), then convert via perf-report-pdf and deliver both files via deliver_file"
type: "guidance"
triggers:
  - "stress test report"
  - "performance report"
  - "report delivery"
  - "压测报告"
  - "性能测试报告"
  - "出报告"
tools:
  - filesystem
  - deliver_file
  - chart_renderer
---

# Skill: Perf Report Writer

Phase 5 deliverable: the canonical Markdown report at `performance/perf-report-{YYYYMMDD}.md` in the project workspace, the metric series it references under `performance/results/`, and - after conversion by `agents/skills/performance/perf-report-pdf.md` - the PDF at `performance/perf-report-{YYYYMMDD}.pdf`. The MD is the authoritative greppable source; the PDF is what stakeholders read. Two audiences, one document - leadership reads sections 1-3, engineers read 4-8.

## Report Structure

Write the sections in this order:

1. **Cover & Metadata** - classification level, project, environment tier, test date, report version, generator (Python asyncio+httpx via `script_writer`), script name + run id(s), scenario design doc reference.
2. **Executive Summary** - business language: **SLA met?** headline p95 / error-rate / throughput, the top 1-3 bottlenecks as one-liners, the single most important recommendation. No raw payloads, no JSON.
3. **Scope & Methodology** - targets from the `PERF_SCOPE` declaration, ceilings actually applied, exclusions; methodology stance: black-box API load, knowledge-informed design; **tool channel + limits disclosed** (`script_writer` executor, 300 s cap, profiles ≤240 s, segmented runs if any).
4. **Test Profile** - the scenario matrix as *executed* (pattern, VUs, ramp, steady, data set, segment windows if split).
5. **SLA & Targets** - per endpoint: p95 / error-rate / throughput targets, each labeled `confirmed` or `proposal`.
6. **Results** - per endpoint: p50/p95/p99/max, RPS, ok/err counts, status-code breakdown; an aggregate row; Mermaid chart via `chart_renderer` (never paste Mermaid source). Every number comes from `performance/results/*.json` or the run's verdict - **never estimated**.
7. **Bottleneck Analysis** - evidence-based: latency growth vs ramp, error spikes, saturation symptoms; each claim cites the results JSON path or a run excerpt. "Within noise" is a valid and honest finding.
8. **Recommendations** - prioritized, actionable, effort-annotated (quick win / needs design change / needs capacity).
9. **Appendix** - `performance/results/*.json` pointer, script path, run ids, limitations (tool-limited runs, skipped segments, missing credentials), evidence index.

## Rules

1. **Factual and evidence-based** - every metric traces to a run artifact; every claim cites a result file or excerpt. Runs that hit the executor cap are disclosed as tool-limited, never glossed.
2. **Percentiles, not averages** - p95/p99 everywhere; if only aggregates survive, say so.
3. **Placeholders for sensitive values** - `[REDACTED]` for tokens/PII; real user data never.
4. **Language** - report body follows `AGENT_DEFAULT_LANGUAGE` from `environment` knowledge unless the user overrides; section titles keep their English canonical names for grep-ability and PDF conversion.
5. **Charts** - latency distribution or rps-over-time as Mermaid -> `chart_renderer` first; rendered PNG displays automatically; never paste Mermaid source or embed image markdown in the reply.
6. **SLA verdict is unambiguous** - section 2 states met/missed per endpoint in one sentence; section 6 carries the numbers that justify it. A proposal SLA that was never confirmed is reported as "observed vs proposal", not as a pass/fail.
7. **Evidence figures** - curated chart screenshots or result excerpts as `![](performance/...)` lines under `Evidence figures:`; paths must exist relative to the workspace - the PDF converter embeds exactly these and warns on broken paths.
8. **Self-check before converting** - all 9 sections in order, every Results row backed by a JSON pointer, figure paths exist, verdict matches the numbers.

## Delivery

1. `filesystem(operation="write_file", ...)` to `performance/perf-report-{YYYYMMDD}.md`.
2. Self-check per rule 8.
3. Convert to PDF per `agents/skills/performance/perf-report-pdf.md` (run its Converter Script via `code_executor`, clear every `WARN:`).
4. Deliver **both** files via `deliver_file` - the `.md` and the `.pdf` - two explicit calls; quote both URLs verbatim.
5. Chat reply: concise summary - SLA verdict per endpoint, headline p95/error-rate, top recommendation, both file paths. The report carries the detail, chat carries the decisions.
