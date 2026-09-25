"""GitClient pagination for its client-side filters."""
from __future__ import annotations


async def test_list_prs_filters_across_pages(monkeypatch):
    """author/reviewer filtering runs client-side (GitHub's list-pulls has no
    such query params) — a matching PR beyond the first page must still be
    found."""
    from api.services.git_client import GitClient

    page1 = [
        {"number": i, "user": {"login": "other"}, "requested_reviewers": []}
        for i in range(1, 31)
    ]
    page2 = [
        {"number": 31, "user": {"login": "alice"}, "requested_reviewers": []},
    ]

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    requested_pages: list[int] = []

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            page = (params or {}).get("page", 1)
            requested_pages.append(page)
            if page == 1:
                return _Resp(page1)
            if page == 2:
                return _Resp(page2)
            return _Resp([])

    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)

    client = GitClient(token="t", base_url="https://git.example.com")
    prs = await client.list_prs("o", "r", author="alice", per_page=30)

    assert [p["number"] for p in prs] == [31]
    # Both pages were fetched: the first was full, so the walk continued.
    assert requested_pages[:2] == [1, 2]
