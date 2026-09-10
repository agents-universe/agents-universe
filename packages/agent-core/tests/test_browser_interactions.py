"""browser_playwright interaction operations — selects, checks, hover, keys."""
from __future__ import annotations

import pytest

from agent_core.tools.browser_playwright import BrowserPlaywrightTool
from browser_fakes import install_browser, make_context


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    install_browser(monkeypatch, context)
    return context


async def test_select_option_accepts_single_value_and_list(ctx):
    tool = BrowserPlaywrightTool()
    single = await tool.execute({"operation": "select_option", "selector": "#city", "value": "sh"}, ctx)
    assert single == {"success": True, "selector": "#city", "selected": ["sh"]}

    multi = await tool.execute(
        {"operation": "select_option", "selector": "#tags", "values": ["a", "b"]}, ctx
    )
    assert multi["selected"] == ["a", "b"]
    assert ctx._browser_page.selected == [("#city", "sh"), ("#tags", ["a", "b"])]


async def test_select_option_requires_a_value(ctx):
    result = await BrowserPlaywrightTool().execute({"operation": "select_option", "selector": "#city"}, ctx)
    assert "requires 'value' or 'values'" in result["error"]


async def test_check_and_uncheck_report_state(ctx):
    tool = BrowserPlaywrightTool()
    checked = await tool.execute({"operation": "check", "selector": "#terms"}, ctx)
    unchecked = await tool.execute({"operation": "uncheck", "selector": "#terms"}, ctx)
    assert checked == {"success": True, "selector": "#terms", "checked": True}
    assert unchecked == {"success": True, "selector": "#terms", "checked": False}
    assert ctx._browser_page.checked == [("#terms", True), ("#terms", False)]


async def test_check_requires_selector(ctx):
    result = await BrowserPlaywrightTool().execute({"operation": "check"}, ctx)
    assert "requires a 'selector'" in result["error"]


async def test_hover_requires_selector(ctx):
    tool = BrowserPlaywrightTool()
    ok = await tool.execute({"operation": "hover", "selector": ".menu"}, ctx)
    assert ok == {"success": True, "selector": ".menu"}
    assert ctx._browser_page.hovered == [".menu"]

    missing = await tool.execute({"operation": "hover"}, ctx)
    assert "requires a 'selector'" in missing["error"]


async def test_press_requires_selector_and_key(ctx):
    tool = BrowserPlaywrightTool()
    ok = await tool.execute({"operation": "press", "selector": "#search", "key": "Enter"}, ctx)
    assert ok == {"success": True, "selector": "#search", "key": "Enter"}
    assert ctx._browser_page.pressed == [("#search", "Enter")]

    missing = await tool.execute({"operation": "press", "selector": "#search"}, ctx)
    assert "requires 'selector' and 'key'" in missing["error"]
