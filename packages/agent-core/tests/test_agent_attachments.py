"""User-attachment multimodal content building tests."""
from __future__ import annotations

from agent_core.agent import build_user_content


def _att(**overrides: object) -> dict:
    base = {
        "id": "a1",
        "url": "/api/media/p/c/f1.png",
        "name": "f1.png",
        "media_type": "image/png",
        "size": 100,
        "rel_path": ".tmp/media/c/f1.png",
    }
    base.update(overrides)
    return base


def test_no_attachments_returns_raw_string():
    assert build_user_content("你好", None, True) == "你好"
    assert build_user_content("你好", [], False) == "你好"


def test_vision_provider_gets_image_parts():
    content = build_user_content(
        "看看这张图",
        [_att(image_data="aGVsbG8=", image_media_type="image/png")],
        True,
    )
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看看这张图"}
    assert content[1] == {"type": "image", "media_type": "image/png", "data": "aGVsbG8="}


def test_non_vision_provider_degrades_image_to_path_ref():
    content = build_user_content(
        "看看这张图",
        [_att(image_data="aGVsbG8=", image_media_type="image/png")],
        False,
    )
    assert isinstance(content, list)
    assert content[1]["type"] == "text"
    assert ".tmp/media/c/f1.png" in content[1]["text"]


def test_inline_text_attachment_with_header():
    content = build_user_content(
        "读一下这个",
        [_att(name="data.csv", media_type="text/csv", inline_text="a,b\n1,2\n", rel_path=".tmp/media/c/data.csv")],
        True,
    )
    assert isinstance(content, list)
    assert content[1] == {"type": "text", "text": "### Attachment: data.csv\na,b\n1,2\n"}


def test_binary_attachment_path_ref():
    content = build_user_content(
        "分析这个文件",
        [_att(name="book.xlsx", media_type="application/octet-stream", rel_path=".tmp/media/c/book.xlsx")],
        True,
    )
    assert isinstance(content, list)
    assert content[1]["type"] == "text"
    text = content[1]["text"]
    assert "book.xlsx" in text
    assert ".tmp/media/c/book.xlsx" in text
