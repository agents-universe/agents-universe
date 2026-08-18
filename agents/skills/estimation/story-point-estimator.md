---
slug: "estimation/story-point-estimator"
description: "Estimate story points for Jira stories by splitting front-end and back-end effort, using API additions and new pages as the baseline unit"
type: "guidance"
triggers: []
---

# Skill: Story Point Estimator

## When to Use

Apply whenever listing, drafting, or reviewing Jira stories — estimate points inline as part of the output, not as a separate step.

## Estimation Rules

Story points split into two independent dimensions — **Backend (BE)** and **Frontend (FE)** — reported separately on the story card and combined into a total.

### Backend Baseline

| Work Type | Points |
|---|---|
| One new API endpoint (create / update / delete / query) | 1.0 |
| Minor variation on an existing endpoint (add 1-2 fields, adjust validation) | 0.5 |
| Background job / batch process equivalent to one endpoint's complexity | 1.0 |
| DB schema change only (no endpoint, no logic) | 0.5 |
| No backend change | 0 |

**Scaling rules (BE):**
- Each additional independent API endpoint adds another 1.0.
- Complex business logic, multi-step orchestration, or external integration on a single endpoint → add 0.5.
- Max BE per story without splitting: 3.0. If higher, recommend decomposing the story.

### Frontend Baseline

| Work Type | Points |
|---|---|
| One new page / view (route, layout, primary components) | 1.0 |
| New modal, drawer, or major section added to an existing page | 0.5 |
| Minor UI change on existing page (field, button, styling tweak) | 0.5 |
| No frontend change | 0 |

**Scaling rules (FE):**
- Each additional new page adds 1.0.
- Complex interactive behavior (multi-step wizard, real-time updates, drag-and-drop) on a single page → add 0.5.
- Max FE per story without splitting: 3.0. If higher, recommend decomposing the story.

### Allowed Point Values

`0`, `0.5`, `1.0`, `1.5`, `2.0`, `2.5`, `3.0`

No other values. Round to the nearest 0.5.

## Output Format

When listing stories, append the estimate inline after the story summary:

```
| Story | BE | FE | Total | Notes |
|---|---|---|---|---|
| As a user, I can reset my password via email | 1.0 | 1.0 | 2.0 | 1 API + 1 new page |
| Add "last login" field to profile page | 0.5 | 0.5 | 1.0 | minor endpoint change + UI field |
| Background job to purge expired sessions | 1.0 | 0 | 1.0 | BE only |
```

When drafting a single story for Jira, include a `## Story Points` section:

```
## Story Points
- Backend: 1.0 (1 new POST /api/... endpoint)
- Frontend: 1.0 (1 new page: /settings/reset-password)
- **Total: 2.0**
```

## Estimation Assumptions

- If the story description does not mention UI, default FE to 0 and note the assumption.
- If it does not mention any API or data change, default BE to 0 and note the assumption.
- If scope is unclear, state the assumption explicitly: `[assumption: FE = 0.5, modal only]`.
- Never silently assign 0 to a dimension that could plausibly have work — flag it.

## Decomposition Trigger

If the estimated total exceeds **4.0**, recommend splitting the story into sub-stories before creating it in Jira, with a suggested decomposition outline.
