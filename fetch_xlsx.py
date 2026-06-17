#!/usr/bin/env python3
"""
fetch_xlsx.py — Find and download Oslo kommune's latest property roster XLSX.

The kommune publishes the file at a hashed CloudFront URL on a content page;
the URL itself rotates with every six-month release. The durable identifier
is the *page* + the anchor text "(XLSX)" (which disambiguates from the
sibling PDF link).

Used by both the GitHub Actions sync workflow and local developers:

    poetry run python fetch_xlsx.py            # → writes the kommune's filename
    poetry run python fetch_xlsx.py -o foo.xlsx
    poetry run python fetch_xlsx.py --url-only # print the URL, don't download
"""
import argparse
import os
import re
import sys
from pathlib import Path

import requests


def _load_env_file(path: str = "source.env") -> None:
    """Tiny dotenv reader so we can resolve SOURCE_HOST etc. without a shell
    `source` call. Pre-existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        os.environ.setdefault(key.strip(), val)


_load_env_file()

DEFAULT_HOST = os.environ.get("SOURCE_HOST", "https://www.oslo.kommune.no")
DEFAULT_PAGE_PATH = os.environ.get(
    "SOURCE_PAGE_PATH",
    "/plan-bygg-og-eiendom/kart-og-eiendomsinformasjon/kommunal-eiendom/eiendomsoversikt/",
)
DEFAULT_MARKER = os.environ.get("XLSX_LINK_MARKER", "(XLSX)")
USER_AGENT = "kombo/1.0 (+https://github.com/deviationist/kombo)"

ANCHOR_RE = re.compile(
    r"""<a[^>]+href=["']([^"']+)["'][^>]*>(.*?)</a>""", re.S
)
TAG_RE = re.compile(r"<[^>]+>")


def find_xlsx_url(
    host: str = DEFAULT_HOST,
    page_path: str = DEFAULT_PAGE_PATH,
    marker: str = DEFAULT_MARKER,
    session: requests.Session | None = None,
) -> str | None:
    """Scrape the eiendomsoversikt page for the XLSX download link.

    Returns the absolute URL of the file, or None if no matching anchor.
    Format-agnostic: works against both the current `/get-file/<id>/<hash>`
    URLs and the older `/getfile.php/<id>-<timestamp>/...` ones, because we
    match by anchor *text* containing the marker, not by href pattern.
    """
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    r = sess.get(host + page_path, timeout=30)
    r.raise_for_status()
    for m in ANCHOR_RE.finditer(r.text):
        href = m.group(1)
        text = re.sub(r"\s+", " ", TAG_RE.sub(" ", m.group(2))).strip()
        if marker in text:
            return href if href.startswith("http") else host + href
    return None


def download_xlsx(
    url: str,
    out_path: Path | None = None,
    session: requests.Session | None = None,
) -> Path:
    """Stream the XLSX to disk. Uses the Content-Disposition filename when
    out_path is None — i.e. preserves the kommune's "per mai 2026" name.

    Raises ValueError if the response isn't actually a spreadsheet (cheap
    defence against the link-finder picking up the PDF sibling).
    """
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    with sess.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "spreadsheet" not in ctype and "openxml" not in ctype:
            raise ValueError(
                f"expected an XLSX, got Content-Type {ctype!r} from {url}"
            )
        if out_path is None:
            cd = r.headers.get("content-disposition", "")
            m = re.search(r'filename="?([^"]+)"?', cd)
            out_path = Path(m.group(1) if m else "source.xlsx")
        out_path = Path(out_path)
        with out_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("-o", "--out", help="Output path. Default = kommune's filename.")
    ap.add_argument("--url-only", action="store_true", help="Print URL, don't download.")
    args = ap.parse_args()

    session = requests.Session()
    url = find_xlsx_url(session=session)
    if not url:
        print(
            f"error: no anchor containing {DEFAULT_MARKER!r} on "
            f"{DEFAULT_HOST}{DEFAULT_PAGE_PATH}",
            file=sys.stderr,
        )
        return 1

    if args.url_only:
        print(url)
        return 0

    out = download_xlsx(url, Path(args.out) if args.out else None, session=session)
    size = out.stat().st_size
    print(f"saved {out} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
