"""Playwright stand-ins shared by the browser tool tests.

The tool's page/context plumbing is worth testing without a browser: fake
objects record what the tool asked for, so assertions are about call shape
(which selector, which payload, which context options) rather than rendering.
Real-Chromium coverage lives in test_browser_bbox.py and
test_browser_upload_chromium.py; this module is the fast path.
"""
from __future__ import annotations

from agent_core.tools.base import ToolContext


def chromium_available(timeout_ms: int = 10_000) -> bool:
    """True when Playwright can actually launch its bundled Chromium.

    Checking ``executable_path`` alone is not enough: current Playwright builds
    launch a *separate* headless shell, so a machine can have
    ``chromium-<rev>`` without ``chrome-headless-shell-<rev>``, the check
    passes, and every test in the module fails at launch instead of skipping.
    Attempting a launch is the only check that matches reality; the browser is
    closed immediately.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(timeout=timeout_ms)
            browser.close()
        return True
    except Exception:
        return False


class FakeVideo:
    def __init__(self, path: str = "/tmp/fake-video.webm", payload: bytes = b"webm" * 512):
        self._path = path
        self._payload = payload
        self.saved_as: str | None = None
        self.deleted = False
        self.save_error: Exception | None = None

    async def path(self) -> str:
        return self._path

    async def save_as(self, dest: str) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.saved_as = dest
        # Playwright writes the finalized file on save_as; mirror that so size
        # plumbing in the tool is exercised.
        with open(dest, "wb") as fh:
            fh.write(self._payload)

    async def delete(self) -> None:
        self.deleted = True


class FakeFileChooser:
    def __init__(self):
        self.files = None
        self.set_calls = 0

    async def set_files(self, files, timeout: int | None = None) -> None:
        self.files = files
        self.set_calls += 1


class _AwaitableValue:
    """Stands in for Playwright's FutureLike — ``await info.value``."""

    def __init__(self, value):
        self._value = value

    def __await__(self):
        async def _resolve():
            return self._value

        return _resolve().__await__()


class _FileChooserContext:
    def __init__(self, chooser: FakeFileChooser):
        self.value = _AwaitableValue(chooser)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePage:
    """Records every interaction the tool performs."""

    def __init__(self, url: str = "about:blank", goto_error: Exception | None = None):
        self._url = url
        self._goto_error = goto_error
        self.closed = False
        self.context: FakeContext | None = None
        self.video = FakeVideo()
        self.title_error: Exception | None = None
        self.click_error: Exception | None = None
        self.routes: list[str] = []
        self.screenshot_calls: list[dict] = []
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.selected: list[tuple] = []
        self.checked: list[tuple[str, bool]] = []
        self.hovered: list[str] = []
        self.pressed: list[tuple[str, str]] = []
        self.evaluated: list[str] = []
        self.uploaded: list = []
        self.file_chooser = FakeFileChooser()
        self.chooser_expectations = 0

    @property
    def url(self) -> str:
        return self._url

    async def goto(self, url: str, timeout: int = 30000):
        if self._goto_error is not None:
            raise self._goto_error
        self._url = url
        return None

    async def wait_for_load_state(self, state: str) -> None:
        return None

    async def title(self) -> str:
        if self.title_error is not None:
            raise self.title_error
        return self._url

    async def route(self, pattern: str, handler) -> None:
        self.routes.append(pattern)

    def on(self, event: str, handler) -> None:
        return None

    def remove_listener(self, event: str, handler) -> None:
        return None

    async def click(self, selector: str, timeout: int = 30000) -> None:
        if self.click_error is not None:
            raise self.click_error
        self.clicks.append(selector)

    async def fill(self, selector: str, value: str, timeout: int = 30000) -> None:
        self.fills.append((selector, value))

    async def select_option(self, selector: str, value=None, timeout: int = 30000):
        self.selected.append((selector, value))
        return value if isinstance(value, list) else [value]

    async def check(self, selector: str, timeout: int = 30000) -> None:
        self.checked.append((selector, True))

    async def uncheck(self, selector: str, timeout: int = 30000) -> None:
        self.checked.append((selector, False))

    async def hover(self, selector: str, timeout: int = 30000) -> None:
        self.hovered.append(selector)

    async def press(self, selector: str, key: str, timeout: int = 30000) -> None:
        self.pressed.append((selector, key))

    async def set_input_files(self, selector: str, files, timeout: int = 30000) -> None:
        self.uploaded.append((selector, files))

    def expect_file_chooser(self, timeout: int = 30000) -> _FileChooserContext:
        self.chooser_expectations += 1
        return _FileChooserContext(self.file_chooser)

    async def wait_for_selector(self, selector: str, timeout: int = 30000) -> None:
        return None

    async def evaluate(self, script: str, arg=None):
        self.evaluated.append(script)
        return {"ok": True}

    async def inner_text(self, selector: str) -> str:
        return f"CONTENT-OF {self._url}"

    async def screenshot(self, path: str, full_page: bool = True) -> None:
        self.screenshot_calls.append({"path": path, "full_page": full_page})

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, browser: FakeBrowser | None = None, **kwargs):
        self.browser = browser
        self.kwargs = kwargs
        self.pages: list[FakePage] = []
        self.closed = False
        self.storage_state_value = {"cookies": [{"name": "session", "value": "s3cret"}], "origins": []}
        self.storage_state_calls = 0

    async def new_page(self, **kwargs) -> FakePage:
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        page = FakePage()
        page.context = self
        self.pages.append(page)
        if self.browser is not None:
            self.browser.pages.append(page)
        return page

    async def storage_state(self):
        self.storage_state_calls += 1
        return self.storage_state_value

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for page in self.pages:
            page.closed = True


class FakeBrowser:
    def __init__(self):
        self.pages: list[FakePage] = []
        self.contexts: list[FakeContext] = []
        self.context_kwargs: list[dict] = []
        self.closed = False

    async def new_context(self, **kwargs) -> FakeContext:
        ctx = FakeContext(browser=self, **kwargs)
        self.contexts.append(ctx)
        self.context_kwargs.append(kwargs)
        return ctx

    async def new_page(self, **kwargs) -> FakePage:
        ctx = await self.new_context()
        return await ctx.new_page()

    async def close(self) -> None:
        self.closed = True


def make_context(tmp_path, **kwargs) -> ToolContext:
    return ToolContext(
        project_id="proj",
        project_fs_path=str(tmp_path),
        conversation_id="conv",
        user_id="user-1",
        **kwargs,
    )


def install_browser(monkeypatch, context: ToolContext) -> FakeBrowser:
    """Patch ToolContext.ensure_browser so the tool uses a FakeBrowser."""
    browser = FakeBrowser()

    async def _ensure_browser(self=None):
        return browser

    monkeypatch.setattr(ToolContext, "ensure_browser", _ensure_browser)
    context._fake_browser = browser
    return browser
