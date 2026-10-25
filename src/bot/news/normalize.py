"""One record per news item, from a source's RSS or Atom feed (docs/intel-plan.md, section 6).

Each item keeps its source and tier, title, link, publication time in UTC, the time it was read, the
coins it concerns and its category. An item without a readable publication time, or dated more than
10 minutes in the future, is dropped and counted, never given a guessed time. Duplicates (the same
link, or the same source and the same title) are merged. Confidence comes from the source's tier,
never from the text. Nothing here predicts what a price will do.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from bot.news.sources import Source

FUTURE_SLACK_S = 600
ATOM = "{http://www.w3.org/2005/Atom}"
COIN_WORDS = {
    "BTCUSDT": ("bitcoin", "btc"),
    "ETHUSDT": ("ethereum", "ether ", "eth "),
    "XRPUSDT": ("xrp", "ripple"),
    "BNBUSDT": ("bnb", "binance"),
}
CRYPTO_WORDS = ("crypto", "digital asset", "bitcoin", "ethereum", "stablecoin", "blockchain", "token", "virtual currenc")
CONFIDENCE = {"A": "high (official source)", "B": "medium (established news organisation)", "C": "low (unverified)"}
IMPLICATIONS = {
    "central bank": "Rate and policy decisions can move all risk assets, crypto included; volatility often "
                    "rises around the announcement.",
    "us data": "Inflation and jobs data feed expectations for interest rates, which can move crypto with "
               "other risk assets.",
    "regulation": "Rules and enforcement can affect the coins, exchanges or products named; the effect is "
                  "often specific rather than market-wide.",
}


class FeedError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    source: str
    source_name: str
    tier: str
    category: str
    title: str
    link: str
    published: float            # epoch seconds, UTC
    fetched: float
    coins: tuple[str, ...] = ()  # empty: all coins (a macro item)

    @property
    def confidence(self) -> str:
        return CONFIDENCE.get(self.tier, "unknown")

    @property
    def implication(self) -> str:
        return IMPLICATIONS.get(self.category, "")


@dataclass
class Parsed:
    items: list[Item] = field(default_factory=list)
    undated: int = 0
    future: int = 0
    filtered: int = 0
    left_out: list[str] = field(default_factory=list)      # titles the source's filter did not keep


def _text(el: ET.Element | None) -> str:
    return re.sub(r"\s+", " ", (el.text or "") if el is not None else "").strip()


def _when(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        d = parsedate_to_datetime(raw)                     # RSS: RFC 822
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00"))   # Atom: RFC 3339
        except ValueError:
            return None
    if d is None:
        return None
    if d.tzinfo is None:
        return None                                         # no time zone: the time is not reliable
    return d.astimezone(timezone.utc).timestamp()


def coins_of(text: str) -> tuple[str, ...]:
    t = f" {text.lower()} "
    return tuple(s for s, words in COIN_WORDS.items() if any(w in t for w in words))


def is_crypto(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in CRYPTO_WORDS) or bool(coins_of(text))


def parse(source: Source, body: bytes, fetched: float) -> Parsed:
    """Items from an RSS 2.0 or Atom feed. A body that is not such a feed raises FeedError."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise FeedError(f"not a readable feed ({e})") from None
    out = Parsed()
    if root.tag == f"{ATOM}feed":
        entries = [(_text(e.find(f"{ATOM}title")),
                    (e.find(f"{ATOM}link").get("href", "") if e.find(f"{ATOM}link") is not None else ""),
                    _text(e.find(f"{ATOM}published")) or _text(e.find(f"{ATOM}updated")))
                   for e in root.iter(f"{ATOM}entry")]
    elif root.tag in ("rss", "{http://purl.org/rss/1.0/}RDF", "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF"):
        entries = [(_text(i.find("title")), _text(i.find("link")),
                    _text(i.find("pubDate")) or _text(i.find("{http://purl.org/dc/elements/1.1/}date")))
                   for i in root.iter() if i.tag in ("item", "{http://purl.org/rss/1.0/}item")]
    else:
        raise FeedError(f"not an RSS or Atom feed (root element {root.tag!r})")
    for title, link, raw in entries:
        if not title:
            continue
        if (source.keywords and not any(k in title.lower() for k in source.keywords)) or \
                (source.crypto_only and not is_crypto(title)):
            out.filtered += 1
            out.left_out.append(title)
            continue
        when = _when(raw)
        if when is None:
            out.undated += 1
            continue
        if when > fetched + FUTURE_SLACK_S:
            out.future += 1
            continue
        out.items.append(Item(source.key, source.name, source.tier, source.category, title, link, when, fetched,
                              coins_of(title)))
    return out


def _key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def dedupe(items: list[Item]) -> list[Item]:
    """The earliest copy of each story: the same link, or the same source and the same title."""
    seen_links: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()
    out = []
    for it in sorted(items, key=lambda i: (i.published, i.source, i.title)):
        k = (it.source, _key(it.title))
        if (it.link and it.link in seen_links) or k in seen_titles:
            continue
        if it.link:
            seen_links.add(it.link)
        seen_titles.add(k)
        out.append(it)
    return out


def recent(items: list[Item], now: float, hours: int = 24) -> list[Item]:
    return [i for i in items if now - hours * 3600 <= i.published <= now + FUTURE_SLACK_S]
