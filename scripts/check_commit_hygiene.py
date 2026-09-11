#!/usr/bin/env python3
"""Commit hygiene checks — the enforcement behind CLAUDE.md convention 14.

Two independent checks, both configured by `.commit-hygiene.toml`:

  identity   The author/committer email must not contain a banned substring,
             so a commit is never made under a corporate address.
  urls       URL hosts in file content must be `example`-family, reserved, or
             explicitly allowlisted, so intranet/corporate hosts cannot leak
             into samples, fixtures and docs.

Usage:
    python scripts/check_commit_hygiene.py identity [--rev-range A..B]
    python scripts/check_commit_hygiene.py urls [--all-files | FILE...]

Run by the pre-commit hooks `commit-identity` / `url-policy` and by the CI
`hygiene` job. Exit codes: 0 clean, 1 violations, 2 usage/config error.

A line containing the policy's `allow_marker` is exempt from the URL check —
the escape hatch for deliberate fixtures (security tests, docs quoting a
hostile host).
"""
from __future__ import annotations

import argparse
import fnmatch
import functools
import importlib.util
import ipaddress
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / ".commit-hygiene.toml"

# `_inet_aton_mapped` is the inet_aton semantics helper from agent-core; it
# lives in a stdlib-only module, loaded by path so this script needs neither an
# installed agent-core nor its dependencies (httpx, frontmatter, ...).
INET_ATON_PATH = REPO_ROOT / "packages/agent-core/src/agent_core/tools/_ssrf.py"

# Schemes whose URLs carry a network host worth checking. SQLAlchemy-style
# `driver+scheme://` URLs are reduced to the bare scheme before the lookup.
NETWORK_SCHEMES = frozenset({
    "http", "https", "ws", "wss", "ftp", "postgres", "postgresql", "mysql",
    "mssql", "redis", "mongodb", "amqp", "ssh", "git", "socks5",
})

# Reserved / documentation-only TLDs and hosts (RFC 2606, RFC 6761).
RESERVED_TLDS = frozenset({"example", "test", "invalid", "localhost"})

# Documentation address blocks (RFC 5737) — safe as literals in examples.
TEST_NET = tuple(
    ipaddress.ip_network(net)
    for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)

# URL characters per RFC 3986 — bounding the match this way keeps prose, CJK
# punctuation and markdown out of the captured host.
SCHEME_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9+.\-]{1,31})://([A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+)"
)
EMAIL_RE = re.compile(r"<([^<>]*)>")
TRAILING_JUNK = "`'\"<>()[]{},;:!?*.、，。；：！？）」』】》"
MAX_FILE_BYTES = 5 * 1024 * 1024

# Windows consoles default to a legacy code page; findings quote file content.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")


@functools.cache
def _inet_aton_mapped(host: str) -> str | None:
    """Dotted quad a numeric host resolves to under inet_aton semantics."""
    spec = importlib.util.spec_from_file_location("_hygiene_ssrf", INET_ATON_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - broken checkout
        raise SystemExit(f"commit-hygiene: cannot load {INET_ATON_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._inet_aton_mapped(host)


def load_policy(path: Path = POLICY_PATH) -> dict:
    try:
        with path.open("rb") as fh:
            policy = tomllib.load(fh)
    except OSError as exc:
        raise SystemExit(f"commit-hygiene: cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"commit-hygiene: invalid TOML in {path}: {exc}") from exc

    email = policy.setdefault("email", {})
    url = policy.setdefault("url", {})
    email.setdefault("banned_substrings", [])
    url.setdefault("allow_marker", "hygiene:allow-url")
    url.setdefault("allowed_hosts", [])
    url.setdefault("blocked_tlds", [])
    url.setdefault("allowed_ips", [])
    url.setdefault("skip_paths", [])
    # Normalise once so lookups are cheap and case cannot hide a match.
    email["banned_substrings"] = [s.lower() for s in email["banned_substrings"]]
    url["allowed_hosts"] = [h.lower().lstrip("*.") for h in url["allowed_hosts"]]
    url["blocked_tlds"] = [t.lower().lstrip(".") for t in url["blocked_tlds"]]
    return policy


def _git(*args: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else None


def _allowed_host(host: str, allowed: list[str]) -> bool:
    """An allowlist entry also covers its subdomains (github.com → api.github.com)."""
    return any(host == entry or host.endswith("." + entry) for entry in allowed)


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    mapped = _inet_aton_mapped(host)
    return ipaddress.ip_address(mapped) if mapped else None


def host_verdict(host: str, policy: dict) -> str | None:
    """Return None when *host* is acceptable, else a short reason it is not."""
    url = policy["url"]
    if _allowed_host(host, url["allowed_hosts"]):
        return None

    tld = host.rsplit(".", 1)[-1]
    if tld in url["blocked_tlds"]:
        return f"ends in .{tld}, an intranet/corporate suffix"

    if "example" in host.split(".") or tld in RESERVED_TLDS:
        return None

    ip = _as_ip(host)
    if ip is not None:
        if ip.is_loopback or str(ip) in url["allowed_ips"]:
            return None
        if any(ip in net for net in TEST_NET):
            return None
        if ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return None
        return f"{host} is a private/link-local IP address"

    return "host is neither example-family nor allowlisted"


# A reported URL can carry credentials (`https://user:pass@host/x`) — never
# echo those into the terminal or CI logs.
USERINFO_RE = re.compile(r"//[^/@\s]+@")


def _redact_url(url: str) -> str:
    return USERINFO_RE.sub("//[REDACTED:USERINFO]@", url)


def _host_of(raw_url: str) -> str | None:
    try:
        host = urlsplit(raw_url.rstrip(TRAILING_JUNK)).hostname
    except ValueError:  # e.g. "http://[::1" — invalid IPv6, not a host we can judge
        return None
    return host.lower() if host else None


def iter_urls(path: Path, marker: str):
    """Yield (line number, host, url) for every checkable URL in *path*."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return
        data = path.read_bytes()
    except OSError:
        return
    if b"\0" in data[:8192]:
        return  # binary

    text = data.decode("utf-8-sig", errors="replace")
    for lineno, line in enumerate(text.splitlines(), 1):
        if marker in line:
            continue
        seen: set[str] = set()
        for match in SCHEME_RE.finditer(line):
            if match.group(1).split("+")[0].lower() not in NETWORK_SCHEMES:
                continue
            raw = match.group(0).rstrip(TRAILING_JUNK)
            host = _host_of(raw)
            if not host or host in seen:
                continue
            if any(char in host for char in "${}%*<>") or host == "...":
                continue  # `${HOST}` / `%s` style template, not a real host
            if "." not in host and not host[0].isdigit():
                continue  # single label: container service name or placeholder
            seen.add(host)
            yield lineno, host, raw


def _skipped(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def check_files(paths, policy: dict) -> list[tuple[str, int, str, str, str]]:
    findings = []
    for raw in paths:
        display = str(raw).replace("\\", "/")
        if _skipped(display, policy["url"]["skip_paths"]):
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = REPO_ROOT / path
        for lineno, host, url in iter_urls(path, policy["url"]["allow_marker"]):
            reason = host_verdict(host, policy)
            if reason:
                findings.append((display, lineno, host, reason, url))
    return findings


def _banned_hits(email: str, banned: list[str]) -> list[str]:
    low = email.lower()
    return [sub for sub in banned if sub in low]


def check_identity(policy: dict) -> list[str]:
    """Emails of the identity git would use for the commit being created."""
    problems = []
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        ident = _git("var", var)
        if ident is None:
            raise SystemExit(f"commit-hygiene: `git var {var}` failed")
        match = EMAIL_RE.search(ident)
        email = (match.group(1) if match else "").strip()
        role = "author" if var == "GIT_AUTHOR_IDENT" else "committer"
        for sub in _banned_hits(email, policy["email"]["banned_substrings"]):
            problems.append(f'{role} email "{email}" contains banned substring "{sub}"')
    return problems


def check_rev_range(rev_range: str, policy: dict) -> list[str]:
    """Emails recorded on the commits in *rev_range* (CI: pushed commits)."""
    log = _git("log", "--format=%H%x00%ae%x00%ce", rev_range)
    if log is None:
        raise SystemExit(f"commit-hygiene: `git log {rev_range}` failed (bad rev range?)")
    problems = []
    for line in log.splitlines():
        fields = line.split("\x00")
        if len(fields) != 3:
            continue
        sha, author, committer = (field.strip() for field in fields)
        if not sha:
            continue
        for role, email in (("author", author), ("committer", committer)):
            for sub in _banned_hits(email, policy["email"]["banned_substrings"]):
                problems.append(
                    f'{sha[:10]} {role} email "{email}" contains banned substring "{sub}"'
                )
    return problems


def _tracked_files() -> list[str]:
    listing = _git("ls-files", "-z")
    if listing is None:
        raise SystemExit("commit-hygiene: `git ls-files` failed")
    return [name for name in listing.split("\0") if name]


def _report_identity(problems: list[str]) -> int:
    if not problems:
        return 0
    print("commit-hygiene: commit identity failed the email rule")
    for problem in problems:
        print(f"  - {problem}")
    print("  See CLAUDE.md convention 14 and the [email] section of .commit-hygiene.toml.")
    return 1


def _report_urls(findings: list[tuple[str, int, str, str, str]]) -> int:
    if not findings:
        return 0
    print(f"commit-hygiene: {len(findings)} URL host violation(s)")
    for path, lineno, host, reason, url in findings:
        print(f"  {path}:{lineno}: {host} — {reason}")
        print(f"      {_redact_url(url)}")
    print("  Use reserved example.com-family hosts, or add a reviewed entry to the")
    print("  [url] allowed_hosts list in .commit-hygiene.toml (or mark the line with")
    print('  the allow_marker, e.g. "# hygiene:allow-url").')
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_commit_hygiene.py",
        description="Enforce the commit identity and URL host rules of CLAUDE.md convention 14.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    identity = sub.add_parser("identity", help="check author/committer email")
    identity.add_argument(
        "--rev-range",
        metavar="REV",
        help="check the commit metadata of REV or A..B instead of the current identity",
    )

    urls = sub.add_parser("urls", help="check URL hosts in file content")
    urls.add_argument("files", nargs="*", help="files to scan (as passed by pre-commit)")
    urls.add_argument(
        "--all-files",
        action="store_true",
        help="scan every tracked file (CI mode; pre-commit's own exclude does not apply)",
    )

    args = parser.parse_args(argv)
    policy = load_policy()

    if args.command == "identity":
        problems = (
            check_rev_range(args.rev_range, policy)
            if args.rev_range
            else check_identity(policy)
        )
        return _report_identity(problems)

    if args.all_files:
        files = _tracked_files()
        if args.files:
            parser.error("--all-files cannot be combined with explicit files")
    else:
        files = args.files
        if not files:
            parser.error("pass files to scan, or --all-files")
    return _report_urls(check_files(files, policy))


if __name__ == "__main__":
    sys.exit(main())
