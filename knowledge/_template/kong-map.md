---
category: technical
slug: technical/kong-map
tags: [kong, gateway, api]
template_words: 69
title: Kong Map
---

# Kong Map

## Base Rules

- URL join: `{projectBase}{relativePath}`
- Auth header: `x-api-key` (project secret `kong:dev` / `kong:uat` / `kong:int`)
- Always record downstream business API mapping, not just gateway path

## Kong Catalog

| Name | Path | Method | Purpose | Downstream API | Key Params | Notes |
|------|------|--------|---------|----------------|------------|-------|
| | | | | | | |

## Route Variants

| Gateway Path | Variant Signal | Scope | Selection Rule | Notes |
|--------------|----------------|-------|----------------|-------|
| | | | | |

## DB Fallback Routes

| Table | Purpose | Gateway Path | Scope | Notes |
|-------|---------|--------------|-------|-------|
| | | | | |
