---
slug: "testing/screenshot-annotator"
description: "Annotate focus areas on test screenshots and output highlighted evidence images that are easy to read in Jira"
---

# Skill: Screenshot Annotator

If `workflows/test-artifact-and-jira-conventions.workflow.md` is not loaded for the current task, read it first.

## Trigger Conditions

- The execution result needs screenshots uploaded to a Jira test card.
- A single screenshot contains multiple information points and the assertion focus may not be obvious.
- A defect ticket or test card needs to emphasize a specific button, status, field, or error message.

## Goal

Without altering the original screenshot, generate an annotated screenshot that marks focus areas with highlight boxes, adds a short label per area, and adds a note at the bottom explaining why each highlighted area matters.

## Input Convention

First generate a JSON file describing the focus areas on the screenshot.
The example below intentionally keeps literal Chinese UI anchors such as `已选择数量` and `批量通过按钮`, because they may be the exact strings visible in the screenshot:

```json
{
  "title": "PROJ-123 batch selection issue",
  "subtitle": "Highlight the unchanged selected count and the still-disabled bulk action button",
  "focusAreas": [
    {
      "x": 118,
      "y": 640,
      "width": 220,
      "height": 60,
      "label": "已选择数量",
      "detail": "The UI still shows 0 items after clicking the list checkbox"
    },
    {
      "x": 860,
      "y": 630,
      "width": 260,
      "height": 72,
      "label": "批量通过按钮",
      "detail": "The button remains disabled after selection"
    }
  ]
}
```

Field descriptions:

- `title`: optional title shown at the top of the image.
- `subtitle`: optional supplemental note shown at the top.
- `focusAreas`: at least one focus area.
- `x/y/width/height`: pixel coordinates with the top-left corner of the original image as the origin.
- `xPct/yPct/widthPct/heightPct`: percentage coordinates in the range `0-100`, suitable for reuse across different resolutions.
- `label`: the short title for that area.
- `detail`: optional explanation of why that area matters.
- `color`: optional custom highlight color.

## Tool Calls

```json
focus_template(image_path="<source-screenshot>", count=2, units="percent", title="<text>", subtitle="<text>")
image_annotator(image_path="<source-screenshot>", title="<text>", subtitle="<text>", focus_areas=[
  {"xPct": 10, "yPct": 20, "widthPct": 30, "heightPct": 15, "label": "Area 1", "detail": "Why this matters"}
])
```

Use `focus_template` first to read the image size and generate a template, then adjust the rectangle coordinates — this avoids writing the full JSON from scratch. `units="percent"` generates a percentage-based coordinate template suitable for reuse across resolutions.

Do not use the raw template output directly: `focus_template` only creates placeholder boxes with labels like `Focus 1`. Before calling `image_annotator`, replace placeholders with real labels, real assertion notes, and reviewed coordinates.

If `output_path` is omitted in `image_annotator`, it defaults to writing `*-annotated.png` in the same directory as the original file.

## Usage Principles

1. Preserve the original image and output the annotated image separately.
2. Mark only the 1 to 4 most important regions; do not fill the entire image with markup.
3. Keep labels short and explanations concrete; prioritize assertion points and abnormal points.
4. Never keep placeholder labels such as `Focus 1/2/3` or placeholder notes such as `补充这里为什么是关注重点。` in a final annotated screenshot.
5. To prove the expected result, write the result in the label; to prove a defect, write the abnormal symptom in the label.
6. When uploading to Jira, prefer attaching the annotated version first; if there may be dispute, also attach the original image.
7. If the screenshot may be reused across different resolutions, prefer a percentage-coordinate template.

## Recommended Output Paths

- Original image: `tests/generated/artifacts/<issue>/<case-id>-<scenario-slug>.png`
- Annotated image: `tests/generated/artifacts/<issue>/<case-id>-<scenario-slug>-annotated.png`

## Example Flow

After the Playwright run: obtain the original screenshot, read it and confirm the focus points, generate the focus JSON file, call `image_annotator` to generate the annotated image, then upload the annotated image to the Jira test card or Bug.

## Learning Feedback

If a project repeatedly uses the same types of screenshot focus points, for example `审批状态标签` (approval status badge), `底部批量操作区` (bottom bulk-action area), or `错误提示弹窗` (error-message modal), persist that experience into `knowledge/{project}/ui-patterns.md` or `test-patterns.md` so it can be reused directly later.
