---
slug: "integration/repo-file-reader"
description: "Read source files from cloned repositories. Use when a task requires reading, browsing, or analyzing file contents from a local git clone."
type: "guidance"
---

# Skill: Repo File Reader

## Triggers

- The task requires reading source code from a cloned repository.
- You need to inspect a specific file's content (e.g. a controller, config, test file).
- You want to browse the directory structure of a cloned repository.
- `git_repo(operation="show")` failed or is unavailable.
- You attempted `shell(command="cat ...")` and it failed with "command not found".

## Method: Use `filesystem` Tool Directly

Cloned repositories live under `repos/` in the project workspace. The `filesystem` tool can read them directly — no `git` binary or `shell` required.

### Reading a file

```json
filesystem(operation="read_file", path="repos/<repo-name>/path/to/file.ts")
```

Example:
```json
filesystem(operation="read_file", path="repos/my-service/src/main/java/com/example/Controller.java")
```

### Listing a directory

```json
filesystem(operation="list_dir", path="repos/<repo-name>/src/main/java/com/example/")
```

### Browsing the repository root

```json
filesystem(operation="list_dir", path="repos/<repo-name>")
```

## Execution Steps

### Step 1: Confirm the repo is cloned

```json
git_repo(operation="list_repos")
```

If the target repo is not in the list, clone it first:
```json
git_repo(operation="clone", repository="org/repo-name")
```

### Step 2: Browse the directory structure

Start from the repo root to understand the project layout:
```json
filesystem(operation="list_dir", path="repos/<repo-name>")
```

Then drill down into relevant subdirectories:
```json
filesystem(operation="list_dir", path="repos/<repo-name>/src")
filesystem(operation="list_dir", path="repos/<repo-name>/src/main/java/com/example")
```

### Step 3: Read the target file

```json
filesystem(operation="read_file", path="repos/<repo-name>/path/to/file.ext")
```

## Fallback Methods (in priority order)

If `filesystem` fails for a specific file, try these alternatives:

1. **`git_repo show`** — reads file content from git history (requires git binary):
   ```json
   git_repo(operation="show", repository="repo-name", ref="HEAD", path="src/path/to/file.ts")
   ```

2. **`git_repo search`** — find content by keyword (requires git binary):
   ```json
   git_repo(operation="search", repository="repo-name", query="className")
   ```

3. **`shell`** — last resort, only if shell commands are available:
   ```json
   shell(command="cat repos/<repo-name>/path/to/file.ts")
   ```

## Common Mistakes to Avoid

- **Do NOT** use `cat`, `head`, or `less` via `shell` as the first approach. The `filesystem` tool is always available and requires no system binary.
- **Do NOT** use absolute paths like `/app/projects/slug/repos/...`. Always use relative paths starting with `repos/`.
- **Do NOT** repeatedly call `git_repo(operation="show")` if it fails. Switch to `filesystem(operation="read_file")` immediately.
- **Do NOT** guess file paths. Always `list_dir` first to discover the actual structure.

## File Size Limits

The `filesystem` tool reads the entire file into memory. For very large files (>1MB):
- Use `git_repo(operation="search", query="keyword")` to find specific sections.
- Read only the relevant portions by path (e.g. a specific subdirectory).

## Integration With Other Skills

After reading files, you can:
- Feed the content into test design (`testing/test-designer`).
- Record findings in knowledge (`knowledge_rw`).
- Use insights for PR review (`integration/git-pr-manager`).
- Identify API endpoints for integration testing.
