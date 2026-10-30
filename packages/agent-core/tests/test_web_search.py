"""Tests for the web_search tool (Keenable / Bing RSS / Baidu / DuckDuckGo)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

import httpx

from agent_core.tools.registry import build_tool_registry
from agent_core.tools.web_search import WebSearchTool

from api_request_fakes import FakeResponse, make_context


def _resp(text="", status=200, headers=None):
    r = FakeResponse(
        status_code=status,
        text=text,
        headers=headers or {"content-type": "text/html; charset=utf-8"},
    )
    r.is_redirect = False
    return r


def _redirect(location, status=302):
    r = FakeResponse(status_code=status, text="", headers={"location": location})
    r.is_redirect = True
    return r


def _stream_ctx(response):
    ctx = Mock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _mock_http(responses):
    """Fake shared client whose stream() yields *responses* in order;
    exception instances in the list are raised instead."""
    http = Mock()
    http.stream = Mock(side_effect=[
        r if isinstance(r, Exception) else _stream_ctx(r) for r in responses
    ])
    return http


BING_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Bing</title>
<item>
  <title>Example Page A</title>
  <link>https://example.com/a</link>
  <description>&lt;p&gt;Desc &lt;b&gt;A&lt;/b&gt; here&lt;/p&gt;</description>
</item>
<item>
  <title>Example Page B</title>
  <link>https://example.com/b</link>
  <description>Plain desc B</description>
</item>
</channel></rss>"""

BAIDU_HTML = """<html><body><div id="content_left">
<div class="result c-container">
  <h3 class="t"><a href="https://www.baidu.com/link?url=abc" mu="https://example.com/real-a" target="_blank">Result A Title</a></h3>
  <div class="content-right_8sXDu">这是一段足够长的摘要文字，超过三十个字符用于触发启发式snippet提取逻辑。</div>
</div>
<div class="result c-container">
  <h3 class="t"><a href="https://example.com/no-mu" target="_blank">Result B Title</a></h3>
  <div class="c-abstract">另一个足够长的摘要文字内容，同样超过三十个字符的长度限制以确保被提取。</div>
</div>
</div></body></html>"""

DDG_HTML = """<html><body><div class="result">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fddg-a&amp;rut=xyz">DDG Result A</a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fddg-a&amp;rut=xyz">Snippet text for A</a>
</div></body></html>"""


async def _run(tool, params, http):
    return await tool.execute(params, make_context(http=http))


KEENABLE_JSON = json.dumps({
    "query": "example",
    "mode": "pro",
    "results": [
        {
            "title": "Keenable Result A",
            "url": "https://example.com/keen-a",
            "snippet": "Page text for A",
            "description": "",
            "published_at": "2026-01-01",
        },
        {"title": "Keenable Result B", "url": "https://example.com/keen-b", "snippet": "Text B"},
        {"title": "", "url": "https://example.com/skip", "snippet": "no title — dropped"},
    ],
})


def _keenable_resp(text=KEENABLE_JSON, status=200):
    return _resp(text, status=status, headers={"content-type": "application/json"})


async def test_bing_rss_success():
    http = _mock_http([_resp(BING_RSS, headers={"content-type": "application/rss+xml"})])
    result = await _run(WebSearchTool(), {"query": "example", "engine": "bing"}, http)
    assert result["engine"] == "bing"
    assert result["count"] == 2
    assert result["results"][0] == {
        "title": "Example Page A",
        "url": "https://example.com/a",
        "snippet": "Desc A here",
    }


async def test_baidu_mu_preferred_and_href_fallback():
    http = _mock_http([_resp(BAIDU_HTML)])
    result = await _run(WebSearchTool(), {"query": "案例", "engine": "baidu"}, http)
    assert result["engine"] == "baidu"
    assert result["count"] == 2
    # mu attribute wins over the tracked baidu.com/link redirect href
    assert result["results"][0]["url"] == "https://example.com/real-a"
    # no mu -> the href itself is kept
    assert result["results"][1]["url"] == "https://example.com/no-mu"
    assert result["results"][0]["title"] == "Result A Title"
    assert "摘要文字" in result["results"][0]["snippet"]


async def test_duckduckgo_uddg_unwrap():
    http = _mock_http([_resp(DDG_HTML)])
    result = await _run(WebSearchTool(), {"query": "test", "engine": "duckduckgo"}, http)
    assert result["engine"] == "duckduckgo"
    assert result["results"][0]["url"] == "https://example.com/ddg-a"
    assert result["results"][0]["title"] == "DDG Result A"
    assert result["results"][0]["snippet"] == "Snippet text for A"


async def test_keenable_success_first_in_chain():
    http = _mock_http([_keenable_resp()])
    result = await _run(WebSearchTool(), {"query": "example"}, http)
    assert result["engine"] == "keenable"
    assert result["count"] == 2  # title-less entry dropped
    assert result["results"][0] == {
        "title": "Keenable Result A",
        "url": "https://example.com/keen-a",
        "snippet": "Page text for A",
    }
    method, url = http.stream.call_args[0][0], http.stream.call_args[0][1]
    assert method == "POST"
    assert url == "https://api.keenable.ai/v1/search/public"
    kwargs = http.stream.call_args[1]
    assert kwargs["headers"]["X-Keenable-Title"]
    assert kwargs["json"]["query"] == "example"
    assert kwargs["json"]["mode"] == "pro"


async def test_keenable_429_falls_back_to_bing():
    http = _mock_http([
        _keenable_resp(text='{"message":"rate limit"}', status=429),
        _resp(BING_RSS, headers={"content-type": "application/rss+xml"}),
    ])
    result = await _run(WebSearchTool(), {"query": "example"}, http)
    assert result["engine"] == "bing"
    assert result["count"] == 2


async def test_engine_pin_keenable_all_fail_without_fallback():
    http = _mock_http([
        _keenable_resp(text='{"message":"rate limit"}', status=429),
    ])
    result = await _run(WebSearchTool(), {"query": "x", "engine": "keenable"}, http)
    assert result["error"] == "All search engines failed"
    assert len(result["engines_tried"]) == 1
    assert result["engines_tried"][0]["engine"] == "keenable"
    assert "429" in result["engines_tried"][0]["reason"]
    assert http.stream.call_count == 1


async def test_keenable_non_json_body_is_engine_failure():
    html = "<html>gateway error</html>"
    http = _mock_http([
        _keenable_resp(text=html),
        _resp(BING_RSS, headers={"content-type": "application/rss+xml"}),
    ])
    result = await _run(WebSearchTool(), {"query": "x"}, http)
    assert result["engine"] == "bing"


async def test_auto_fallback_second_engine_wins():
    http = _mock_http([
        _keenable_resp(text="busy", status=503),         # keenable fails
        _resp(BING_RSS, headers={"content-type": "application/rss+xml"}),  # bing wins
    ])
    result = await _run(WebSearchTool(), {"query": "案例"}, http)
    assert result["engine"] == "bing"
    assert result["count"] == 2


async def test_baidu_captcha_page_blocked():
    http = _mock_http([_resp("<html>百度安全验证</html>")])
    result = await _run(WebSearchTool(), {"query": "x", "engine": "baidu"}, http)
    assert "captcha" in result["engines_tried"][0]["reason"]


async def test_all_engines_fail_reports_reasons():
    http = _mock_http([
        _keenable_resp(text='{"message":"rate limit"}', status=429),   # keenable: 429
        _resp("busy", status=503),                                     # bing: HTTP 503
        _redirect("https://wappass.baidu.com/captcha/verify"),         # baidu: captcha hop
        _resp("请完成安全验证后继续访问"),                               # wappass page
        httpx.TimeoutException("ddg unreachable"),                     # duckduckgo: timeout
    ])
    result = await _run(WebSearchTool(), {"query": "x"}, http)
    assert result["error"] == "All search engines failed"
    reasons = {t["engine"]: t["reason"] for t in result["engines_tried"]}
    assert "429" in reasons["keenable"]
    assert "HTTP 503" in reasons["bing"]
    assert "captcha" in reasons["baidu"]
    assert "timed out" in reasons["duckduckgo"]


async def test_redirect_chain_followed():
    http = _mock_http([
        _redirect("https://example.com/mid"),
        _redirect("https://www.bing.com/search?q=x&format=rss"),
        _resp(BING_RSS, headers={"content-type": "application/rss+xml"}),
    ])
    result = await _run(WebSearchTool(), {"query": "x", "engine": "bing"}, http)
    assert result["count"] == 2
    assert http.stream.call_count == 3


async def test_timeout_falls_through_chain():
    http = _mock_http([
        httpx.TimeoutException("t1"),
        httpx.TimeoutException("t2"),
        httpx.TimeoutException("t3"),
        httpx.TimeoutException("t4"),
    ])
    result = await _run(WebSearchTool(), {"query": "x"}, http)
    assert result["error"] == "All search engines failed"
    assert len(result["engines_tried"]) == 4
    assert all("timed out" in t["reason"] for t in result["engines_tried"])


async def test_num_results_clamped_and_query_urlencoded():
    items = "".join(
        f"<item><title>T{i}</title><link>https://example.com/{i}</link><description>d</description></item>"
        for i in range(12)
    )
    rss = f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'
    http = _mock_http([_resp(rss, headers={"content-type": "application/rss+xml"})])
    result = await _run(
        WebSearchTool(), {"query": "a & b c", "engine": "bing", "num_results": "30"}, http
    )
    assert result["count"] == 10  # "30" (string) clamped to 10
    requested_url = http.stream.call_args[0][1]
    assert requested_url == "https://www.bing.com/search?q=a+%26+b+c&format=rss"


async def test_empty_query_rejected():
    result = await _run(WebSearchTool(), {"query": "   "}, _mock_http([]))
    assert "error" in result
    assert "query" in result["error"]


async def test_legit_zero_results_is_not_error():
    rss = '<?xml version="1.0"?><rss version="2.0"><channel><title>empty</title></channel></rss>'
    http = _mock_http([_resp(rss, headers={"content-type": "application/rss+xml"})])
    result = await _run(WebSearchTool(), {"query": "zzz-unfindable", "engine": "bing"}, http)
    assert result["results"] == []
    assert result["count"] == 0
    assert "error" not in result


def test_registry_instantiates_web_search():
    registry = build_tool_registry(["web_search"])
    assert "web_search" in registry
    assert isinstance(registry["web_search"], WebSearchTool)
