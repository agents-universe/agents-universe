"""Upload payload resolver — source handling, containment, and caps."""
from __future__ import annotations

import base64

import pytest

from agent_core.tools._uploads import (
    MAX_INLINE_BYTES,
    UploadSourceError,
    resolve_upload_spec,
    resolve_upload_specs,
)
from agent_core.tools.base import ToolContext


def make_context(tmp_path, attachments: dict[str, bytes] | None = None) -> ToolContext:
    store = dict(attachments or {})
    return ToolContext(
        project_id="p",
        project_fs_path=str(tmp_path),
        conversation_id="c",
        user_id="u",
        upload_file_lookup=store.get if attachments is not None else None,
        upload_file_names=(lambda: list(store)) if attachments is not None else None,
    )


def test_path_source_reads_workspace_file(tmp_path):
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    target = tmp_path / "tests" / "fixtures" / "orders.csv"
    target.write_bytes(b"id,total\n1,42\n")
    payload = resolve_upload_spec(make_context(tmp_path), {"source": "path", "path": "tests/fixtures/orders.csv"})
    assert payload.data == b"id,total\n1,42\n"
    assert payload.name == "orders.csv"
    assert payload.mime_type == "text/csv"
    assert payload.source == "path"


def test_path_source_rejects_traversal_and_absolute_paths(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_bytes(b"secret")
    ctx = make_context(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    for rel in (f"../{outside.name}", str(outside)):
        with pytest.raises(UploadSourceError, match="outside the project workspace"):
            resolve_upload_spec(ctx, {"source": "path", "path": rel})


def test_path_source_blocks_hidden_files_but_allows_tmp(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_bytes(b"[core]")
    (tmp_path / ".env").write_bytes(b"SECRET=1")
    media = tmp_path / ".tmp" / "media" / "c"
    media.mkdir(parents=True)
    (media / "shot.png").write_bytes(b"\x89PNG")

    ctx = make_context(tmp_path)
    for rel in (".git/config", ".env"):
        with pytest.raises(UploadSourceError, match="Hidden files"):
            resolve_upload_spec(ctx, {"source": "path", "path": rel})

    allowed = resolve_upload_spec(ctx, {"source": "path", "path": ".tmp/media/c/shot.png"})
    assert allowed.data == b"\x89PNG"
    assert allowed.source == "path"


def test_path_source_reports_missing_file(tmp_path):
    with pytest.raises(UploadSourceError, match="File not found"):
        resolve_upload_spec(make_context(tmp_path), {"source": "path", "path": "nope.txt"})


def test_path_source_enforces_size_cap(tmp_path):
    (tmp_path / "big.bin").write_bytes(b"x" * 65)
    with pytest.raises(UploadSourceError, match="above the 64B limit"):
        resolve_upload_spec(make_context(tmp_path), {"source": "path", "path": "big.bin"}, max_bytes=64)


def test_virtual_media_path_prefers_attachment_store(tmp_path):
    media = tmp_path / ".tmp" / "media" / "c"
    media.mkdir(parents=True)
    (media / "report.csv").write_bytes(b"from-disk")
    ctx = make_context(tmp_path, {"report.csv": b"from-memory"})
    payload = resolve_upload_spec(ctx, {"path": ".tmp/media/c/report.csv"})
    assert payload.data == b"from-memory"
    assert payload.source == "attachment"


def test_attachment_source_lookup_and_miss_hint(tmp_path):
    ctx = make_context(tmp_path, {"upload_ab12.csv": b"a,b\n"})
    payload = resolve_upload_spec(ctx, {"source": "attachment", "name": "upload_ab12.csv"})
    assert payload.data == b"a,b\n"
    assert payload.name == "upload_ab12.csv"

    with pytest.raises(UploadSourceError, match="Available: upload_ab12.csv"):
        resolve_upload_spec(ctx, {"source": "attachment", "name": "missing.csv"})


def test_attachment_source_without_store_explains_fallback(tmp_path):
    ctx = make_context(tmp_path)
    with pytest.raises(UploadSourceError, match="use source='path' or source='inline'"):
        resolve_upload_spec(ctx, {"source": "attachment", "name": "x.csv"})


def test_inline_text_source_defaults_to_text_plain(tmp_path):
    payload = resolve_upload_spec(
        make_context(tmp_path),
        {"source": "inline", "name": "payload", "content": "id,total\n"},
    )
    assert payload.data == b"id,total\n"
    assert payload.mime_type == "text/plain"
    assert payload.source == "inline"


def test_inline_base64_source_decodes_and_infers_mime(tmp_path):
    raw = b"\x89PNG\r\n\x1a\n"
    payload = resolve_upload_spec(
        make_context(tmp_path),
        {"source": "inline", "name": "logo.png", "content_base64": base64.b64encode(raw).decode()},
    )
    assert payload.data == raw
    assert payload.mime_type == "image/png"


def test_inline_rejects_bad_base64_without_echoing_it(tmp_path):
    with pytest.raises(UploadSourceError, match="not valid base64") as exc:
        resolve_upload_spec(
            make_context(tmp_path),
            {"source": "inline", "name": "x.bin", "content_base64": "not base64!!!SECRET"},
        )
    assert "SECRET" not in str(exc.value)


def test_inline_rejects_both_content_forms_and_missing_name(tmp_path):
    ctx = make_context(tmp_path)
    with pytest.raises(UploadSourceError, match="not both"):
        resolve_upload_spec(ctx, {"source": "inline", "name": "x", "content": "a", "content_base64": "YQ=="})
    with pytest.raises(UploadSourceError, match="requires 'name'"):
        resolve_upload_spec(ctx, {"source": "inline", "content": "a"})


def test_inline_enforces_decoded_size_cap(tmp_path):
    with pytest.raises(UploadSourceError, match="above the 64B limit"):
        resolve_upload_spec(
            make_context(tmp_path),
            {"source": "inline", "name": "x.bin", "content": "y" * 65},
            inline_max_bytes=64,
        )


def test_filename_sanitization_strips_paths_and_control_chars(tmp_path):
    payload = resolve_upload_spec(
        make_context(tmp_path),
        {"source": "inline", "name": "../../etc/pa\r\nsswd", "content": "x"},
    )
    assert payload.name == "passwd"
    with pytest.raises(UploadSourceError, match="requires 'name'"):
        resolve_upload_spec(make_context(tmp_path), {"source": "inline", "name": "..", "content": "x"})


def test_invalid_mime_falls_back_to_inference(tmp_path):
    payload = resolve_upload_spec(
        make_context(tmp_path),
        {"source": "inline", "name": "data.json", "content": "{}", "mime_type": "not a mime"},
    )
    assert payload.mime_type == "application/json"


def test_field_is_carried_for_multipart_consumers(tmp_path):
    payload = resolve_upload_spec(
        make_context(tmp_path),
        {"source": "inline", "name": "a.txt", "content": "x", "field": "attachment"},
    )
    assert payload.field == "attachment"


def test_source_is_inferred_when_omitted(tmp_path):
    assert resolve_upload_spec(make_context(tmp_path), {"content": "x", "name": "a.txt"}).source == "inline"
    with pytest.raises(UploadSourceError, match="Provide one of"):
        resolve_upload_spec(make_context(tmp_path), {})


def test_unknown_source_is_rejected(tmp_path):
    with pytest.raises(UploadSourceError, match="Unknown upload source 'ftp'"):
        resolve_upload_spec(make_context(tmp_path), {"source": "ftp", "path": "a.txt"})


def test_specs_list_validation_and_caps(tmp_path):
    ctx = make_context(tmp_path)
    spec = {"source": "inline", "name": "a.txt", "content": "x"}
    assert len(resolve_upload_specs(ctx, [spec])) == 1
    with pytest.raises(UploadSourceError, match="at least one entry"):
        resolve_upload_specs(ctx, [])
    with pytest.raises(UploadSourceError, match="stringified JSON"):
        resolve_upload_specs(ctx, '[{"source": "inline"}]')
    with pytest.raises(UploadSourceError, match="must be an array"):
        resolve_upload_specs(ctx, {"source": "inline"})
    with pytest.raises(UploadSourceError, match="Too many files"):
        resolve_upload_specs(ctx, [spec] * 3, max_files=2)


def test_default_inline_cap_is_one_megabyte():
    assert MAX_INLINE_BYTES == 1024 * 1024
