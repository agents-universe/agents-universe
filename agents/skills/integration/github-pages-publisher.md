---
slug: "integration/github-pages-publisher"
description: "Create, maintain, and validate workflow-based GitHub Pages publishing configuration, with support for a self-hosted runner"
---

# Skill: GitHub Pages Publisher

## Applicable Scenarios

- Create a new workflow-based GitHub Pages publishing YAML.
- Switch GitHub Pages from `Deploy from a branch` to `GitHub Actions` mode.
- Assign a free self-hosted runner for the Pages publishing flow.
- Maintain `.github/workflows/pages.yml` or troubleshoot a failed Pages deployment.

## Current Repository Conventions

- Static-content directory: `docs/`.
- Workflow file path: `.github/workflows/pages.yml`.
- Self-hosted runner label: `ubuntu-latest`.
- Publishing mode: GitHub Pages `GitHub Actions`, not branch-based.

## Target Output

The standard output for creating a Pages workflow:

1. An executable `.github/workflows/pages.yml`
2. Correct self-hosted runner config `runs-on: ubuntu-latest`
3. Pages artifact upload configuration pointing to `docs/`
4. A short note telling the user to switch Settings -> Pages to `GitHub Actions`

## Standard Workflow Structure

1. Trigger on `push` to the default branch
2. Support `workflow_dispatch`
3. Configure `permissions`
4. Configure `concurrency`
5. Define the `build` job
6. Define the `deploy` job

## Required Actions

`actions/checkout`, `actions/configure-pages`, `actions/upload-pages-artifact`, `actions/deploy-pages`.

## Workflow Requirements

- Both `build` and `deploy` jobs use `runs-on: ubuntu-latest` by default.
- Triggers must include at least: push to the default branch and `workflow_dispatch`.
- Declare these permissions: `contents: read`, `pages: write`, `id-token: write`.
- Enable concurrency control so Pages deployments do not overwrite each other.
- Artifact upload path must be `./docs`.
- The `deploy` job sets `environment.name: github-pages` and exposes `page_url`.

## Default Template

```yaml
name: Deploy GitHub Pages

on:
  push:
    branches:
      - main
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: true

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Configure Pages
        uses: actions/configure-pages@v5

      - name: Upload Pages artifact
        uses: actions/upload-pages-artifact@v3
        with:
          path: ./docs

  deploy:
    runs-on: ubuntu-latest
    needs: build
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@v4
```

## Execution Steps

1. Confirm the static site directory is `docs/`.
2. Check whether `.github/workflows/pages.yml` already exists.
3. Not exists → create it from the default template.
4. Exists → make only the minimum necessary changes to avoid breaking other workflow behavior.
5. Validate the YAML has no obvious syntax errors.
6. Remind the user to switch Settings -> Pages -> Source to `GitHub Actions`.
7. If the user asks to push the code, commit and push separately.

## Validation Checklist

1. `.github/workflows/pages.yml` exists and contains the required content.
2. The workflow uses the `ubuntu-latest` runner.
3. The `Upload Pages artifact` step uploads the `docs/` directory.
4. The `Deploy to GitHub Pages` step exists and includes `id: deployment`.
5. The repository Pages Source is switched to `GitHub Actions`.
6. After a successful run, a valid `page_url` is returned.
7. The site is accessible through the repository's Pages URL.

## Default User-Facing Delivery Points

Make explicit in the default response:

1. The workflow file has been created or updated.
2. The runner in use is `ubuntu-latest`.
3. A repository administrator or site maintainer still needs to switch the Pages Source to `GitHub Actions`.

## Common Issues

- Pages still in branch-based mode → the workflow may exist but will not become the actual publishing source.
- Runner label not `ubuntu-latest` → the job may go to the wrong runner pool or fail to schedule.
- Publish directory not `docs/` → the site may deploy successfully but be empty or stale.
- Workflow already exists → do not rewrite the whole file; preserve existing conventions, make minimum changes.
