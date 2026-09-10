"""api_request multipart/form-data — file parts, form fields, auth, refusals.

The failures worth guarding are the silent ones: httpx drops `json` when
`files` is set, and a caller-supplied Content-Type discards the generated
boundary. Both produce a request that looks successful and reaches the
upstream unparseable, so the tool refuses them outright.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from api_request_fakes import FakeResponse, FakeSession, _mock_stream_response, make_context
from agent_core.tools.api_request import ApiRequestTool


async def _run(params, http, session=None, context=None):
    ctx = context or make_context(http=http, session=session)
    return await ApiRequestTool().execute(params, ctx)


def _kwargs(http) -> dict:
    return http.stream.call_args.kwargs


def _base(**overrides) -> dict:
    params = {
        "integration_key": "svc",
        "method": "POST",
        "path": "/upload",
        "base_url": "https://api.example.com",
        "auth_type": "none",
    }
    params.update(overrides)
    return params


@pytest.fixture
def http():
    return _mock_stream_response(AsyncMock(), FakeResponse(200, {"ok": True}))


async def test_inline_upload_becomes_a_multipart_part(http):
    payload = base64.b64encode(b"id,total\n1,42\n").decode()
    result = await _run(
        _base(
            files=[
                {
                    "field": "attachment",
                    "source": "inline",
                    "name": "orders.csv",
                    "mime_type": "text/csv",
                    "content_base64": payload,
                }
            ],
            require_confirmation=False,
        ),
        http,
        FakeSession(),
    )

    assert result["status"] == 200
    files = _kwargs(http)["files"]
    assert files == [("attachment", ("orders.csv", b"id,total\n1,42\n", "text/csv"))]
    assert "json" not in _kwargs(http)
    # The result reports what was sent — never the bytes, and never under
    # "files" (agent.py turns result["files"] into user-facing deliverables).
    assert result["uploaded"] == [
        {
            "name": "orders.csv",
            "field": "attachment",
            "size": 14,
            "mime_type": "text/csv",
            "source": "inline",
        }
    ]
    assert "files" not in result


async def test_workspace_path_upload_and_form_fields(http, tmp_path):
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    ctx = make_context(http=http, session=FakeSession(), project_fs_path=tmp_path)

    result = await _run(
        _base(
            files=[{"source": "path", "path": "fixtures/logo.png", "field": "image"}],
            form_fields={"caption": "chart", "count": 2, "tags": ["a", "b"]},
        ),
        http,
        context=ctx,
    )

    assert result["status"] == 200
    assert _kwargs(http)["files"] == [("image", ("logo.png", b"\x89PNG\r\n\x1a\n", "image/png"))]
    # ints and sequences are stringified exactly as a browser form would submit
    # them — httpx passes a dict value through otherwise.
    assert _kwargs(http)["data"] == {"caption": "chart", "count": "2", "tags": ["a", "b"]}


async def test_conversation_attachment_upload(http):
    ctx = make_context(
        http=http,
        session=FakeSession(),
        upload_file_lookup=lambda name: b"ATTACHED" if name == "shot.png" else None,
    )

    result = await _run(
        _base(files=[{"source": "attachment", "name": "shot.png"}]),
        http,
        context=ctx,
    )

    assert result["status"] == 200
    assert _kwargs(http)["files"] == [("file", ("shot.png", b"ATTACHED", "image/png"))]


async def test_upload_without_form_fields_sends_no_data(http):
    await _run(
        _base(files=[{"source": "inline", "name": "a.txt", "content": "hello"}]),
        http,
        FakeSession(),
    )
    assert "data" not in _kwargs(http)


async def test_files_with_json_body_is_refused(http):
    result = await _run(
        _base(
            files=[{"source": "inline", "name": "a.txt", "content": "hello"}],
            json_body={"q": 1},
        ),
        http,
        FakeSession(),
    )

    assert "mutually exclusive" in result["error"]
    assert http.stream.call_args is None  # nothing was sent


async def test_files_on_get_is_refused(http):
    result = await _run(
        _base(
            method="GET",
            files=[{"source": "inline", "name": "a.txt", "content": "hello"}],
        ),
        http,
        FakeSession(),
    )

    assert "cannot carry a multipart body" in result["error"]
    assert http.stream.call_args is None


async def test_form_fields_without_files_is_refused(http):
    result = await _run(_base(form_fields={"a": "b"}), http, FakeSession())

    assert "form_fields requires files" in result["error"]
    assert http.stream.call_args is None


async def test_manual_content_type_is_refused(http):
    result = await _run(
        _base(
            files=[{"source": "inline", "name": "a.txt", "content": "hello"}],
            headers={"Content-Type": "application/json"},
        ),
        http,
        FakeSession(),
    )

    assert "multipart boundary" in result["error"]
    assert http.stream.call_args is None


async def test_unresolvable_file_returns_the_source_error(http):
    result = await _run(
        _base(files=[{"source": "path", "path": "../../etc/passwd"}]),
        http,
        FakeSession(),
    )

    assert "outside the project workspace" in result["error"]
    assert http.stream.call_args is None


async def test_body_field_auth_lands_in_the_form_not_a_json_body(http):
    """body_field auth with an upload must send the key as a form field;
    writing it into json_body instead would ship an unauthenticated request."""
    with patch("agent_core.tools._auth.get_secret_optional", new=AsyncMock(return_value="k-9")):
        result = await _run(
            _base(
                auth_type="body_field",
                auth_field_name="apiKey",
                secret_ref="svc:key",
                files=[{"source": "inline", "name": "a.txt", "content": "hello"}],
                form_fields={"q": "1"},
            ),
            http,
            FakeSession(),
        )

    assert result["status"] == 200
    assert _kwargs(http)["data"] == {"q": "1", "apiKey": "k-9"}
    assert "json" not in _kwargs(http)
    # The plaintext key never reaches the tool result the LLM and the DB see.
    assert "k-9" not in json.dumps(result)


async def test_form_field_size_caps(http):
    result = await _run(
        _base(
            files=[{"source": "inline", "name": "a.txt", "content": "x"}],
            form_fields={"big": "y" * (100 * 1024 + 1)},
        ),
        http,
        FakeSession(),
    )

    assert "per-field limit" in result["error"]
    assert http.stream.call_args is None


async def test_form_field_nested_object_is_refused(http):
    """A nested dict would stringify to Python repr and reach the server as
    garbage — name the mistake instead of submitting it."""
    result = await _run(
        _base(
            files=[{"source": "inline", "name": "a.txt", "content": "x"}],
            form_fields={"nested": {"a": 1}},
        ),
        http,
        FakeSession(),
    )

    assert "must be a string or a list of strings" in result["error"]
    assert http.stream.call_args is None


async def test_error_message_does_not_echo_inline_content(http):
    """A bad spec must not bounce the payload back into the LLM context."""
    secret_looking = "sk-live-SUPERSECRET123"
    result = await _run(
        _base(files=[{"source": "inline", "name": "a.txt", "content_base64": secret_looking}]),
        http,
        FakeSession(),
    )

    assert "SUPERSECRET" not in json.dumps(result)
    assert "not valid base64" in result["error"]
