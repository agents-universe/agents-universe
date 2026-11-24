---
slug: "marketing/copy-adaptation"
description:
  zh-CN: "推广文案适配——一份活动 brief 派生各渠道变体（长度、语气、CTA、话题标签），所有数字溯源项目知识，草稿落 promotion/campaign-*/copy/"
  en-US: "Promotion copy adaptation - derive per-channel variants (length, tone, CTA, hashtags) from one campaign brief, every number traced to project knowledge, drafts under promotion/campaign-*/copy/"
type: "guidance"
triggers:
  - "改写文案"
  - "渠道文案"
  - "推广文案"
  - "copy adaptation"
  - "adapt the copy"
  - "channel copy variants"
tools:
  - filesystem
  - knowledge_rw
---

# Skill: Copy Adaptation

One campaign, one source of truth, many channel-shaped variants. This skill turns the approved brief into per-channel copy without ever letting the message drift — or letting a number in.

## 1. The Campaign Brief (single source of truth)

Before writing any variant, author `promotion/campaign-{YYYYMMDD}/brief.md`:

- **Objective** — what this campaign moves (release adoption, sign-ups, event RSVPs).
- **Core message** — one or two sentences; the thing every variant must still say after trimming.
- **Facts** — version numbers, feature lists, metrics, dates, links — **each with its knowledge source** (`knowledge_rw` file it came from). No fact enters the brief without a source.
- **Audience** — from the channel strategy.
- **Do-not-say list** — legal/compliance exclusions, unannounced features, anything `domain/brand-guidelines` forbids.

Every channel variant is derived from this file. If a variant seems to need a new fact, add it to the brief first (with its source), then adapt.

## 2. Per-Channel Adaptation Rules

| Channel | Length | Tone | CTA | Extras |
|---|---|---|---|---|
| GitHub repo / release notes | concise, technical | precise, changelog-like | link to docs/upgrade path | keep conventional commit/release style |
| GitHub Pages site | full landing copy | public-facing, benefit-led | primary button (sign-up / try) | headline + proof points + links |
| Email newsletter | short intro + scannable body | personal, direct | one clear link | subject line ≤ ~50 chars, preheader, plain-text fallback |
| Slack/Discord/Telegram webhook | 1–3 short paragraphs | community-native, no spam tone | one link | respect channel norms; avoid hashtag stuffing |
| Mastodon/Bluesky | short post | conversational | link + hashtags | ≤ platform limit; 1–3 relevant tags |
| LinkedIn/X (browser) | platform-limit post | professional (LinkedIn) / punchy (X) | link | thread-split when over limit |
| Calendar entries | teaser-sized | matched to stage (tease → launch → recap) | stage-appropriate | dated per the calendar |

Universal rules:

1. **Language follows `AGENT_DEFAULT_LANGUAGE`** unless the user overrides; keep one language per variant (no mid-post mixing).
2. **Every number traces to the brief's facts.** No invented metrics, no invented quotes, no "over 10,000 users" that knowledge does not support. Missing numbers are marked tool-limited or simply omitted.
3. **Links use the example.com family or the project's real public URLs** — never fabricated domains (convention 14).
4. **One primary CTA per variant** — multiple CTAs dilute; pick the one matching the channel's audience intent.
5. **Respect the do-not-say list absolutely** — it outranks persuasion.

## 3. Draft Layout

```
promotion/campaign-{YYYYMMDD}/
├── plan.md                  (from marketing/channel-strategy)
├── brief.md                 (single source of truth)
└── copy/
    ├── github-release.md
    ├── github-pages.md
    ├── email-newsletter.md
    ├── slack-webhook.md
    ├── mastodon.md
    └── calendar-tease.md    (one file per calendar entry)
```

Each copy file carries front matter: channel, target endpoint/URL, secret_ref key (name only — **never the secret**), and the variant's status (`draft` → `approved` → `published` with read-back evidence appended).

## 4. Consistency Pass

Before the campaign approval dialog, run one pass across all variants:

- Same core message in all (trim, don't re-angle).
- Facts identical across variants (same version, same date, same numbers).
- Links resolve to the same destination family; Pages URL matches what the publisher will produce.
- Tone consistent with `domain/brand-guidelines` when present.

The approval dialog presents these variants in summary; what was approved is what gets published — no silent copy edits after approval. A genuine correction after approval is announced in chat before publishing.
