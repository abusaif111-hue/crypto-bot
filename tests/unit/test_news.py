"""Official news (docs/intel-plan.md, step 5): readable times only, duplicates merged, never a guess."""
import argparse
import ast
import io
import json
from pathlib import Path

import pytest

from bot import cli, daily_report as dr
from bot.news import feed, normalize, sources
from bot.news.sources import FetchError, Source, SourceCheck

NOW = 1_792_830_000.0                                          # 2026-10-24 03:40 UTC
SRC = Path(__file__).resolve().parents[2] / "src" / "bot" / "news"
FED = Source("fed", "Federal Reserve: monetary policy", "https://fed.example/feed.xml", "central bank")
SEC = Source("sec", "US SEC: press releases", "https://sec.example/rss", "regulation", needs_contact=True,
             crypto_only=True)
BLS = Source("bls", "US BLS", "https://bls.example/rss", "us data", keywords=("consumer price index",))


def rss(*items):
    body = "".join(f"<item><title>{t}</title><link>{link}</link>{f'<pubDate>{d}</pubDate>' if d else ''}</item>"
                   for t, link, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


FED_FEED = rss(("Federal Reserve issues FOMC statement", "https://fed.example/a", "Wed, 21 Oct 2026 18:00:00 GMT"),
               ("Minutes of the Federal Open Market Committee", "https://fed.example/b", ""),
               ("A statement from the future", "https://fed.example/c", "Sat, 31 Oct 2026 18:00:00 GMT"),
               ("Federal Reserve issues FOMC statement", "https://fed.example/a", "Wed, 21 Oct 2026 18:00:00 GMT"))


# ---------------------------------------------------------------- reading a feed
def test_an_rss_item_keeps_its_source_time_and_link():
    p = normalize.parse(FED, FED_FEED, NOW)
    first = p.items[0]
    assert (first.source, first.tier, first.category, first.link) == ("fed", "A", "central bank", "https://fed.example/a")
    assert first.published == 1_792_605_600.0 and first.fetched == NOW            # 2026-10-21 18:00 UTC
    assert first.confidence == "high (official source)" and "volatility" in first.implication
    assert first.coins == ()                                                         # a macro item: all coins


def test_items_without_a_readable_time_or_from_the_future_are_left_out_and_counted():
    p = normalize.parse(FED, FED_FEED, NOW)
    assert p.undated == 1 and p.future == 1 and len(p.items) == 2                  # the duplicate is merged later
    no_zone = rss(("FOMC statement", "https://fed.example/d", "2026-10-21T18:00:00"))
    assert normalize.parse(FED, no_zone, NOW).undated == 1                          # a time without a zone is not used


def test_an_atom_feed_is_read_too():
    atom = (b'<feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>'
            b'<entry><title>Key ECB interest rates unchanged</title><link href="https://ecb.example/1"/>'
            b'<updated>2026-10-23T12:15:00Z</updated></entry></feed>')
    (it,) = normalize.parse(FED, atom, NOW).items
    assert it.link == "https://ecb.example/1" and it.published == 1_792_757_700.0


def test_filters_keep_only_what_matters_to_crypto():
    sec = rss(("SEC charges investment adviser", "https://sec.example/1", "Fri, 23 Oct 2026 14:00:00 GMT"),
              ("SEC approves rule on crypto asset custody", "https://sec.example/2", "Fri, 23 Oct 2026 15:00:00 GMT"),
              ("Statement on Bitcoin exchange-traded products", "https://sec.example/3", "Fri, 23 Oct 2026 16:00:00 GMT"))
    p = normalize.parse(SEC, sec, NOW)
    assert [i.link for i in p.items] == ["https://sec.example/2", "https://sec.example/3"] and p.filtered == 1
    assert p.items[1].coins == ("BTCUSDT",)
    bls = rss(("Consumer Price Index - September 2026", "https://bls.example/1", "Thu, 22 Oct 2026 12:30:00 GMT"),
              ("Import and Export Price Indexes", "https://bls.example/2", "Thu, 22 Oct 2026 12:30:00 GMT"))
    assert [i.title for i in normalize.parse(BLS, bls, NOW).items] == ["Consumer Price Index - September 2026"]


def test_coins_are_found_by_name():
    assert normalize.coins_of("Statement on spot Ether and Bitcoin products") == ("BTCUSDT", "ETHUSDT")
    assert normalize.coins_of("Order against Binance Holdings") == ("BNBUSDT",)
    assert normalize.coins_of("Ripple settlement approved") == ("XRPUSDT",)
    assert normalize.coins_of("Interest rates unchanged") == ()


@pytest.mark.parametrize("body", [b"<html><body>Not found</body></html>", b"not xml at all", b"<rss><channel>"])
def test_something_that_is_not_a_feed_is_an_error_not_an_empty_day(body):
    with pytest.raises(normalize.FeedError):
        normalize.parse(FED, body, NOW)


def test_an_xml_bomb_is_refused():
    levels = ['<!ENTITY l0 "lol">'] + [f'<!ENTITY l{i} "{("&l" + str(i - 1) + ";") * 10}">' for i in range(1, 10)]
    bomb = (f'<?xml version="1.0"?><!DOCTYPE rss [{"".join(levels)}]>'
            '<rss><channel><item><title>&l9;</title></item></channel></rss>').encode()   # 3 x 10^9 characters
    with pytest.raises(normalize.FeedError):
        normalize.parse(FED, bomb, NOW)


def test_duplicates_are_merged_keeping_the_earliest():
    a = normalize.parse(FED, FED_FEED, NOW).items
    same_story = rss(("Federal Reserve issues FOMC statement.", "https://other.example/x", "Wed, 21 Oct 2026 19:00:00 GMT"))
    b = normalize.parse(FED, same_story, NOW).items                      # same source, same title, new link
    other = normalize.parse(Source("o", "Other", "u", "central bank"), same_story, NOW).items
    merged = normalize.dedupe(a + b + other)
    assert len(merged) == 2 and merged[0].published == 1_792_605_600.0      # the earliest copy, and the other source


def test_recent_means_the_last_24_hours():
    items = normalize.dedupe(normalize.parse(FED, FED_FEED, NOW).items)
    assert normalize.recent(items, NOW) == [] and normalize.recent(items, 1_792_605_600.0 + 3600) == items


# ---------------------------------------------------------------- fetching
def test_a_failed_fetch_says_what_happened():
    with pytest.raises(FetchError, match="HTTP 404"):
        sources.fetch(FED, http=lambda url, h: (404, b""))
    with pytest.raises(FetchError, match="empty"):
        sources.fetch(FED, http=lambda url, h: (200, b"  "))


def test_only_a_generic_user_agent_is_sent_and_a_contact_only_when_set():
    seen = {}

    def http(url, h):
        seen.update(h)
        return 200, FED_FEED
    sources.fetch(FED, http=http, environ={})
    assert set(seen) == {"User-Agent", "Accept"} and seen["User-Agent"] == sources.USER_AGENT
    with pytest.raises(FetchError, match="NEWS_CONTACT"):
        sources.fetch(SEC, http=http, environ={})
    sources.fetch(SEC, http=http, environ={"NEWS_CONTACT": "ops@example.org"})
    assert seen["User-Agent"].endswith("ops@example.org")


def test_a_huge_answer_is_cut_off(monkeypatch):
    class Big:
        status = 200

        def read(self, n):
            return b"x" * n

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(sources.urllib.request, "urlopen", lambda req, timeout: Big())
    with pytest.raises(FetchError, match="larger than"):
        sources._https("https://x.example", {})


# ---------------------------------------------------------------- all sources, the check record
def fake_http(table):
    def http(url, h):
        got = table.get(url)
        if isinstance(got, Exception):
            raise got
        return got or (404, b"")
    return http


def test_one_broken_source_never_hides_the_others():
    srcs = (FED, Source("bad", "Broken", "https://bad.example", "central bank"), BLS)
    checks, items = feed.read_all(NOW, sources=srcs, environ={},
                                  http=fake_http({FED.url: (200, FED_FEED), "https://bad.example": FetchError("network: timeout")}))
    by = {c.key: c for c in checks}
    assert by["fed"].ok and by["fed"].items == 2 and by["fed"].undated == 1 and "future" in by["fed"].problem
    assert not by["bad"].ok and by["bad"].problem == "network: timeout"
    assert not by["bls"].ok and by["bls"].problem == "HTTP 404"
    assert [i.link for i in items] == ["https://fed.example/a"]


def test_only_verified_sources_are_read():
    calls = []
    checks, _ = feed.read_all(NOW, only={"fed"}, sources=(FED, BLS), environ={},
                              http=lambda url, h: calls.append(url) or (200, FED_FEED))
    assert calls == [FED.url] and checks[1].problem.startswith("not checked yet")


def test_the_check_record_round_trips_and_a_damaged_one_trusts_nothing(tmp_path):
    sources.save_check([SourceCheck("fed", "F", "u", True), SourceCheck("bls", "B", "u", False, problem="x")], tmp_path, NOW)
    assert sources.verified(tmp_path) == {"fed"} and sources.check_time(tmp_path) == NOW
    (tmp_path / sources.CHECK_FILE).write_text("{broken")
    assert sources.verified(tmp_path) == set() and sources.verified(tmp_path / "none") == set()


# ---------------------------------------------------------------- the commands
def test_news_check_records_what_works(tmp_path):
    out = io.StringIO()
    http = fake_http({sources.SOURCES[0].url: (200, FED_FEED)})
    assert cli._news_check(argparse.Namespace(news_dir=str(tmp_path)), http=http, out=out) == 0
    text = out.getvalue()
    assert "OK   Federal Reserve: monetary policy: 2 items kept" in text and "FAIL" in text
    assert sources.verified(tmp_path) == {sources.SOURCES[0].key}
    assert "1 of 5 sources work" in text


def test_news_show_needs_a_check_first(tmp_path, capsys):
    a = argparse.Namespace(news_dir=str(tmp_path), hours=24)
    assert cli._news_show(a, http=fake_http({})) == 2 and "bot news check" in capsys.readouterr().err
    sources.save_check([SourceCheck(sources.SOURCES[0].key, "F", "u", True)], tmp_path, NOW)
    out = io.StringIO()
    a.hours = 24 * 365 * 10
    assert cli._news_show(a, http=fake_http({sources.SOURCES[0].url: (200, FED_FEED)}), out=out) == 0
    assert "Federal Reserve issues FOMC statement" in out.getvalue() and "confidence high" in out.getvalue()


def test_the_commands_parse(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_news_check", lambda args: seen.setdefault("args", args) and 0)
    cli.main(["news", "check"])
    assert seen["args"].news_dir == "~/news"


# ---------------------------------------------------------------- in the daily report
def news_section(news, now=1_792_605_600.0 + 7200):
    r = dr.build(now, [], [], [], [], {}, news=news)
    return dict(r.sections)["News"], dict(r.sections)["Data quality and API health"]


def test_the_report_lists_recent_official_items_with_source_time_and_link():
    items = normalize.parse(FED, FED_FEED, NOW).items
    lines, _ = news_section(([SourceCheck("fed", FED.name, FED.url, True, 2)], items))
    assert lines[0] == ("2026-10-21 18:00 UTC: Federal Reserve: monetary policy: Federal Reserve issues FOMC "
                        "statement [all coins; central bank; confidence high (official source)] https://fed.example/a")
    assert lines[1].startswith("    possible implications: Rate and policy decisions")
    assert "not scored" in lines[-2] and lines[-1] == dr.NOT_COVERED


def test_an_item_from_before_the_daily_close_is_marked():
    items = normalize.parse(FED, FED_FEED, NOW).items
    lines, _ = news_section(([SourceCheck("fed", FED.name, FED.url, True, 2)], items), now=1_792_627_200.0 + 600)
    assert "(before the last daily close)" in lines[0]                         # 18:00 on the 21st, report on the 22nd


def test_a_quiet_day_and_an_unchecked_server_say_so():
    lines, _ = news_section(([SourceCheck("fed", FED.name, FED.url, True, 0)], []))
    assert lines[0] == "No new items in the last 24 hours from 1 official sources (Federal Reserve: monetary policy)."
    lines, data = news_section(([SourceCheck("fed", FED.name, FED.url, False, problem="not checked yet (run: x)")], []))
    assert "No official source has passed `bot news check`" in lines[0]
    assert not any("news source" in x for x in data)                          # unchecked is not a fault


def test_a_failing_source_shows_in_data_health():
    _, data = news_section(([SourceCheck("fed", FED.name, FED.url, True, 1),
                             SourceCheck("bls", "US BLS", "u", False, problem="HTTP 503")], []))
    assert "WARNING: news source US BLS: HTTP 503." in data


def test_news_cannot_trade_or_reach_risk_code():
    for f in SRC.glob("*.py"):
        tree = ast.parse(f.read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
               {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m and m.startswith(("bot.execution", "bot.exchange", "bot.risk", "bot.paper", "bot.backtest",
                                           "bot.strategies", "bot.forward", "bot.market")) for m in mods), (f, mods)
    text = (SRC / "sources.py").read_text()
    assert "@gmail" not in text and json.dumps(sources.USER_AGENT).count("@") == 0


def test_the_same_link_from_two_sources_is_one_story():
    one = rss(("FOMC statement", "https://fed.example/same", "Wed, 21 Oct 2026 18:00:00 GMT"))
    two = rss(("Fed holds rates", "https://fed.example/same", "Wed, 21 Oct 2026 18:05:00 GMT"))
    other = Source("o", "Other", "u", "central bank")
    merged = normalize.dedupe(normalize.parse(FED, one, NOW).items + normalize.parse(other, two, NOW).items)
    assert [i.title for i in merged] == ["FOMC statement"]


def test_the_same_title_from_one_source_under_two_links_is_one_story():
    feed_ = rss(("Fed holds rates", "https://fed.example/1", "Wed, 21 Oct 2026 18:00:00 GMT"),
                ("Fed holds rates!", "https://fed.example/2", "Wed, 21 Oct 2026 18:30:00 GMT"),
                ("Fed raises rates", "https://fed.example/3", "Wed, 21 Oct 2026 19:00:00 GMT"))
    assert [i.link for i in normalize.dedupe(normalize.parse(FED, feed_, NOW).items)] == [
        "https://fed.example/1", "https://fed.example/3"]


def test_every_item_of_every_working_source_is_kept():
    two = rss(("FOMC statement", "https://fed.example/1", "Wed, 21 Oct 2026 18:00:00 GMT"),
              ("FOMC minutes", "https://fed.example/2", "Thu, 22 Oct 2026 18:00:00 GMT"))
    bls = rss(("Consumer Price Index - September 2026", "https://bls.example/1", "Thu, 22 Oct 2026 12:30:00 GMT"))
    _, items = feed.read_all(NOW, sources=(FED, BLS), environ={},
                             http=fake_http({FED.url: (200, two), BLS.url: (200, bls)}))
    assert sorted(i.link for i in items) == ["https://bls.example/1", "https://fed.example/1", "https://fed.example/2"]


def test_the_check_shows_what_a_filter_left_out(tmp_path):
    ecb = sources.SOURCES[1]
    body = rss(*[(f"Speech by board member {i}", f"https://ecb.example/{i}", "Fri, 23 Oct 2026 10:00:00 GMT")
                 for i in range(5)])
    out = io.StringIO()
    cli._news_check(argparse.Namespace(news_dir=str(tmp_path)), http=fake_http({ecb.url: (200, body)}), out=out)
    text = out.getvalue()
    assert f"OK   {ecb.name}: 0 items kept; 5 left out by the filter" in text and "newest None" not in text
    assert text.count("left out: Speech by board member") == feed.SAMPLE
    saved = json.loads((tmp_path / sources.CHECK_FILE).read_text())["sources"][1]
    assert saved["filtered"] == 5 and len(saved["left_out"]) == 3
