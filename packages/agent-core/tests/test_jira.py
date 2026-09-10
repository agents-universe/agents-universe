"""Jira tool: JIRA_PROJECT_KEY must come from integration settings — cfg()'s
credential-key guard (final segment "KEY") would never return it."""
from unittest.mock import AsyncMock, Mock, patch

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools.jira import JiraTool, _JiraClient


def _ctx(**over) -> ToolContext:
    ctx = ToolContext(
        project_id="proj",
        project_fs_path="/tmp/proj",
        conversation_id="conv",
        user_id="user-1",
        db_session=None,
    )
    ctx.integration_settings = {"JIRA_PROJECT_KEY": "DDM"}
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


@pytest.mark.asyncio
async def test_create_issue_uses_integration_settings_project_key():
    tool = JiraTool()
    client = AsyncMock()
    client.create_issue = AsyncMock(return_value={"key": "DDM-7", "id": "10007"})
    client.base_url = "https://jira.example.com"

    result = await tool._op_create_issue(
        {"summary": "Fix the bug"},
        client,
        _ctx(),
    )

    assert result["key"] == "DDM-7"
    client.create_issue.assert_awaited_once()
    call = client.create_issue.await_args.kwargs
    assert call["project_key"] == "DDM"
    assert call["issue_type"] == "Task"


@pytest.mark.asyncio
async def test_create_test_issue_uses_integration_settings_project_key():
    tool = JiraTool()
    client = AsyncMock()
    client.create_issue = AsyncMock(return_value={"key": "DDM-8", "id": "10008"})
    client.base_url = "https://jira.example.com"

    result = await tool._op_create_test_issue(
        {"target_issue_key": "DDM-7", "summary": "Verify the fix"},
        client,
        _ctx(),
    )

    assert result["key"] == "DDM-8"
    call = client.create_issue.await_args.kwargs
    assert call["project_key"] == "DDM"


@pytest.mark.asyncio
async def test_create_test_issue_requires_project_key():
    """无 JIRA_PROJECT_KEY 且未传 project_key 时，_op_create_test_issue 必须
    报错——此前把 None 传给 Jira（原始 400）+ link 静默跳过，与
    _op_create_issue 的行为不一致。"""
    tool = JiraTool()
    client = AsyncMock()
    client.base_url = "https://jira.example.com"

    ctx = _ctx()
    ctx.integration_settings = {}

    result = await tool._op_create_test_issue(
        {"target_issue_key": "DDM-7", "summary": "Verify"},
        client,
        ctx,
    )

    assert result == {"error": "project_key is required"}
    client.create_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_comment_posts_wiki_markup_not_markdown():
    """Jira's v2 API renders a string body as wiki markup — a Markdown
    '### heading' would come back as a '1.1.1' nested numbered list."""
    http = AsyncMock()
    resp = Mock()
    resp.raise_for_status = Mock()
    resp.json.return_value = {"id": "10042"}
    http.post.return_value = resp

    client = _JiraClient(
        api_token="t", email="e", base_url="https://jira.example.com",
        jira_path="/jira", auth_type="basic", http=http,
    )
    await client.add_comment(
        "DDM-7",
        "### 执行结果\n\n- 通过\n- **回归通过**",
    )

    sent = http.post.call_args.kwargs["json"]["body"]
    assert sent == "h3. 执行结果\n\n* 通过\n* *回归通过*"


class _MinimalCtx:
    """Minimal stand-in for execute(): _build_client is patched, so only the
    token getters and cfg() need to exist."""

    def __init__(self):
        self.user_id = "u1"
        self.secret_key = "test-secret-key"

    def cfg(self, key, default=None):
        return default


@pytest.mark.asyncio
async def test_http_error_body_redacts_credential(caplog):
    """Atlassian can echo the credential in 401 bodies — the resolved token
    must never reach the LLM/history inside the returned error message, nor
    the log, which used to receive the raw body before the redaction pass."""
    import httpx

    tool = JiraTool()

    client = AsyncMock()
    client.api_token = "ATATT-secret-token-999"
    client.email = "agent@example.com"
    client.base_url = "https://jira.example.com"
    client.auth_type = "bearer"

    def _boom(key):
        resp = httpx.Response(
            401,
            text='{"message": "Bad credentials: ATATT-secret-token-999"}',
            request=httpx.Request("GET", "https://jira.example.com/rest/api/2/issue/DDM-1"),
        )
        raise httpx.HTTPStatusError("Unauthorized", request=resp.request, response=resp)

    client.get_issue = AsyncMock(side_effect=_boom)

    with patch("agent_core.tools.jira.get_token", return_value="ATATT-secret-token-999"), \
         patch("agent_core.tools.jira.get_token_optional", return_value="agent@example.com"), \
         patch.object(tool, "_build_client", new=AsyncMock(return_value=client)):
        result = await tool.execute(
            {"operation": "get_issue", "issue_key": "DDM-1"},
            _MinimalCtx(),
        )

    assert result["error"].startswith("Jira API returned 401")
    assert "ATATT-secret-token-999" not in result["error"]
    assert "REDACTED" in result["error"]
    assert "ATATT-secret-token-999" not in caplog.text


# ---------------------------------------------------------------------------
# add_attachment size caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_attachment_over_the_cap_is_refused_before_sending(tmp_path):
    """attach_file reads the whole file into memory on a worker thread; a
    50MB+ recording must be refused with advice, not uploaded and 413'd."""
    from agent_core.tools.jira import _MAX_ATTACHMENT_BYTES

    big = tmp_path / ".tmp" / "media" / "conv" / "recording_ab12cd34.webm"
    big.parent.mkdir(parents=True)
    with open(big, "wb") as fh:
        fh.truncate(_MAX_ATTACHMENT_BYTES + 1)

    client = AsyncMock()
    result = await JiraTool()._op_add_attachment(
        {"issue_key": "DDM-1", "file_path": ".tmp/media/conv/recording_ab12cd34.webm"},
        client,
        _ctx(project_fs_path=str(tmp_path)),
    )

    assert "Attachment too large" in result["error"]
    assert "trace.zip" in result["error"]  # the actionable alternative
    assert result["size"] == _MAX_ATTACHMENT_BYTES + 1
    client.attach_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_attachment_maps_upstream_413(tmp_path):
    """The instance's own limit can be lower than ours — the 413 must come
    back as the same advice instead of a raw Jira API error."""
    import httpx

    clip = tmp_path / "clip.webm"
    clip.write_bytes(b"webm" * 4)

    def _boom(key, path):
        resp = httpx.Response(
            413,
            text='{"message": "Attachment is too large"}',
            request=httpx.Request("POST", "https://jira.example.com/rest/api/2/issue/DDM-1/attachments"),
        )
        raise httpx.HTTPStatusError("Payload Too Large", request=resp.request, response=resp)

    client = AsyncMock()
    client.attach_file = AsyncMock(side_effect=_boom)

    result = await JiraTool()._op_add_attachment(
        {"issue_key": "DDM-1", "file_path": "clip.webm"}, client, _ctx(project_fs_path=str(tmp_path))
    )

    assert "413" in result["error"]
    assert "trace.zip" in result["error"]


@pytest.mark.asyncio
async def test_add_attachment_passes_through_other_errors(tmp_path):
    """Only 413 is reworded; everything else keeps the tool's generic
    HTTP-error path (and its credential redaction)."""
    import httpx

    clip = tmp_path / "clip.webm"
    clip.write_bytes(b"webm")

    resp = httpx.Response(
        404,
        text="Not Found",
        request=httpx.Request("POST", "https://jira.example.com/rest/api/2/issue/DDM-1/attachments"),
    )
    client = AsyncMock()
    client.attach_file = AsyncMock(
        side_effect=httpx.HTTPStatusError("Not Found", request=resp.request, response=resp)
    )
    client.api_token = "tok"
    client.email = "agent@example.com"

    tool = JiraTool()
    with patch.object(tool, "_build_client", new=AsyncMock(return_value=client)):
        result = await tool.execute(
            {"operation": "add_attachment", "issue_key": "DDM-1", "file_path": "clip.webm"},
            _ctx(project_fs_path=str(tmp_path)),
        )

    assert result["error"].startswith("Jira API returned 404")


@pytest.mark.asyncio
async def test_add_attachment_rejects_a_directory(tmp_path):
    """is_file(), not exists(): reading a directory raises IsADirectoryError
    inside the worker thread and surfaces as a generic failure."""
    (tmp_path / "tests").mkdir()
    client = AsyncMock()
    result = await JiraTool()._op_add_attachment(
        {"issue_key": "DDM-1", "file_path": "tests"}, client, _ctx(project_fs_path=str(tmp_path))
    )
    assert "Not a file" in result["error"]
    client.attach_file.assert_not_awaited()
