"""Grammar loading unit tests — no git repo, no real grammar downloads.

get_grammar must never permanently cache a failure: the language pack
downloads grammars on first use, so a missing grammar has to be retried on
later calls (the download may have landed meanwhile). Prefetch and the
failure warning each fire at most once per key per process.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from unittest.mock import patch

from agent_core.knowledge.graph import languages


@contextmanager
def _isolate():
    """Fresh module state so per-test retries/warnings start from zero."""
    with patch.object(languages, "_GRAMMAR_CACHE", {}), \
         patch.object(languages, "_PREFETCH_TRIED", set()), \
         patch.object(languages, "_WARNED", set()):
        yield


def test_none_is_not_cached_later_call_succeeds():
    sentinel = object()
    calls = {"n": 0}

    def fake_load(key: str):
        calls["n"] += 1
        # fail before the prefetch AND right after it; succeed on the next call
        return sentinel if calls["n"] >= 3 else None

    with _isolate(), \
         patch.object(languages, "_load_grammar", side_effect=fake_load), \
         patch("tree_sitter_language_pack.prefetch"):
        assert languages.get_grammar("java") is None      # fails, prefetches
        assert languages.get_grammar("java") is sentinel  # retry succeeds
        assert languages.get_grammar("java") is sentinel  # success is cached


def test_prefetch_attempted_at_most_once_per_key():
    with _isolate(), \
         patch.object(languages, "_load_grammar", return_value=None), \
         patch("tree_sitter_language_pack.prefetch") as pref:
        tried = languages._PREFETCH_TRIED  # the patched set
        for _ in range(3):
            languages.get_grammar("java")
    assert pref.call_count == 1
    assert "java" in tried


def test_warn_once_per_key_across_repeated_failures(caplog):
    with _isolate(), \
         patch.object(languages, "_load_grammar", return_value=None), \
         caplog.at_level(logging.WARNING):
        tried = languages._PREFETCH_TRIED  # the patched set
        warned = languages._WARNED
        # prefetch already tried for this key -> straight to the warn branch
        tried.add("java")
        languages.get_grammar("java")
        languages.get_grammar("java")
    msgs = [r.message for r in caplog.records if "unavailable" in r.message]
    assert len(msgs) == 1
    assert "java" in warned


def test_fallback_key_used_when_primary_missing():
    sentinel = object()

    def fake_load(key: str):
        return sentinel if key == "javascript" else None

    with _isolate(), \
         patch.object(languages, "_load_grammar", side_effect=fake_load), \
         patch("tree_sitter_language_pack.prefetch"):
        assert languages.get_grammar("jsx") is sentinel
