---
slug: "office/web-slides"
description: "Generate web-based slide decks - single-file self-contained reveal.js HTML (reveal.js + theme + highlight + notes inlined at generation time, images as base64 data URIs), no build step, zero external requests at view time; renders in-platform via /api/media/ (text/html)"
type: "guidance"
triggers:
  - "网页版ppt"
  - "网页版演示"
  - "html幻灯片"
  - "web slides"
  - "reveal"
  - "html presentation"
tools:
  - code_executor
  - filesystem
  - user_confirm
---

# Skill: Web Slides (reveal.js HTML)

Create browser-based presentations as **one self-contained HTML file** using reveal.js - no build step, no npm, no server. Ported from the community `revealjs-skill` and official `html-slides` skill, adapted to this framework's self-contained-output convention (`[[analysis/report-writer]]` Rule 3).

## Trigger Conditions

- User asks for a web-based / HTML presentation, or a presentation to "open in a browser" (网页版ppt / 网页版演示 / html幻灯片 / web slides / reveal).
- The delivery is a single `.html` file that renders directly in the browser, viewable in-platform via its `/api/media/` link.

## Rule 1 - Self-Contained Contract

- One `.html` file: CSS and JS **fully inlined**, images as base64 `data:` URIs.
- **Zero external requests at view time** - no `<script src="https://...">`, no web fonts, no CDN links. The file must work offline after download.
- Never ship a file that references external URLs for its assets. If inlining failed for any reason, ship the degraded fallback (Rule 2) - never a CDN-linked file.

## Rule 2 - Inlining reveal.js at Generation Time

**Library code comes ONLY from downloaded bytes. Never write, reproduce, abbreviate, or "fix" reveal.js / highlight.js / notes.js code yourself.** An LLM cannot faithfully reproduce hundreds of KB of minified JS - from-memory library code always corrupts silently (dropped declarations, mis-escaped regexes, placeholder variables) and breaks the entire page with `SyntaxError: Invalid regular expression flags` / `Reveal is not defined`. If the CDN fetch fails, ship the degraded fallback (below) - never a from-memory approximation of reveal.js.

The generation script (code_executor) downloads the assets, verifies them, and splices them into a small hand-written HTML skeleton via `str.replace` placeholders. The agent authors only the skeleton and the slide content - the library bytes pass from `fetch()` to the file untouched:

```python
import os, urllib.request

BASE = "https://cdn.jsdelivr.net/npm/reveal.js@4.6.1"

def fetch(path: str) -> str:
    with urllib.request.urlopen(f"{BASE}/{path}", timeout=10) as r:
        body = r.read().decode("utf-8")
        if len(body) < 1000:
            raise RuntimeError(f"empty asset: {path}")
        return body

# TEMPLATE is the agent-written skeleton: doctype, agent-authored control-bar
# CSS + wiring (Rule 5), <style>/*__CSS__*/</style>, <section> slides,
# <script>/*__JS__*/</script>, Reveal.initialize block. The agent authors only
# the skeleton and the slide content - the library bytes pass from fetch() to
# the file untouched.
TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>...</title>
<style>/*__CSS__*/</style>
<style>
  /* agent-authored on-screen control bar (Rule 5) */
  .aw-controls { position: fixed; right: 14px; bottom: 14px; z-index: 30;
    display: flex; gap: 6px; align-items: center; opacity: .85;
    font: 12px/1.4 system-ui, sans-serif; color: #eee; }
  .aw-controls button { width: 34px; height: 34px; font-size: 18px; line-height: 1;
    background: rgba(0,0,0,.45); color: inherit;
    border: 1px solid rgba(255,255,255,.4); border-radius: 6px; cursor: pointer; }
  .aw-controls button:hover { background: rgba(0,0,0,.7); }
  .aw-hint { margin-left: 6px; white-space: nowrap; opacity: .7; }
</style></head>
<body><div class="reveal"><div class="slides">
  <section>...</section>
</div></div>
<div class="aw-controls">
  <button id="aw-prev" title="上一页 (←)">‹</button>
  <button id="aw-next" title="下一页 (→)">›</button>
  <button id="aw-full" title="全屏 (F)">⛶</button>
  <span class="aw-hint">← → 翻页 · F 全屏 · S 演讲者视图 · ? 快捷键</span>
</div>
<script>/*__JS__*/</script>
<script>
  Reveal.initialize({
    hash: true, controls: true, controlsTutorial: true,
    progress: true, slideNumber: true, help: true,
    plugins: [RevealHighlight, RevealNotes]
  });
  // agent-authored wiring for the control bar (uses only DOM APIs, no library bytes)
  document.getElementById("aw-prev").onclick = () => Reveal.prev();
  document.getElementById("aw-next").onclick = () => Reveal.next();
  document.getElementById("aw-full").onclick = () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen();
  };
</script></body></html>"""

try:
    theme_css = fetch("dist/theme/black.css")          # pick one of the 11 themes
    css = fetch("dist/reset.min.css") + fetch("dist/reveal.min.css") + theme_css
    js = (fetch("dist/reveal.js") + fetch("plugin/highlight/highlight.min.js")
          + fetch("plugin/notes/notes.js"))
    if "</script>" in js:
        raise RuntimeError("library JS contains a literal </script>; aborting inline")
    # str.replace is literal (no regex/format hazards); placeholders keep the
    # library bytes out of any f-string or hand-editing path.
    html = TEMPLATE.replace("/*__CSS__*/", css).replace("/*__JS__*/", js)
    # Hard integrity gate: the exact fetched bytes must appear verbatim in the
    # assembled file - catches any rewriting, truncation, or re-escaping.
    assert css in html and js in html, "asset bytes were altered during assembly"
except Exception as e:
    print(f"reveal.js inline failed ({e}); using degraded fallback")
    html = degraded_fallback_html()   # hand-written HTML+CSS demo below
```

**Degraded fallback** (only when the CDN fetch fails): a hand-written single-file HTML demo with the same slide content - the same on-screen control bar as Rule 5 (prev/next/fullscreen buttons + the hint line), arrow-key navigation, and one accent color per slide section. Never ship a file with external `<script src>` tags, and never substitute reveal.js code written from memory - the fallback is the only option when fetching fails.

**Themes** (pick one that matches the topic): `black` / `white` / `league` / `beige` / `sky` / `night` / `serif` / `simple` / `solarized` / `blood` / `moon`.

## Rule 3 - Design

- **Content-informed palette** (mirror `office/pptx` Rule 1): dominant 60–70%, 1–2 supporting, 1 accent; text/background contrast ≥ 4.5:1.
- **Font sizes in `pt`** (slides are fixed-size) - body ≥ 28pt; never `em`/`rem`/`px`.
- **6x6 rule**: ≤ 6 words per line, ≤ 6 lines per slide. One point per slide.
- Use system font stacks only - no web fonts (self-contained contract).

## Rule 4 - Slide Content Features

- Code blocks get `class="language-<lang>"` + highlight.js styling; first slide of a code section may use `data-auto-animate`.
- Speaker notes per slide: `<aside class="notes">...</aside>`.
- Fragments (reveal-on-click) at most 2 per slide, via `class="fragment"`.
- Slide backgrounds: color `data-background-color` or image `data-background-image="data:..."` (base64 only).

## Rule 5 - Structure & Navigation

- Skeleton: `<div class="reveal"><div class="slides"><section>…</section></div></div>` + `Reveal.initialize({hash: true, controls: true, controlsTutorial: true, progress: true, slideNumber: true, help: true, plugins: [RevealHighlight, RevealNotes]})`.
- **On-screen control bar** (copy from the TEMPLATE above): fixed bottom-right ‹ / › / ⛶ buttons plus a hint line (`← -> 翻页 · F 全屏 · S 演讲者视图 · ? 快捷键`). The buttons are hand-written DOM wiring - allowed; they must not touch the inlined library bytes. Keyboard hints in the chat reply alone are not enough - the deck must be operable by someone who never saw the reply.
- Navigation: arrow keys/space to move; `F` fullscreen; `S` speaker view; `?` reveal.js help overlay. Say so in the delivery note too.

## Rule 6 - Output & Verify

- Save to `os.path.join(os.environ["OUTPUT_DIR"], "web-slides.html")` - auto-delivered; the `/api/media/` link renders inline as `text/html`.
- **Verification is a separate, read-only script.** It reads the written file, re-fetches the assets into memory (plain strings - no file writes anywhere), and checks: (a) `count("<section") >= 2`, (b) `Reveal.initialize` present, (c) the fetched `css`/`js` strings appear **verbatim** in the file content (this is the gate that catches rewritten or hand-typed library code), (d) no external asset references - scan for `src="http`, `href="http`, `url(http`, `@import url(http` only (a plain `https://` inside inlined JS strings or comments is fine; do not false-positive), (e) file size < 5MB.
- **The verify script must not write anything - above all not to OUTPUT_DIR.** Every OUTPUT_DIR write auto-delivers another download link; re-running the generation script as "verification" turns one deck into N links. If a check fails, fix the generation script and re-run that (it overwrites `web-slides.html`; the platform collapses same-name deliveries back to one link).
- ASCII filename only (`web-slides.html`).

## Output Requirements

Deliver the `/api/media/` link (opens as a rendered presentation in a new tab), a per-section outline, and the navigation hint (on-screen buttons bottom-right; arrow keys to flip, `F` fullscreen, `S` speaker view, `?` for all shortcuts). If the fallback was used, say so and offer a retry.
