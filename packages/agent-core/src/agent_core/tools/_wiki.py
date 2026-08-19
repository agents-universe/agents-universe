"""Markdown → Jira wiki markup conversion for issue bodies.

Jira's REST v2 API renders a string ``body`` as Jira wiki markup, not
Markdown.  A Markdown ``### heading`` therefore comes back as a three-level
nested numbered list ("1.1.1 heading") and ``-`` bullets / ``1.`` lists
render as plain text.  Every body written to Jira goes through
``markdown_to_wiki`` so agents can keep writing Markdown and the rendered
comment stays readable.

The conversion is intentionally line-based and pragmatic, not a full
Markdown parser.  Lines that are already wiki markup (e.g. the
``{color:red}...{color}`` self-adapt-DB marking from the Jira conventions)
pass through untouched.
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_BLOCKQUOTE = re.compile(r"^(?:>+\s?)(.*)$")
_TASKLIST = re.compile(r"^(\s*)[-*+]\s+\[[ xX]\]\s*(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_FENCE = re.compile(r"^```([A-Za-z0-9_+-]*)\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_LINK = re.compile(r"\[([^\]!][^\]]*)\]\(([^)\s]+)\)")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
# Already-wiki lines must not be re-interpreted as Markdown.
_WIKI_PREFIX = re.compile(r"^(?:h[1-6]\.|bq\.|----|\{)")


def _inline(md: str) -> str:
    """Convert inline Markdown constructs (images, links, code, bold)."""
    s = _IMAGE.sub(r"!\2|\1!", md)
    s = _LINK.sub(r"[\1|\2]", s)
    s = _CODE_SPAN.sub(r"{{\1}}", s)
    s = _BOLD.sub(r"*\1*", s)
    return s


def markdown_to_wiki(md: str) -> str:
    if not md:
        return md
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    lines = md.split("\n")
    out: list[str] = []
    in_code = False
    # (indent, depth) stack for the current list block — relative depth
    # handles both 2-space and 4-space markdown nesting.  Reset whenever a
    # non-list line appears.
    indent_stack: list[tuple[int, int]] = []

    def list_item_depth(indent: int) -> int:
        while indent_stack and indent < indent_stack[-1][0]:
            indent_stack.pop()
        if indent_stack and indent == indent_stack[-1][0]:
            return indent_stack[-1][1]
        depth = indent_stack[-1][1] + 1 if indent_stack else 1
        indent_stack.append((indent, depth))
        return depth

    def reset_list() -> None:
        indent_stack.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code blocks → {code} blocks.
        fence = _FENCE.match(line)
        if fence:
            if in_code:
                out.append("{code}")
                in_code = False
            else:
                lang = fence.group(1)
                out.append("{code" + (f":{lang}" if lang else "") + "}")
                in_code = True
            i += 1
            continue
        if in_code:
            out.append(line)
            i += 1
            continue

        # Already wiki markup (e.g. {color:red}[SELF-ADAPT-DB] ... {color}):
        # pass through untouched instead of re-interpreting it as Markdown.
        if _WIKI_PREFIX.match(stripped):
            out.append(line)
            reset_list()
            i += 1
            continue

        # Markdown table → wiki table (||header|| / |cell|).  The separator
        # row (|---|---|) has no wiki equivalent and is dropped.
        if _TABLE_SEP.match(line) or _TABLE_ROW.match(line):
            block: list[str] = []
            has_sep = False
            while i < len(lines) and (_TABLE_SEP.match(lines[i]) or _TABLE_ROW.match(lines[i])):
                has_sep = has_sep or bool(_TABLE_SEP.match(lines[i]))
                block.append(lines[i])
                i += 1
            rows = [
                [c.strip() for c in raw.strip().strip("|").split("|")]
                for raw in block if not _TABLE_SEP.match(raw)
            ]
            if rows:
                if has_sep:
                    header, body = rows[0], rows[1:]
                    out.append("||" + "||".join(_inline(c) for c in header) + "||")
                else:
                    body = rows
                for row in body:
                    out.append("|" + "|".join(_inline(c) for c in row) + "|")
            reset_list()
            continue

        # Horizontal rule.
        if _HR.match(line):
            out.append("----")
            reset_list()
            i += 1
            continue

        # Blockquote → bq.
        bq = _BLOCKQUOTE.match(line)
        if bq:
            out.append("bq. " + _inline(bq.group(1)))
            reset_list()
            i += 1
            continue

        # ATX headings → h1. ... h6.  A bare Markdown "###" would otherwise
        # render as a third-level nested numbered list ("1.1.1").
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"h{level}. " + _inline(heading.group(2)))
            reset_list()
            i += 1
            continue

        # List items: wiki nests by repeating the marker per depth level.
        task = _TASKLIST.match(line)
        if task:
            out.append("*" * list_item_depth(len(task.group(1))) + " " + _inline(task.group(2)))
            i += 1
            continue
        bullet = _BULLET.match(line)
        if bullet:
            out.append("*" * list_item_depth(len(bullet.group(1))) + " " + _inline(bullet.group(2)))
            i += 1
            continue
        ordered = _ORDERED.match(line)
        if ordered:
            out.append("#" * list_item_depth(len(ordered.group(1))) + " " + _inline(ordered.group(2)))
            i += 1
            continue

        # Plain paragraph.
        out.append(_inline(line))
        reset_list()
        i += 1

    return "\n".join(out)
