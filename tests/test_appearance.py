"""Browser checks for shared appearance, CSP compatibility and phone navigation."""

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_mail_preview import browser as browser
from test_mail_preview import snapshot_payload
from test_pages import fake_png

from stock_daily.html_export import export_html
from stock_daily.intraday import render_snapshot
from stock_daily.pages import export_pages
from stock_daily.realtime import render, view
from stock_daily.web import create_app


@pytest.fixture
def ui_context(browser, populated):
    db, settings, _ = populated
    with TestClient(create_app(settings, db)) as client, browser.new_context(color_scheme="dark") as context:
        def respond(route):
            response = client.get(route.request.url.replace("http://guanshi.test", ""))
            route.fulfill(status=response.status_code, body=response.content, headers=dict(response.headers))

        context.route("http://guanshi.test/**", respond)
        yield context


def theme(page):
    return page.locator("html").get_attribute("data-theme")


def choose(page, label, expected):
    page.get_by_role("button", name=label, exact=True).click()
    page.wait_for_function("expected => document.documentElement.dataset.theme === expected", arg=expected)
    assert page.get_by_role("button", name=label, exact=True).get_attribute("aria-pressed") == "true"


def test_workbench_theme_tracks_system_persists_and_syncs_tabs(ui_context):
    page = ui_context.new_page()
    page.goto("http://guanshi.test/")
    page.wait_for_function("() => window.REPORT_READY === true")
    assert theme(page) == "dark"
    choose(page, "日间模式", "light")
    page.reload()
    assert theme(page) == "light"
    other = ui_context.new_page()
    other.goto("http://guanshi.test/realtime")
    assert theme(other) == "light"
    choose(other, "夜间模式", "dark")
    page.wait_for_function("() => document.documentElement.dataset.theme === 'dark'")
    page.emulate_media(color_scheme="light")
    assert theme(page) == "dark"  # An explicit choice wins over system changes.
    choose(page, "跟随系统", "light")
    page.emulate_media(color_scheme="dark")
    page.wait_for_function("() => document.documentElement.dataset.theme === 'dark'")
    page.evaluate("localStorage.removeItem('guanshi-appearance')")
    other.wait_for_function("() => document.documentElement.dataset.appearance === 'system'")


@pytest.mark.parametrize("kind", ["daily", "snapshot", "realtime", "portal"])
def test_self_contained_reports_support_appearance_under_their_csp(browser, populated, tmp_path, kind):
    db, settings, _ = populated
    config = replace(settings, source="demo", export_png=False)
    path = tmp_path / f"{kind}.html"
    if kind == "daily":
        export_html(db, config, output=path)
    elif kind == "snapshot":
        path.write_text(render_snapshot(snapshot_payload(), config))
    elif kind == "realtime":
        path.write_text(render(view(db, config), config))
    else:
        export_pages(db, config, tmp_path / "public", render_image=fake_png)
        path = tmp_path / "public/index.html"
    with browser.new_context(viewport={"width": 390, "height": 844}) as context:
        page = context.new_page()
        errors, violations = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda msg: violations.append(msg.text) if "Content Security Policy" in msg.text else None)
        page.goto(Path(path).as_uri())
        choose(page, "夜间模式", "dark")
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        assert page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor") == "rgb(14, 23, 35)"
        page.reload()
        assert theme(page) == "dark"
        choose(page, "日间模式", "light")
        assert not errors and not violations


def test_theme_still_works_when_browser_storage_is_unavailable(ui_context):
    ui_context.add_init_script("""for (const name of ['getItem','setItem']) {
        Storage.prototype[name] = () => { throw new DOMException('Blocked', 'SecurityError'); };
    }""")
    page = ui_context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://guanshi.test/")
    choose(page, "日间模式", "light")
    choose(page, "夜间模式", "dark")
    assert not errors


def test_responsive_workbench_navigation_filters_and_dialogs(ui_context):
    page = ui_context.new_page()
    page.goto("http://guanshi.test/")
    page.wait_for_function("() => window.REPORT_READY === true")
    choose(page, "夜间模式", "dark")
    for width in (320, 390, 768, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        for label, target in [("我的自选", "watch"), ("信号跟踪", "tracking"), ("模拟持仓", "backtest"), ("每日复盘", "overview")]:
            button = page.get_by_role("button", name=label, exact=True)
            button.click()
            assert button.get_attribute("aria-current") == "page"
            assert page.locator(f"#view-{target}").is_visible()
            if target == "backtest":
                page.locator("#simulation-account").wait_for(state="visible")
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.get_by_role("button", name="放量突破", exact=True).click()
    assert page.get_by_role("button", name="放量突破", exact=True).get_attribute("aria-pressed") == "true"
    page.locator("#candidate-search").fill("不存在的股票")
    assert "没有符合当前搜索条件的信号" in page.locator("#candidate-table").inner_text()
    page.locator("#candidate-search").fill("")
    page.locator("#method-button").click()
    assert page.locator("#method-dialog").is_visible()
    page.keyboard.press("Escape")
    assert page.locator("#method-dialog").is_hidden()


def test_deployment_refresh_preserves_theme_and_each_application_script(browser, populated, tmp_path):
    import importlib.util
    import json

    from stock_daily.db import snapshots
    from stock_daily.pages import public_snapshot

    db, settings, _ = populated
    payload = public_snapshot(snapshot_payload())
    payload['source'] = settings.source
    with db.engine.begin() as connection:
        db.upsert(connection, snapshots, {'source': settings.source, 'day': payload['day'],
                                          'slot': payload['slot'], 'payload': payload})
    folder = tmp_path / 'deployed'
    export_pages(db, settings, folder, render_image=fake_png)
    spec = importlib.util.spec_from_file_location('refresh_ui', '.github/scripts/refresh_public_ui.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.refresh(folder, 'example/reports', Path('stock_daily').resolve())
    manifest = json.loads((folder / 'manifest.json').read_text())
    paths = [folder / 'index.html', folder / 'realtime.html']
    paths.extend(folder / record['html'] for record in manifest['reports'])
    with browser.new_context(viewport={'width': 390, 'height': 844}) as context:
        for path in paths:
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(path.as_uri())
            choose(page, '夜间模式', 'dark')
            choose(page, '日间模式', 'light')
            if page.locator('#offline-snapshot').count():
                page.wait_for_function('() => window.REPORT_READY === true')
                assert page.evaluate("typeof refreshPublicPortfolio") == 'function'
                assert page.evaluate("typeof renderBacktest") == 'function'
                # The deployment must not replace the early theme script with app.js.
                scripts = page.locator('script[nonce]').all_text_contents()
                assert sum('const offlineElement' in script for script in scripts) == 1
                assert 'guanshi-appearance' in scripts[0]
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            assert not errors
            page.close()
