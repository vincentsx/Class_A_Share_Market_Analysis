from dataclasses import replace
from pathlib import Path

import pytest

from stock_daily.html_export import export_html
from stock_daily.intraday import export_snapshot


@pytest.fixture(scope="module")
def browser():
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Chromium未安装；禁用脚本的附件预览检查需要本地浏览器")
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


def snapshot_payload():
    stamp = "2026-10-12T15:05:00+08:00"
    row = {"code": "600000.SH", "name": "示例观察公司", "industry": "示例行业", "price": 10.5,
           "pct": 5, "amount": 80_000_000, "volume": 10_000_000, "quote_time": stamp,
           "volume_ratio": 1.8, "volume_baseline": "此前20日全天均量", "ma20": 10,
           "distance_ma20": 5, "prior_high20": 10.8, "distance_high20": -2.78,
           "tags": ["放量上涨"], "risks": ["观察风险说明"], "afternoon": None, "reason": "自选股"}
    sector = {"name": "示例行业", "pct": 2.5, "count": 5, "up_ratio": 80, "amount_share": 10,
              "active": True, "status": "活跃", "afternoon": None, "leaders": [],
              "volume_count": 5, "volume_up_count": 1, "volume_down_count": 0}
    title = "人工智能政策发布</script><script>window.UNSAFE=1</script>"
    item = {"title": title, "published_at": stamp, "kind": "政策/监管报道", "excerpt": "政策新闻摘要",
            "after_quote": False, "new_since_midday": None, "topics": ["人工智能"],
            "sources": [{"name": "财联社电报", "url": "https://www.cls.cn/detail/123"}],
            "mentioned_stocks": [{"name": row["name"], "code": row["code"], "pct": 5, "volume_ratio": 1.8}],
            "related_sectors": [], "risk_words": []}
    news = {"status": "available", "as_of": stamp, "quote_cutoff": stamp, "supplemental": False,
            "count": 1, "new_count": None, "notes": [], "items": [item],
            "sources": [{"name": "财联社电报", "status": "available", "fetched_at": stamp,
                         "count": 1, "invalid_count": 0, "error": ""}],
            "topics": [{"name": "人工智能", "count": 1, "latest_at": stamp, "new_count": None,
                        "related_sectors": [], "mentioned_stocks": [], "after_quote_count": 0,
                        "headline": title, "url": "javascript:alert(1)"}]}
    return {"day": "20261012", "slot": "close", "source": "baostock", "label": "收盘快照",
            "scheduled_time": "15:05", "quote_provider": "测试报价", "quote_start": stamp, "quote_end": stamp,
            "captured_at": stamp, "metadata_at": stamp, "expected": 1, "coverage": 100, "missing": {},
            "reference_day": "20261009", "quotes": [row], "focus": [row], "industries": [sector], "news": news,
            "market": {"count": 1, "up": 1, "down": 0, "flat": 0, "amount": row["amount"],
                       "benchmark": {"pct": 1.25, "price": 4000, "quote_time": stamp}},
            "analysis": {"version": "intraday-v2", "volume_coverage": 1, "notes": [],
                         "summaries": ["量价观察摘要已保存"], "hot_sectors": [sector],
                         "counts": {"volume_up": 1, "volume_down": 0, "risk": 0},
                         "volume_up": [row], "volume_down": [], "risk_alerts": []}}


def test_phone_attachment_shows_data_without_scripts_and_browser_retains_filters(browser, settings, tmp_path):
    config = replace(settings, source="baostock", html_report_dir=str(tmp_path), export_png=False)
    result = export_snapshot(snapshot_payload(), config)
    with browser.new_context(java_script_enabled=False, viewport={"width": 390, "height": 844}) as context:
        page = context.new_page()
        requests = []
        page.on("request", lambda r: requests.append(r.url) if r.url.startswith(("http:", "https:")) else None)
        page.goto(Path(result["html"]).as_uri())
        assert page.locator("#static-report").is_visible()
        assert page.locator("#interactive-report").is_hidden()
        text = page.locator("#static-report").inner_text()
        for expected in ("量价观察摘要已保存", "+1.25%", "人工智能", "政策新闻摘要", "示例行业",
                         "放量上涨", "1.80", "示例观察公司", "600000.SH"):
            assert expected in text
        assert page.locator("#static-report script").count() == 0
        assert page.locator('#static-report a[href^="javascript:"]').count() == 0
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        assert not requests
    with browser.new_context(viewport={"width": 390, "height": 844}) as context:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(Path(result["html"]).as_uri())
        page.wait_for_function("window.REPORT_READY === true")
        assert page.locator("#static-report").is_hidden()
        assert page.locator("#interactive-report").is_visible()
        page.locator("#news-search").fill("人工智能")
        assert page.locator("#news-items .news-entry").count() == 1
        page.get_by_role("button", name="缩量观察", exact=False).click()
        assert "未发现可确认" in page.locator("#signals").inner_text()
        assert page.evaluate("typeof window.UNSAFE") == "undefined"
        assert not errors


def test_daily_attachment_also_shows_market_candidates_and_watchlist_without_scripts(browser, populated, tmp_path):
    db, settings, _ = populated
    path = tmp_path / "daily.html"
    export_html(db, settings, output=path)
    candidate = db.latest_report(settings.source)["candidates"][0]
    with browser.new_context(java_script_enabled=False, viewport={"width": 390, "height": 844}) as context:
        page = context.new_page()
        page.goto(path.as_uri())
        assert page.locator("#static-report").is_visible()
        text = page.locator("#static-report").inner_text()
        for expected in ("市场温度", "候选观察列表", candidate["code"], candidate["name"],
                         candidate["exit_condition"], "我的自选", "信号跟踪", "合成示例数据"):
            assert expected in text
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    with browser.new_context() as context:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(path.as_uri())
        page.wait_for_function("window.REPORT_READY === true")
        assert page.locator("#static-report").is_hidden()
        assert page.locator("#report-content").is_visible()
        assert not errors


def test_png_export_shows_all_signal_groups_and_first_forty_quotes(browser, settings, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    import playwright.sync_api

    from stock_daily.png_export import export_png

    payload = snapshot_payload()
    original_row = payload['quotes'][0]
    rows = [{**original_row, 'code': f'{600000+i:06d}.SH', 'name': f'报价样本{i:02d}'} for i in range(43)]
    up = {**rows[0], 'name':'放量观察样本', 'tags':['放量上涨']}
    down = {**rows[1], 'name':'缩量观察样本', 'tags':['缩量回调'], 'volume_ratio':0.5, 'pct':-1}
    risk = {**rows[2], 'name':'风险异动样本', 'tags':['放量下跌', '冲高回落'], 'volume_ratio':2, 'pct':-6}
    rows[:3] = [up, down, risk]
    payload['quotes'] = rows
    payload['focus'] = []
    payload['expected'] = 43
    payload['market'].update(count=43, up=41, down=2, flat=0)
    payload['analysis'].update(volume_up=[up], volume_down=[down], risk_alerts=[risk],
                               counts={'volume_up':1, 'volume_down':1, 'risk':1})
    config = replace(settings, source='baostock', html_report_dir=str(tmp_path), export_png=False)
    result = export_snapshot(payload, config)
    original_screenshot = playwright.sync_api.Page.screenshot
    captured = []

    def check_export_view(page, *args, **kwargs):
        assert page.locator('#signal-view').is_hidden()
        assert page.locator('#export-signals').is_visible()
        for key, name in [('volume_up','放量观察样本'), ('volume_down','缩量观察样本'), ('risk_alerts','风险异动样本')]:
            section = page.locator(f'.export-signal-section[data-kind="{key}"]')
            assert section.is_visible()
            assert name in section.inner_text()
        assert page.locator('#quote-section').evaluate('(element) => element.open')
        assert page.locator('#quotes tr').count() == 40
        assert page.locator('#quotes tr').last.is_visible()
        assert '共43只有效报价' in page.locator('#quote-summary').inner_text()
        assert '报价样本42' not in page.locator('#quotes').inner_text()
        assert page.locator('#search').is_hidden()
        captured.append(True)
        return original_screenshot(page, *args, **kwargs)

    monkeypatch.setattr(playwright.sync_api.Page, 'screenshot', check_export_view)
    # The shared browser fixture already owns a Playwright event loop. Run
    # the production renderer in its own thread, with its own real runtime.
    with ThreadPoolExecutor(max_workers=1) as pool:
        png = pool.submit(export_png, result['html']).result()
    assert captured == [True]
    assert Path(png['path']).read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    # Preparing a bitmap must not alter the default interactive HTML on disk.
    with browser.new_context() as context:
        page = context.new_page()
        page.goto(Path(result['html']).as_uri())
        page.wait_for_function('window.REPORT_READY === true')
        assert page.locator('#signal-view').is_visible()
        assert page.locator('#export-signals').is_hidden()
        assert not page.locator('#quote-section').evaluate('(element) => element.open')
