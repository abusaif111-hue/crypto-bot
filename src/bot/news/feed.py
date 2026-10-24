"""Read the official sources: the items of the last day and each source's health, never a crash."""
from __future__ import annotations

from bot.news.normalize import FeedError, Item, dedupe, parse
from bot.news.sources import SOURCES, FetchError, Http, Source, SourceCheck, _https, fetch, utc


def read_all(now: float, only: set[str] | None = None, sources: tuple[Source, ...] = SOURCES, http: Http = _https,
             environ: dict[str, str] | None = None) -> tuple[list[SourceCheck], list[Item]]:
    """Every source (or only the verified ones), its health, and all dated items, duplicates merged."""
    checks, items = [], []
    for s in sources:
        if only is not None and s.key not in only:
            checks.append(SourceCheck(s.key, s.name, s.url, False, problem="not checked yet (run: bot news check)"))
            continue
        try:
            got = parse(s, fetch(s, http, environ), now)
        except (FetchError, FeedError) as e:
            checks.append(SourceCheck(s.key, s.name, s.url, False, problem=str(e)[:200]))
            continue
        newest = max((i.published for i in got.items), default=None)
        problem = "" if got.items or got.filtered else "the feed has no dated items"
        if got.future:
            problem = f"{got.future} items dated in the future were left out"
        checks.append(SourceCheck(s.key, s.name, s.url, not problem.startswith("the feed has no"), len(got.items),
                                  got.undated, utc(newest), problem))
        items += got.items
    return checks, dedupe(items)
