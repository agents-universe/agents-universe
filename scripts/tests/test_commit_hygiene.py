"""Tests for scripts/check_commit_hygiene.py.

Run from the repo root:  python -m pytest scripts/tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_commit_hygiene as hygiene  # noqa: E402

POLICY = hygiene.load_policy()


# --- policy ---------------------------------------------------------------


def test_policy_bans_the_configured_email_substring():
    assert "mer" in POLICY["email"]["banned_substrings"]


# --- host verdict ---------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "api.example.com",
        "h.example.com",
        "example.atlassian.net",
        "example.openai.azure.com",  # `example` as a non-final label
        "localhost",
        "site.test",
        "does-not-exist.invalid",
    ],
)
def test_example_family_hosts_pass(host):
    assert hygiene.host_verdict(host, POLICY) is None


@pytest.mark.parametrize(
    "host",
    [
        "github.com",
        "api.github.com",  # subdomain of an allowlisted entry
        "agents-universe.com",
        "agent.agents-universe.com",
        "host.docker.internal",
        "www.w3.org",
    ],
)
def test_allowlisted_hosts_pass(host):
    assert hygiene.host_verdict(host, POLICY) is None


@pytest.mark.parametrize("host", ["intranet.example-inc.com", "jira.contoso-internal.com"])
def test_unknown_multi_label_hosts_fail(host):
    assert hygiene.host_verdict(host, POLICY)


def test_blocked_tld_beats_an_embedded_example_label():
    assert hygiene.host_verdict("jira.example.corp.internal", POLICY)


@pytest.mark.parametrize("host", ["192.168.1.50", "10.1.2.3", "169.254.1.1", "3232235520"])
def test_private_and_link_local_ips_fail(host):
    assert hygiene.host_verdict(host, POLICY)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "198.51.100.9", "8.8.8.8", "169.254.169.254"])
def test_loopback_testnet_and_allowed_ips_pass(host):
    assert hygiene.host_verdict(host, POLICY) is None


@pytest.mark.parametrize("host", ["2130706433", "0x7f000001", "017700000001", "127.1"])
def test_inet_aton_loopback_forms_pass(host):
    assert hygiene.host_verdict(host, POLICY) is None


def test_inet_aton_metadata_form_is_covered_by_the_allowlist():
    assert hygiene.host_verdict("2852039166", POLICY) is None


# --- URL extraction -------------------------------------------------------


def _scan(tmp_path: Path, text: str):
    path = tmp_path / "sample.txt"
    path.write_text(text, encoding="utf-8")
    return list(hygiene.iter_urls(path, POLICY["url"]["allow_marker"]))


def test_host_is_read_after_userinfo(tmp_path):
    hits = _scan(tmp_path, "http://scanner:pw@proxy.example.com:8080/x")
    assert [host for _, host, _ in hits] == ["proxy.example.com"]


def test_placeholders_and_single_label_hosts_are_skipped(tmp_path):
    text = "https://${host}/a\nhttp://test\nhttps://host/agent/x\nredis://redis:6379/0\n"
    assert _scan(tmp_path, text) == []


def test_non_network_schemes_are_skipped(tmp_path):
    text = "sqlite+aiosqlite:///:memory:\nfile:///etc/passwd\nagenthttps://x/y\n"
    assert _scan(tmp_path, text) == []


def test_match_stops_at_markdown_and_cjk_punctuation(tmp_path):
    hits = _scan(tmp_path, "见 https://agents-universe.com)，并且正在被它自己管理。** 末尾")
    assert [host for _, host, _ in hits] == ["agents-universe.com"]


def test_allow_marker_exempts_the_line(tmp_path):
    assert _scan(tmp_path, "https://intranet.example-inc.com/x  # hygiene:allow-url\n") == []


def test_one_finding_per_host_per_line(tmp_path):
    hits = _scan(tmp_path, "https://a.example.com/x and https://a.example.com/y")
    assert [host for _, host, _ in hits] == ["a.example.com"]


def test_check_files_reports_file_and_reason(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text("see https://jira.contoso-internal.com/x\n", encoding="utf-8")  # hygiene:allow-url
    findings = hygiene.check_files([str(path)], POLICY)
    assert [(f[2], f[1]) for f in findings] == [("jira.contoso-internal.com", 1)]


def test_reported_url_hides_credentials(tmp_path, capsys):
    path = tmp_path / "bad.env"
    path.write_text(
        "DATABASE_URL=postgresql://svc:not-a-real-secret@db.contoso-internal.com:5432/app\n",  # hygiene:allow-url
        encoding="utf-8",
    )
    findings = hygiene.check_files([str(path)], POLICY)
    assert [f[2] for f in findings] == ["db.contoso-internal.com"]
    assert hygiene._report_urls(findings) == 1
    out = capsys.readouterr().out
    assert "not-a-real-secret" not in out
    assert "[REDACTED:USERINFO]@" in out


def test_check_files_honours_skip_paths(tmp_path, monkeypatch):
    path = tmp_path / "package-lock.json"
    path.write_text('{"url": "https://internal.example-inc.com/x"}\n', encoding="utf-8")  # hygiene:allow-url
    monkeypatch.setitem(POLICY["url"], "skip_paths", ["*package-lock.json"])
    assert hygiene.check_files([str(path)], POLICY) == []


# --- identity -------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "noreply@github.com",
        "agents-universe@localhost",
        "34120374+agents-universe@users.noreply.github.com",
    ],
)
def test_identities_without_a_banned_substring_pass(email):
    assert hygiene._banned_hits(email, POLICY["email"]["banned_substrings"]) == []


def test_banned_substring_is_case_insensitive():
    assert hygiene._banned_hits("Someone@ACMER.com", POLICY["email"]["banned_substrings"]) == ["mer"]


def test_unusable_rev_range_fails_loudly():
    with pytest.raises(SystemExit):
        hygiene.check_rev_range("definitely-not-a-ref..HEAD", POLICY)


# --- the repository itself ------------------------------------------------


def test_tracked_files_are_clean():
    """Guard rail for the state the CI job asserts: no URL host violations."""
    assert hygiene.check_files(hygiene._tracked_files(), POLICY) == []
