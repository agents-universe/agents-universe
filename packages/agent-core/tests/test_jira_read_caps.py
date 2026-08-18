"""Jira read operations cap response size; attach_file reads bytes off-loop."""
import asyncio
import json

import pytest

from agent_core.tools.jira import _JiraClient


class _StreamResp:
    def __init__(self, chunks: list[bytes], status: int = 200):
        self._chunks = chunks
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"http {self._status}")

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _StreamCtx:
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return _StreamResp(self._chunks)

    async def __aexit__(self, *a):
        return False


class _FakeHTTP:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.stream_calls: list[tuple] = []
        self.post_calls: list[tuple] = []

    def stream(self, method, url, **kw):
        self.stream_calls.append((method, url, kw))
        return _StreamCtx(self._chunks)

    async def post(self, url, **kw):
        self.post_calls.append((url, kw))
        return _Resp()

    async def get(self, url, **kw):
        return _Resp()


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"id": "1"}


def _client(http) -> _JiraClient:
    return _JiraClient(
        api_token="tok",
        email="e@x.example.com",
        base_url="https://jira.example.com",
        jira_path="",
        auth_type="bearer",
        http=http,
    )


async def test_get_issue_parses_capped_response():
    payload = {"key": "DDM-1", "fields": {"summary": "Fix bug"}}
    http = _FakeHTTP([json.dumps(payload).encode()])
    client = _client(http)

    data = await client.get_issue("DDM-1")
    assert data["key"] == "DDM-1"
    method, url, kw = http.stream_calls[0]
    assert method == "GET"
    assert url.endswith("/rest/api/2/issue/DDM-1")
    assert kw["headers"]["Authorization"] == "Bearer tok"


async def test_get_capped_raises_on_oversize():
    cap = _JiraClient._MAX_RESPONSE
    chunks = [b"x" * (cap // 2), b"y" * (cap // 2 + 100)]  # total > cap
    http = _FakeHTTP(chunks)
    client = _client(http)

    with pytest.raises(ValueError, match="too large"):
        await client.get_issue("DDM-1")


async def test_search_sends_jql_via_capped_post():
    http = _FakeHTTP([b'{"issues": [{"key": "DDM-9"}]}'])
    client = _client(http)

    issues = await client.search("project = DDM", max_results=5)
    assert issues == [{"key": "DDM-9"}]
    method, url, kw = http.stream_calls[0]
    assert method == "POST"
    assert url.endswith("/rest/api/2/search")
    assert kw["json"] == {"jql": "project = DDM", "maxResults": 5}


async def test_attach_file_reads_into_memory(tmp_path):
    f = tmp_path / "report.txt"
    f.write_bytes(b"attachment bytes")

    http = _FakeHTTP([])
    client = _client(http)

    await client.attach_file("DDM-1", str(f))

    url, kw = http.post_calls[0]
    assert url.endswith("/rest/api/2/issue/DDM-1/attachments")
    name, content, ctype = kw["files"]["file"]
    # bytes, not a file handle read synchronously inside the event loop
    assert isinstance(content, bytes)
    assert content == b"attachment bytes"
    assert ctype == "application/octet-stream"
