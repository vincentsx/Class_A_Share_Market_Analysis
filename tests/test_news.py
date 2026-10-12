from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from stock_daily.config import SHANGHAI
from stock_daily.db import news_fetches, news_items, snapshots
from stock_daily.news import (
    PublicNewsFeed,
    build_news,
    collect_news,
    normalize,
    refresh_snapshot_news,
    safe_url,
    topic_matches,
)
from stock_daily.providers import DataError

DAY = "20261012"
NOON = datetime(2026, 10, 12, 11, 35, tzinfo=SHANGHAI)
CLOSE = datetime(2026, 10, 12, 15, 5, tzinfo=SHANGHAI)


@pytest.fixture
def news_settings(settings):
    return replace(settings, source="baostock", news_enabled=True)


def raw(title="人工智能新政策发布", published="2026-10-12 11:30:00", content="软件股份发布大模型产品"):
    return {"title": title, "showTime": published, "summary": content, "code": "202610123892700001"}


class Feed:
    def __init__(self, entries=None, fail=()):
        self.entries = entries if entries is not None else [raw()]
        self.fail = fail
        self.calls = []

    def fetch(self, provider, captured):
        self.calls.append(provider)
        if provider in self.fail:
            raise httpx.ReadTimeout("private detail must not be logged")
        if provider == "em":
            return self.entries
        return [{"id": 123 + index, "title": r.get("title"), "content": r.get("summary"), "level": "B",
                 "ctime": datetime.fromisoformat(r["showTime"]).replace(tzinfo=SHANGHAI).timestamp()}
                for index, r in enumerate(self.entries)]


def report(slot="midday", captured=NOON):
    return {"day": DAY, "slot": slot, "captured_at": captured.isoformat(),
            "quotes": [
                {"code": "000001.SZ", "name": "软件股份", "industry": "I65软件和信息技术服务业", "pct": 2,
                 "volume_ratio": 1.8},
                {"code": "600000.SH", "name": "浦发银行", "industry": "J66货币金融服务", "pct": -1,
                 "volume_ratio": 0.6},
            ],
            "focus": [{"code": "000001.SZ"}],
            "industries": [
                {"name": "I65软件和信息技术服务业", "pct": 2, "up_ratio": 75, "active": True},
                {"name": "J66货币金融服务", "pct": -1, "up_ratio": 25, "active": False},
            ]}


def test_sources_are_deduplicated_and_connected_to_direct_mentions_and_industries(db, news_settings):
    collect_news(db, news_settings, feed=Feed(), captured=NOON)
    news = build_news(db, news_settings, report())
    assert news["status"] == "available" and news["count"] == 1
    item = news["items"][0]
    assert len(item["sources"]) == 2
    assert item["topics"] == ["人工智能"]
    assert item["mentioned_stocks"][0]["code"] == "000001.SZ"
    assert item["mentioned_stocks"][0]["watched"] is True
    assert item["related_sectors"][0]["basis"] == "直接提及个股所属行业"
    assert news["topics"][0]["count"] == 1
    assert not item["after_quote"]


def test_first_seen_is_immutable_and_later_revisions_do_not_leak_back(db, news_settings):
    feed = Feed()
    collect_news(db, news_settings, feed=feed, captured=NOON)
    collect_news(db, news_settings, force=True, feed=feed, captured=CLOSE)
    assert {r["first_seen_at"] for r in db.rows(select(news_items))} == {NOON.isoformat()}
    revised = Feed([raw(content="软件股份否认人工智能产品的市场传闻")])
    collect_news(db, news_settings, force=True, feed=revised, captured=CLOSE)
    original = build_news(db, news_settings, report())
    assert not original["items"][0]["risk_words"]
    later = build_news(db, news_settings, report("close", CLOSE))
    assert "否认" in later["items"][0]["risk_words"]
    assert len(db.rows(select(news_items))) == 4


def test_published_and_observed_cutoffs_both_prevent_lookahead(db, news_settings):
    collect_news(db, news_settings, feed=Feed(), captured=CLOSE)
    assert build_news(db, news_settings, report())["count"] == 0
    assert build_news(db, news_settings, report())["status"] == "not_collected"
    after_close = CLOSE + timedelta(hours=1)
    late_feed = Feed([raw(published="2026-10-12 15:30:00", title="银行发布新政策")])
    collect_news(db, news_settings, force=True, feed=late_feed, captured=after_close)
    at_close = build_news(db, news_settings, report("close", CLOSE))
    assert at_close["count"] == 1
    supplement = build_news(db, news_settings, report("close", CLOSE), as_of=after_close)
    assert supplement["supplemental"] and supplement["after_quote_count"] == 1
    assert next(r for r in supplement["items"] if "银行" in r["title"])["after_quote"]


def test_future_and_malformed_rows_do_not_become_news(db, news_settings):
    bad = [raw(published="2026-10-12 15:30:00"), raw(published="bad")]
    # The fake supplier deliberately returns bad schema for both providers.
    class BadFeed:
        def fetch(self, provider, captured):
            return bad
    result = collect_news(db, news_settings, feed=BadFeed(), captured=NOON)
    assert result["status"] == "partial"
    assert not db.rows(select(news_items))
    assert all(r["invalid_count"] == 2 for r in db.rows(select(news_fetches)))
    assert build_news(db, news_settings, report())["status"] == "unavailable"


def test_provider_failure_retains_archive_and_exposes_partial_status(db, news_settings):
    collect_news(db, news_settings, feed=Feed(), captured=NOON)
    collect_news(db, news_settings, force=True, feed=Feed(fail=("cls",)), captured=CLOSE)
    news = build_news(db, news_settings, report("close", CLOSE))
    assert news["status"] == "partial" and news["count"] == 1
    assert news["sources"][1]["status"] == "unavailable"
    assert "private" not in news["sources"][1]["error"]


def test_news_collection_respects_interval_and_disabled_or_demo_never_fetch(db, news_settings):
    feed = Feed()
    collect_news(db, news_settings, feed=feed, captured=NOON)
    assert collect_news(db, news_settings, feed=feed, captured=NOON + timedelta(minutes=9))["status"] == "cached"
    assert len(feed.calls) == 2
    collect_news(db, news_settings, feed=feed, captured=NOON + timedelta(minutes=10))
    assert len(feed.calls) == 4
    for config in (replace(news_settings, news_enabled=False), replace(news_settings, source="demo")):
        assert collect_news(db, config, feed=feed)["status"] == "disabled"
        assert build_news(db, config, report())["status"] == "disabled"
    assert len(feed.calls) == 4


def test_new_since_midday_uses_all_tracked_ids_and_preserves_frozen_news(db, news_settings):
    collect_news(db, news_settings, feed=Feed(), captured=NOON)
    morning = report()
    morning["news"] = build_news(db, news_settings, morning)
    frozen = deepcopy(morning["news"])
    with db.engine.begin() as connection:
        db.upsert(connection, snapshots, {"source": "baostock", "day": DAY, "slot": "midday", "payload": morning})
    feed = Feed([raw(), raw("浦发银行发布经营数据", "2026-10-12 13:00:00", "浦发银行披露业绩")])
    collect_news(db, news_settings, force=True, feed=feed, captured=CLOSE)
    close = build_news(db, news_settings, report("close", CLOSE))
    assert close["new_count"] == 1
    assert close["count"] == 2
    assert db.rows(select(snapshots))[0]["payload"]["news"] == frozen
    bank = next(item for item in close["items"] if "浦发银行" in item["title"])
    assert bank["new_since_midday"]
    assert bank["related_sectors"][0]["name"] == "J66货币金融服务"


def test_supplemented_morning_is_not_used_as_an_original_noon_baseline(db, news_settings):
    collect_news(db, news_settings, feed=Feed(), captured=CLOSE)
    morning = report()
    morning["news"] = build_news(db, news_settings, morning, as_of=CLOSE)
    with db.engine.begin() as connection:
        db.upsert(connection, snapshots, {"source": "baostock", "day": DAY, "slot": "midday", "payload": morning})
    assert build_news(db, news_settings, report("close", CLOSE))["new_count"] is None


def test_timezones_html_fallback_titles_and_untrusted_links_are_handled():
    row = normalize("em", {**raw(), "showTime": "2026-10-12T03:30:00Z", "title": "", "summary": "<b>【机器人产业政策】</b>内容"}, NOON)
    assert row["published_at"] == "2026-10-12T11:30:00+08:00"
    assert row["title"] == "机器人产业政策"
    assert not safe_url("javascript:alert(1)")
    assert not safe_url("https://cls.cn.evil.example/item")
    assert safe_url("https://www.cls.cn/detail/123")
    assert not topic_matches("RAIL 邮箱 CAI 公司")
    assert "人工智能" in topic_matches("AI智能体")


def test_expired_collection_is_visible_even_when_old_articles_remain(db, news_settings):
    collect_news(db, news_settings, feed=Feed(), captured=NOON)
    news = build_news(db, news_settings, report("close", CLOSE))
    assert news["status"] == "unavailable" and news["count"] == 1
    assert all(r["status"] == "stale" for r in news["sources"])


def test_bounded_http_client_checks_schema_and_status(monkeypatch):
    original_client = httpx.Client
    def transport(request):
        assert request.url.scheme == "https"
        if request.url.host == "www.cls.cn":
            assert request.url.params["sign"]
            return httpx.Response(200, json={"data": {"roll_data": [{"id": 1}]}})
        return httpx.Response(200, json={"data": {"fastNewsList": [{"code": 2}]}})
    monkeypatch.setattr("stock_daily.news.httpx.Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(transport), **kwargs))
    feed = PublicNewsFeed(8)
    assert feed.fetch("cls", NOON) == [{"id": 1}]
    assert feed.fetch("em", NOON) == [{"code": 2}]


def test_refresh_rejects_historical_or_unavailable_snapshot_without_fetch(db, news_settings, monkeypatch):
    monkeypatch.setattr("stock_daily.news.now", lambda: NOON)
    monkeypatch.setattr("stock_daily.news.collect_news", lambda *args, **kwargs: pytest.fail("must not fetch"))
    with pytest.raises(DataError, match="不能用当前新闻"):
        refresh_snapshot_news(db, news_settings, "20261009")
    with pytest.raises(DataError, match="先采集"):
        refresh_snapshot_news(db, news_settings, DAY)
    assert db.rows(select(func.count().label("n")).select_from(news_fetches))[0]["n"] == 0


def test_event_news_precedes_price_commentary_and_search_set_keeps_risk_items(db, news_settings):
    entries = [raw('软件股份涨超10% 板块再度拉升', content='软件股份涨停')]
    entries += [raw(f'软件股份新增产品订单第{i}批', content='软件股份新增业务') for i in range(30)]
    entries += [raw('银行监管部门发布风险警示', content='风险警示待核对')]
    collect_news(db, news_settings, feed=Feed(entries), captured=NOON)
    news = build_news(db, news_settings, report())
    assert news['items'][0]['kind'] != '行情报道'
    assert len(news['items']) == 32
    assert any(item['risk_words'] for item in news['items'])
    assert next(r for r in news['items'] if '涨超' in r['title'])['kind'] == '行情报道'
