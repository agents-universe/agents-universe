"""Markdown → Jira wiki markup conversion tests.

Jira's v2 API renders a string body as wiki markup, not Markdown — a raw
Markdown "### heading" comes back as "1.1.1 heading".  These tests pin the
conversion that keeps rendered Jira comments readable.
"""
import pytest

from agent_core.tools._wiki import markdown_to_wiki


def test_headings_become_h_levels():
    assert markdown_to_wiki("# Title") == "h1. Title"
    assert markdown_to_wiki("## Section") == "h2. Section"
    # The reported bug: "###" must not survive as a nested numbered list.
    assert markdown_to_wiki("### Subsection") == "h3. Subsection"


def test_bullets_nest_by_indent():
    md = "- a\n    - b\n        - c"
    assert markdown_to_wiki(md) == "* a\n** b\n*** c"


def test_ordered_lists_become_numbered_wiki_lists():
    md = "1. a\n2. b\n    1. b1"
    assert markdown_to_wiki(md) == "# a\n# b\n## b1"


def test_task_list_markers_are_dropped():
    assert markdown_to_wiki("- [x] done") == "* done"
    assert markdown_to_wiki("- [ ] todo") == "* todo"


def test_inline_bold_and_code():
    md = "**bold** and `code` and _italic_"
    assert markdown_to_wiki(md) == "*bold* and {{code}} and _italic_"


def test_code_fence_becomes_code_block():
    md = "```python\nprint(1)\n```"
    assert markdown_to_wiki(md) == "{code:python}\nprint(1)\n{code}"
    assert markdown_to_wiki("```\nplain\n```") == "{code}\nplain\n{code}"


def test_links_and_images():
    assert markdown_to_wiki("[see](https://jira.example.com/browse/DDM-1)") == (
        "[see|https://jira.example.com/browse/DDM-1]")
    assert markdown_to_wiki("![shot](https://x/y.png)") == "!https://x/y.png|shot!"


def test_table_converts_and_drops_separator():
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    assert markdown_to_wiki(md) == "||A||B||\n|1|2|"


def test_blockquote_and_hr():
    assert markdown_to_wiki("> quote") == "bq. quote"
    assert markdown_to_wiki("---") == "----"


def test_wiki_markup_lines_pass_through():
    md = "{color:red}[SELF-ADAPT-DB] needs DB access{color}"
    assert markdown_to_wiki(md) == md
    assert markdown_to_wiki("h1. already wiki") == "h1. already wiki"


def test_plain_paragraph_untouched():
    assert markdown_to_wiki("just some text") == "just some text"


def test_empty_body():
    assert markdown_to_wiki("") == ""
    assert markdown_to_wiki(None) is None


@pytest.mark.parametrize("md", [
    "",
    "plain",
    "### 多级标题\n- 列表项",
    "# T\n\n## S\n\n正文",
])
def test_no_crash_on_common_inputs(md):
    assert isinstance(markdown_to_wiki(md), str)
