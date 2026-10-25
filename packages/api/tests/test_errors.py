"""Structured error helper contract (api/errors.py)."""
from __future__ import annotations

from fastapi import HTTPException

from api.errors import api_detail, api_error, error_message


def test_api_error_shape():
    exc = api_error(404, "THING_NOT_FOUND", "Thing not found", extra_field="x")
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 404
    assert exc.detail == {
        "code": "THING_NOT_FOUND",
        "message": "Thing not found",
        "extra_field": "x",
    }


def test_api_error_no_extras():
    exc = api_error(400, "BAD_INPUT", "Bad input")
    assert exc.detail == {"code": "BAD_INPUT", "message": "Bad input"}


def test_api_detail_normalizes_missing_keys():
    assert api_detail({"message": "boom"}) == {"code": "INTERNAL_ERROR", "message": "boom"}
    assert api_detail({}) == {"code": "INTERNAL_ERROR", "message": "Internal server error"}


def test_api_detail_renames_detail_key():
    assert api_detail({"detail": "legacy"}) == {
        "code": "INTERNAL_ERROR",
        "message": "legacy",
    }


def test_error_message_from_api_error():
    exc = api_error(422, "X_INVALID", "X is invalid")
    assert error_message(exc) == "X is invalid"


def test_error_message_from_plain_string_detail():
    exc = HTTPException(status_code=400, detail="plain text")
    assert error_message(exc) == "plain text"


def test_error_message_from_non_http_exception():
    assert error_message(ValueError("oops")) == "oops"


def test_error_message_empty_str_falls_back_to_type():
    assert error_message(RuntimeError("")) == "RuntimeError"
