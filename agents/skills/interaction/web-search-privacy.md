---
slug: "interaction/web-search-privacy"
description:
  zh-CN: "web_search 隐私红线：禁止检索可识别自然人的个人信息（身份证/手机号/住址/征信/通话记录、人肉/开盒等），技术语境（正则/校验/格式/脱敏/加密/合规）查询豁免"
  en-US: "web_search privacy red line: never look up personal information of an identifiable private individual (ID numbers, phone numbers, home addresses, credit/call records, doxxing); technical-context queries (regex/validation/format/masking/encryption/compliance) are exempt"
type: "guidance"
triggers:
  - "个人信息"
  - "身份证"
  - "手机号"
  - "家庭住址"
  - "户籍"
  - "征信"
  - "通话记录"
  - "人肉"
  - "开盒"
  - "personal information"
  - "phone number"
  - "home address"
  - "id card number"
  - "social security number"
  - "ssn"
  - "doxxing"
tools:
  - web_search
---

# Skill: Web Search Privacy

`web_search` (and any follow-up fetching of its results) must never be used to look up **personal information about a specific, identifiable private individual**. A query violates this rule when its target is a person's private data rather than a technical, business, or public-topic question. The rule is about the query's intent — a PII-shaped query alone (a bare phone number plus "查一下" style probing) is blocked even when no name is attached.

## Blocked

Queries whose goal is to identify, locate, or profile a private person:

- Identity documents: 身份证号, passport/ID card numbers, SSN, 户籍
- Contact & location: a real person's 手机号, home address, family members, real-time location
- Sensitive records: 征信/credit records, 通话记录, medical, financial, employment or school-enrollment records
- Aggregation & dossiers: 人肉搜索, 开盒, doxxing, "who is behind this number/handle/email" lookups that tie an identifier to a real person

Blocked means: do not call `web_search`, do not reformulate the request to sneak the same lookup past this rule, and do not fetch a people-search/records site discovered by other means for the same purpose. Never guess or fabricate personal data as a "compromise" answer.

## Exempt — technical context

Personal-data *topics* are not personal-data *lookups*. When the query treats PII as a **data format, validation rule, or engineering/compliance topic**, search normally:

- 正则 / regex & 校验 / validation: "手机号正则", "身份证校验位算法", "phone number validation regex", "SSN format check"
- 格式 / format, masking, 脱敏, 加密 / encryption: field formats, data masking, tokenization, PII redaction in logs
- Compliance & policy: GDPR / 个人信息保护法 obligations, anonymization, aggregate statistics where no individual is identifiable
- Public figures or companies in their public capacity (official bios, published work) — never their private life or family

Rule of thumb: 「这个字段怎么校验/脱敏」 → allowed; 「这个人的手机号是多少」 → blocked.

## When blocked

1. Decline in one sentence, citing this policy — do not partially run the search.
2. Offer the compliant alternative: analyze data the user already has (uploaded files, project knowledge), aggregate/anonymized statistics, or rephrase as a technical-format query if that was the real need.
3. If intent is genuinely ambiguous, ask what the data will be used for before searching.
