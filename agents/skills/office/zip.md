---
slug: "office/zip"
description: "Compress files/folders into .zip archives and safely extract .zip archives — member-path validation (zip-slip), zip-bomb size checks, ASCII arcnames, verification, OUTPUT_DIR auto-delivery; for packaging deliverables or unpacking archives — not for tar/7z (explicit requests only, same safety rules) or for editing Office files (that is office/pptx|xlsx|docx)"
type: "guidance"
triggers:
  - "压缩zip"
  - "解压"
  - "打包"
  - "zip文件"
  - "unzip"
  - "压缩包"
tools:
  - code_executor
  - filesystem
  - user_confirm
---

# Skill: ZIP (compress & extract)

Compress and extract `.zip` archives with Python's stdlib `zipfile` inside `code_executor` (sandboxed Python, 30s timeout). **Never use the `shell` tool for archives** — its command allowlist contains no `zip`/`unzip`/`tar`.

## Trigger Conditions

- User asks to **compress / package** files or a folder into a zip (压缩zip / 打包 / 压缩包).
- User asks to **extract / unpack** a zip (解压 / unzip / zip文件).
- NOT for `.tar`/`.gz`/`.7z` as a routine request — stdlib `tarfile` exists, treat it as an explicit one-off with the same safety rules below; NOT for editing the contents of Office files (that is `office/pptx` / `office/xlsx` / `office/docx`).

## Where inputs live (read this first)

- Work on **workspace paths** (relative to the project root, which is the sandbox cwd).
- A zip previously delivered in this conversation is a real disk file under `.tmp/media/{conversation_id}/` — readable from the sandbox.
- **A user-attached zip upload exists only in memory and is NOT readable from the sandbox.** Explain this and ask the user to place the file in the workspace — same rule as agent guardrail 5 for binary Office attachments.

## Rule 1 — Compress flow

- One `code_executor` run: open with `zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED)`, walk the inputs, and store **relative paths as arcnames** (forward slashes, no absolute prefixes, no drive letters).
- Save to `os.path.join(os.environ["OUTPUT_DIR"], "name.zip")` — files in `OUTPUT_DIR` are **auto-delivered** as authenticated `/api/media/` download links.
- ASCII filenames only (Chinese goes in the reply text, not the filename). Never overwrite an existing deliverable — new filename each time.
- Keep the script within the 30s timeout; keep the result under 5MB when possible and report the real size if larger.

```python
import os, zipfile

root = os.environ["PROJECT_DIR"]
out = os.path.join(os.environ["OUTPUT_DIR"], "archive.zip")
targets = ["reports", "summary.md"]  # workspace-relative inputs
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    for t in targets:
        p = os.path.join(root, t)
        if os.path.isfile(p):
            zf.write(p, t.replace(os.sep, "/"))
        else:
            for dirpath, _, files in os.walk(p):
                for f in files:
                    fp = os.path.join(dirpath, f)
                    arc = os.path.relpath(fp, root).replace(os.sep, "/")
                    zf.write(fp, arc)
```

## Rule 2 — Extract flow (safety is mandatory, every time)

Before writing a single byte, open the archive and validate **every** `ZipInfo`:

1. **Zip-slip** — reject members whose normalized path (`name.replace("\\", "/")`) is absolute, starts with `../`, contains a `..` segment, or carries a drive letter (`C:`). Abort the whole extraction listing the offending members — never extract "the rest".
2. **Zip bomb** — sum the declared `file_size` and reject if the total > 200MB, any single member > 50MB, or `file_size / compress_size > 100` for any member. Abort with the numbers — do not "try and see"; the sandbox guard confines writes to the project but does not stop disk exhaustion inside it.
3. Extract into a **fresh subfolder** of the workspace (e.g. `.tmp/extracts/<name>/`); never merge into an existing folder and never overwrite existing files.
4. Then `zf.testzip()` — a bad CRC member means the extraction is not deliverable.

```python
import os, zipfile

src = "uploads/incoming.zip"  # workspace-relative
dest = os.path.join(".tmp", "extracts", "incoming")  # fresh subfolder, never existing
with zipfile.ZipFile(src) as zf:
    bad = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if (name.startswith("/") or ".." in name.split("/")
                or (len(name) > 1 and name[1] == ":")):
            bad.append(name)
        if info.file_size > 50 * 1024 * 1024:
            bad.append(f"{name}: {info.file_size} bytes")
        if info.compress_size and info.file_size / info.compress_size > 100:
            bad.append(f"{name}: ratio {info.file_size / info.compress_size:.0f}")
    total = sum(i.file_size for i in zf.infolist())
    if bad or total > 200 * 1024 * 1024:
        print("refused:", bad or f"total {total} bytes"); raise SystemExit(1)
    os.makedirs(dest, exist_ok=True)
    zf.extractall(dest)
    assert zf.testzip() is None
```

## Rule 3 — Deliver extracted top-level outputs

Only **top-level files** in `OUTPUT_DIR` are auto-collected. If the deliverable is the extracted content itself, write the files the user cares about to `OUTPUT_DIR` top level (flat ASCII names). If the deliverable is the whole tree, leave it in the workspace and either `deliver_file` the chosen entry file or re-list the contents in the reply.

## Rule 4 — Verify before delivering

- **Compress**: reopen the produced zip with `zipfile` and assert it opens, `testzip()` is None, and the expected arcnames are present and non-empty.
- **Extract**: assert the destination folder exists and the member count matches the archive listing.
- Never deliver an unverified archive.

## Output Requirements

- Quote the tool result's `/api/media/` URLs **verbatim** — complete absolute addresses (host + deployment sub-path included); never prepend a base URL or rewrite them as relative paths.
- Reply includes: what was compressed/extracted, member count + total size, and the download link(s) (compress) or the workspace listing (extract). Refusals list the offending members and the numbers that tripped the limit.
