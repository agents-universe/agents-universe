"""Knowledge completeness scoring algorithm."""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

import frontmatter

_log = logging.getLogger("agent_core.knowledge.scorer")

_CROSS_REF_RE = re.compile(r"\[\[([^\]]+)\]\]")
_GAP_RE = re.compile(r"<!--\s*gaps:\s*(\[.*?\])\s*-->", re.DOTALL)

# Patterns that indicate unfilled/placeholder content
_PLACEHOLDER_PATTERNS = [
    re.compile(r"\(to be filled[^)]*\)", re.IGNORECASE),
    re.compile(r"\(to be added[^)]*\)", re.IGNORECASE),
    re.compile(r"\{[A-Z][A-Z0-9_-]+\}"),  # {APPLICATION-ID}, {env}
    re.compile(r">\s*(How|What|Why|Where|When|Which)\s+.*\?"),  # blockquote questions as prompts
]

# Word count target per category — roughly how many meaningful words a "complete" file should have
_EXPECTED_WORDS: dict[str, int] = {
    "domain": 500,
    "technical": 400,
    "skills": 600,
    "system": 200,
}
_DEFAULT_EXPECTED_WORDS = 400

# Minimum meaningful lines for a section to count as "filled"
_MIN_LINES_PER_SECTION = 3

# Weights
W_CONTENT = 0.60
W_SECTIONS = 0.25
W_CROSS_REF = 0.10
W_GAP = 0.05


@dataclass
class CompletenessComponents:
    coverage_breadth: float  # 0–100: section fill rate
    content_depth: float     # 0–100: word count progress toward target
    recency_score: float     # 0–100
    cross_ref_density: float # 0–100
    agent_gap_score: float   # 0–100 (higher = more gaps = worse)
    final_score: float       # 0–100


def _count_filled_cells(line: str) -> tuple[int, int]:
    """Count filled vs total cells in a markdown table row."""
    cells = [c.strip() for c in line.split("|")[1:-1]]
    total = len(cells)
    filled = sum(1 for c in cells if c and c != "---" and not c.startswith("-"))
    return filled, total


def _is_line_meaningful(stripped: str) -> bool:
    """Check if a single line contains meaningful project-specific content."""
    if not stripped:
        return False
    if stripped.startswith("#"):
        return False
    if stripped.startswith("<!--") and stripped.endswith("-->"):
        return False
    if re.match(r"^\|[-|\s:]+\|$", stripped):
        return False
    if any(p.search(stripped) for p in _PLACEHOLDER_PATTERNS):
        return False
    if re.match(r"^[-*]\s+\w[^:]*:\s*$", stripped):
        return False
    if "|" in stripped:
        filled, total = _count_filled_cells(stripped)
        if total > 0 and filled <= 1:
            return False
    return True


def _count_meaningful_in_section(lines: list[str]) -> int:
    """Count meaningful lines in a section."""
    return sum(1 for line in lines if _is_line_meaningful(line.strip()))


def compute_completeness(
    fs_path: str,
    days_since_update: float,
    inbound_link_count: int = 0,
    content: str | None = None,
) -> CompletenessComponents:
    """Compute completeness score for a knowledge file.

    If *content* is provided, the file is not re-read from disk.
    """
    if content is None:
        path = Path(fs_path)
        if not path.exists():
            return CompletenessComponents(0, 0, 0, 0, 100, 0)
        content = path.read_text(encoding="utf-8")
    post = frontmatter.loads(content)
    category = post.metadata.get("category", "")
    body = post.content

    lines = body.splitlines()

    # --- Component 1: Section fill rate (coverage_breadth) ---
    sections: list[list[str]] = []
    current_section: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current_section:
                sections.append(current_section)
            current_section = [line]
        else:
            current_section.append(line)
    if current_section:
        sections.append(current_section)

    if sections:
        filled_sections = sum(
            1 for s in sections
            if _count_meaningful_in_section(s) >= _MIN_LINES_PER_SECTION
        )
        coverage_breadth = (filled_sections / len(sections)) * 100
    else:
        coverage_breadth = 0.0

    # --- Component 2: Content depth (word count progress) ---
    # Count meaningful words (excluding placeholders, headers, comments)
    meaningful_words = 0
    for line in lines:
        stripped = line.strip()
        if _is_line_meaningful(stripped):
            meaningful_words += len(stripped.split())

    # Subtract template baseline — only NEW words added by user count
    template_baseline = int(post.metadata.get("template_words", 0))
    net_words = max(0, meaningful_words - template_baseline)

    expected = _EXPECTED_WORDS.get(category, _DEFAULT_EXPECTED_WORDS)
    # Logarithmic curve: slow start, diminishing returns
    # At 25% of expected words → ~40% score; at 100% → ~80%; at 200% → ~95%
    if net_words == 0:
        content_depth = 0.0
    else:
        ratio = net_words / expected
        content_depth = min(100.0, 100.0 * (1 - math.exp(-2.0 * ratio)))

    # --- Component 3: Recency score (decays 100 → 0 over 300 days) ---
    recency_score = max(0.0, min(100.0, 100.0 - (days_since_update / 3.0)))

    # --- Component 4: Cross-reference density ---
    outbound = len(_CROSS_REF_RE.findall(body))
    total_links = outbound + inbound_link_count
    cross_ref_density = min(100.0, total_links * 10.0)

    # --- Component 5: Agent gap score (inverse — more gaps = lower final score) ---
    gap_match = _GAP_RE.search(content)
    gap_count = 0
    if gap_match:
        try:
            gaps = json.loads(gap_match.group(1))
            gap_count = len(gaps) if isinstance(gaps, list) else 0
        except (json.JSONDecodeError, ValueError):
            _log.debug("Failed to parse gap JSON: %s", gap_match.group(1)[:100])
            gap_count = 1
    agent_gap_score = min(100.0, gap_count * 20.0)

    # --- Final weighted score ---
    # No new content beyond template baseline → score is 0
    if content_depth == 0.0:
        final_score = 0.0
    else:
        raw_score = (
            W_CONTENT * content_depth
            + W_SECTIONS * coverage_breadth
            + W_CROSS_REF * cross_ref_density
            + W_GAP * (100.0 - agent_gap_score)
        )
        final_score = max(0.0, min(100.0, round(raw_score, 1)))

    return CompletenessComponents(
        coverage_breadth=coverage_breadth,
        content_depth=content_depth,
        recency_score=recency_score,
        cross_ref_density=cross_ref_density,
        agent_gap_score=agent_gap_score,
        final_score=final_score,
    )


def completeness_color(score: float) -> str:
    """Return UI color class for a completeness score."""
    if score < 40:
        return "red"
    elif score < 70:
        return "amber"
    elif score < 90:
        return "green"
    return "teal"
