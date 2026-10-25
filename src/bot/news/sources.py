"""The official news sources (tier A) and how they are fetched (docs/intel-plan.md, section 5).

Every source is an official body's own RSS or Atom feed. A source is used only after `bot news check`,
run on the server, has fetched and read it (`~/news/check.json`). The check shows what each feed
returned; it does not review the publisher's terms of use, which the plan leaves to Sultan.

Fetching is plain HTTPS with a timeout and a size cap. The request carries a generic user agent; a
contact address is added only if NEWS_CONTACT is set in the environment (the SEC asks for one and
refuses requests without it), and nothing else about the account or its owner is ever sent.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

TIMEOUT_S = 15
MAX_BYTES = 2_000_000
USER_AGENT = "crypto-bot-research/1.0 (reads public official feeds once a day)"
CHECK_FILE = "check.json"


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    url: str
    category: str               # central bank, us data, regulation
    tier: str = "A"
    needs_contact: bool = False
    crypto_only: bool = False   # keep only items that mention crypto (regulators publish on everything)
    keywords: tuple[str, ...] = ()  # keep only items whose title has one of these (empty: keep all)


SOURCES = (
    Source("fed_monetary", "Federal Reserve: monetary policy", "https://www.federalreserve.gov/feeds/press_monetary.xml",
           "central bank"),
    Source("ecb_press", "European Central Bank: press releases", "https://www.ecb.europa.eu/rss/press.html",
           "central bank", keywords=("monetary policy", "interest rate", "key ecb interest rates")),
    Source("bls_latest", "US Bureau of Labor Statistics: latest releases", "https://www.bls.gov/feed/bls_latest.rss",
           "us data", keywords=("consumer price index", "employment situation", "producer price index")),
    Source("sec_press", "US SEC: press releases", "https://www.sec.gov/news/pressreleases.rss", "regulation",
           needs_contact=True, crypto_only=True),
    Source("cftc_press", "US CFTC: press releases", "https://www.cftc.gov/RSS/RSSGP/rssgp.xml", "regulation",
           crypto_only=True),
)

Http = Callable[[str, dict[str, str]], tuple[int, bytes]]


class FetchError(RuntimeError):
    pass


def _https(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            body = r.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise FetchError(f"the feed is larger than {MAX_BYTES // 1_000_000} MB")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, OSError) as e:
        raise FetchError(f"network: {type(e).__name__}: {getattr(e, 'reason', e)}") from None


def headers(source: Source, environ: dict[str, str] | None = None) -> dict[str, str] | None:
    """The request headers, or None when the source needs a contact that is not configured."""
    contact = (os.environ if environ is None else environ).get("NEWS_CONTACT", "").strip()
    if source.needs_contact and not contact:
        return None
    ua = f"{USER_AGENT} {contact}".strip() if contact else USER_AGENT
    return {"User-Agent": ua, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}


def fetch(source: Source, http: Http = _https, environ: dict[str, str] | None = None) -> bytes:
    h = headers(source, environ)
    if h is None:
        raise FetchError("skipped: this publisher asks for a contact address (set NEWS_CONTACT on the server)")
    status, body = http(source.url, h)
    if status != 200:
        raise FetchError(f"HTTP {status}")
    if not body.strip():
        raise FetchError("empty answer")
    return body


@dataclass(frozen=True)
class SourceCheck:
    key: str
    name: str
    url: str
    ok: bool
    items: int = 0
    undated: int = 0
    newest: str | None = None
    problem: str = ""
    filtered: int = 0
    left_out: tuple[str, ...] = ()       # a few titles the filter did not keep, to check the filter is right


def save_check(checks: list[SourceCheck], folder: Path, now: float) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / CHECK_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"checked_at": now, "sources": [asdict(c) for c in checks]}, indent=1))
    os.replace(tmp, path)
    return path


def verified(folder: Path) -> set[str]:
    """Keys of the sources the last `bot news check` read successfully (none if it never ran)."""
    try:
        d = json.loads((Path(folder) / CHECK_FILE).read_text())
        return {s["key"] for s in d.get("sources", []) if s.get("ok")}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def check_time(folder: Path) -> float | None:
    try:
        return float(json.loads((Path(folder) / CHECK_FILE).read_text())["checked_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def utc(t: float | None) -> str | None:
    return None if t is None else time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))
