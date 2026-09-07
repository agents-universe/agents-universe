"""Refresh the vendored tree-sitter grammars baked into the Docker image.

The runtime sandbox has no network and the language pack downloads grammars
lazily at first use, so the image bakes them in from ``docker/ts-grammars/``
(a plain ``COPY`` in the Dockerfile — the build never touches the network).
GitHub releases is frequently unreachable from build environments, so this
script pulls the official pack tarball through a chain of public mirrors,
verifies its sha256 against the official manifest, and re-extracts the
grammars the repo graph needs (``libtree_sitter_<lang>.so`` — the name the
pack expects on Linux).

Usage (needs tar with zstd support — Git Bash on Windows, or any Linux):
    python scripts/fetch_ts_grammars.py
    python scripts/fetch_ts_grammars.py --langs python java ruby

Only the manifest + one tarball are downloaded (~22 MB), then the requested
grammars are extracted; the tarball itself is not committed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

PACK_VERSION = "1.14.3"
BASE_URL = (
    "https://github.com/xberg-io/tree-sitter-language-pack/releases/download/"
    f"v{PACK_VERSION}"
)
MANIFEST = "parsers.json"
TARBALL = "parsers-linux-x86_64.tar.zst"

# Default languages: everything the repo graph registers in languages.py.
DEFAULT_LANGS = ["python", "typescript", "tsx", "javascript", "vue", "java"]

# GitHub-release mirrors, tried in order before the direct URL. Public
# proxies for github.com/release assets; swap freely if one goes away.
MIRRORS = ["https://ghfast.top", "https://gh-proxy.com", "https://ghproxy.net"]

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "docker" / "ts-grammars"


def _candidates(path: str) -> list[str]:
    url = f"{BASE_URL}/{path}"
    return [f"{mirror}/{url}" for mirror in MIRRORS] + [url]


def _fetch(path: str, expected_sha: str | None, workdir: Path) -> Path:
    """Download one file through the mirror chain into workdir; verify sha."""
    dest = workdir / path
    last_error: Exception | None = None
    for url in _candidates(path):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "agent-world"})
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read()
            if expected_sha is not None:
                digest = hashlib.sha256(data).hexdigest()
                if digest != expected_sha:
                    last_error = ValueError(
                        f"sha256 mismatch for {url}: got {digest}, want {expected_sha}"
                    )
                    continue
            dest.write_bytes(data)
            return dest
        except Exception as exc:  # noqa: BLE001 — mirror chain, try the next
            last_error = exc
    raise RuntimeError(f"all download routes failed for {path}: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--langs",
        nargs="+",
        default=DEFAULT_LANGS,
        help=f"grammar names to vendor (default: {DEFAULT_LANGS})",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="tsg-") as tmp:
        workdir = Path(tmp)
        manifest_path = _fetch(MANIFEST, None, workdir)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        platform = manifest["platforms"]["linux-x86_64"]
        tarball_path = _fetch(TARBALL, platform["sha256"], workdir)
        print(f"downloaded {TARBALL} ({tarball_path.stat().st_size} bytes), sha256 ok")

        extract_dir = workdir / "extract"
        extract_dir.mkdir()
        subprocess.run(
            ["tar", "-xf", str(tarball_path), "-C", str(extract_dir)], check=True
        )

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for lang in args.langs:
            src = extract_dir / f"libtree_sitter_{lang}.so"
            if not src.is_file():
                print(f"WARN: grammar {lang!r} not in tarball, skipped", file=sys.stderr)
                continue
            dst = OUT_DIR / src.name
            dst.write_bytes(src.read_bytes())
            print(f"vendored {dst.relative_to(REPO_ROOT)} ({dst.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
