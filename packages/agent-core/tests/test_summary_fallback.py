"""derive_summary: frontmatter summary wins; otherwise first prose paragraph.

An empty summary rendered an empty shell row in the deferred-knowledge table —
the model could not tell what a detail file contained, so it never loaded it.
No LLM summarization: cross-refs/headers/markdown are stripped and the first
paragraph is truncated.
"""
from __future__ import annotations

from agent_core.knowledge.loader import derive_summary


def test_frontmatter_summary_wins():
    content = "---\nsummary: hand-written summary\n---\nBody paragraph here.\n"
    assert derive_summary(content) == "hand-written summary"


def test_first_prose_paragraph_derived():
    content = "---\ntitle: X\n---\n\nOrder creation requires company and buyer.\n\nMore text.\n"
    assert derive_summary(content) == "Order creation requires company and buyer."


def test_headings_are_skipped():
    content = "---\ntitle: X\n---\n# Heading\n\nActual first paragraph.\n"
    assert derive_summary(content) == "Actual first paragraph."


def test_cross_refs_keep_the_slug_text():
    content = "---\ntitle: X\n---\nSee [[technical/api-map]] for the index.\n"
    summary = derive_summary(content)
    assert "technical/api-map" in summary
    assert "[[" not in summary


def test_markdown_noise_stripped():
    content = "---\ntitle: X\n---\n- **bold** and `code` and > quote\n"
    summary = derive_summary(content)
    assert "**" not in summary
    assert "`" not in summary
    assert ">" not in summary


def test_truncated_to_limit():
    content = "---\ntitle: X\n---\n" + "word " * 100
    assert len(derive_summary(content)) <= 160


def test_empty_body_returns_empty_string():
    assert derive_summary("---\ntitle: X\n---\n") == ""


def test_no_frontmatter_still_derives():
    assert derive_summary("Plain paragraph text.") == "Plain paragraph text."
