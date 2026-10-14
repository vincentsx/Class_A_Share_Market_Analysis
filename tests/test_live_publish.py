import base64
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_mail_preview import browser as browser

from stock_daily.live_publish import live_url, public_portfolio, publish_portfolio
from stock_daily.operations import state
from stock_daily.pages import export_pages
from stock_daily.providers import DataError


def fake_png(path):
    path.with_suffix('.png').write_bytes(b'png')


def test_lightweight_feed_has_no_private_data_and_deduplicates_commits(populated, monkeypatch):
    import stock_daily.live_publish as publishing

    db, settings, _ = populated
    settings = replace(settings, pages_enabled=True, pages_repository='example/reports',
                       api_key='private-api-key', smtp_password='private-password')
    db.save_watch('600000.SH', 'private-note')
    calls, uploaded = [], []
    def gh(*args, **kwargs):
        calls.append(args)
        if '--input' in args:
            request = json.loads(Path(args[-1]).read_text())
            uploaded.append(json.loads(base64.b64decode(request['content'])))
            assert request['branch'] == 'pages-live' and request['sha'] == 'existing-sha'
        if args[0] == 'api' and '/contents/' in args[1]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({'sha': 'existing-sha'}))
        return SimpleNamespace(returncode=0, stdout='{}')
    monkeypatch.setattr(publishing, 'gh', gh)
    result = publish_portfolio(db, settings)
    assert result['status'] == 'success' and result['bytes'] < 100000
    assert uploaded[0]['version'] == 'portfolio-live-v1'
    for private in ['private-api-key', 'private-password', 'private-note', 'watchlist', 'ai_api_key']:
        assert private not in json.dumps(uploaded)
    assert publish_portfolio(db, settings)['status'] == 'unchanged'
    assert len(uploaded) == 1 and not any('upload' in args or 'workflow' in args for args in calls)


def test_failed_lightweight_upload_keeps_previous_feed_and_can_retry(populated, monkeypatch):
    import stock_daily.live_publish as publishing

    db, settings, _ = populated
    settings = replace(settings, pages_enabled=True, pages_repository='example/reports')
    def gh(*args, **kwargs):
        if '--input' in args:
            raise DataError('网络不可用')
        return SimpleNamespace(returncode=0, stdout=json.dumps({'sha':'existing-sha'}))
    monkeypatch.setattr(publishing, 'gh', gh)
    with pytest.raises(DataError):
        publish_portfolio(db, settings)
    assert state(db, settings, 'portfolio_publish')['status'] == 'failed'
    assert 'fingerprint' not in state(db, settings, 'portfolio_publish')
    assert publish_portfolio(db, replace(settings, pages_enabled=False))['status'] == 'disabled'
    assert live_url(replace(settings, pages_repository='invalid')) == ''


def test_live_quotes_do_not_trigger_large_site_rebuilds(populated, tmp_path, monkeypatch):
    db, settings, _ = populated
    settings = replace(settings, pages_repository='example/reports')
    captured = {'at':'2026-10-14T11:10:00+08:00'}
    def valuation(*args):
        return {'status':'unavailable', 'day':'20261014', 'captured_at':captured['at'],
                'interval_minutes':10, 'message':'缺少报价'}
    monkeypatch.setattr('stock_daily.portfolio.value_portfolio', valuation)
    folder = tmp_path / 'public'
    first = export_pages(db, settings, folder, render_image=fake_png)
    captured['at'] = '2026-10-14T11:20:00+08:00'
    def no_render(*args):
        pytest.fail('盘中轻量更新不应重建图片或日线页面')
    second = export_pages(db, settings, folder, render_image=no_render)
    assert first['signature'] == second['signature']
    assert 'raw.githubusercontent.com' in (folder / 'index.html').read_text()
    report = (folder / 'reports' / 'demo-20261009-portfolio.html').read_text()
    assert 'connect-src https://raw.githubusercontent.com' in report
    assert '"live_url":"https://raw.githubusercontent.com/example/reports/pages-live/portfolio.json"' in report


def test_deployment_refreshes_old_public_interface_without_private_inputs(populated, tmp_path):
    db, settings, _ = populated
    folder = tmp_path / 'public'
    export_pages(db, settings, folder, render_image=fake_png)
    spec = importlib.util.spec_from_file_location('refresh_ui', '.github/scripts/refresh_public_ui.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.refresh(folder, 'example/reports', Path('stock_daily').resolve())
    manifest = json.loads((folder / 'manifest.json').read_text())
    portfolio = next(r for r in manifest['reports'] if r['slot'] == 'portfolio')
    html = (folder / portfolio['html']).read_text()
    assert 'connect-src https://raw.githubusercontent.com' in html
    assert 'refreshPublicPortfolio' in html
    assert 'https://raw.githubusercontent.com/example/reports/pages-live/portfolio.json' in html
    import hashlib
    for name in [portfolio['html'], 'index.html']:
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == manifest['files'][name]
    assert public_portfolio(db, settings)['source'] == settings.source


def test_phone_reads_new_live_values_without_reloading_report(browser, populated, tmp_path):
    from datetime import datetime

    from stock_daily.html_export import daily_payload, render_daily

    db, settings, _ = populated
    settings = replace(settings, pages_repository='example/reports')
    payload = daily_payload(db, settings, public=True, portfolio=True)
    html = render_daily(payload, settings)
    live = public_portfolio(db, settings)
    live.update(data_at='2026-10-14T11:20:00+08:00', published_at='2026-10-14T11:20:03+08:00')
    live['backtest']['valuation'] = {'status':'available', 'interval_minutes':10, 'day':'20261014',
        'captured_at':live['data_at'], 'quote_start':live['data_at'], 'quote_end':live['data_at'],
        'basis_day':'20261009', 'message':'已确认持仓的盘中估值', 'positions':[],
        'summary':{**live['backtest']['summary'], 'initial_capital':100000, 'market_value':0,
                   'available_cash':101000, 'equity':101000, 'profit':1000, 'return':1, 'through':'20261014'}}
    with browser.new_context(viewport={'width':390,'height':844}) as context:
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.clock.install(time=datetime.fromisoformat('2026-10-14T11:25:00+08:00'))
        page.route('https://reports.example/**', lambda route: route.fulfill(content_type='text/html',body=html))
        page.route('https://raw.githubusercontent.com/**', lambda route: route.fulfill(json=live))
        page.goto('https://reports.example/reports/portfolio.html')
        page.wait_for_function('document.querySelector("[data-account-field=profit] .metric-value")?.textContent.includes("1000.00")')
        assert '公开同步' in page.locator('#message').inner_text()
        live['backtest']['valuation']['summary']['profit'] = 2000
        live['backtest']['valuation']['summary']['equity'] = 102000
        live['backtest']['valuation']['summary']['available_cash'] = 102000
        live['data_at'] = '2026-10-14T11:21:00+08:00'
        page.evaluate('refreshPublicPortfolio()')
        assert '+2000.00' in page.locator('[data-account-field=profit] .metric-value').inner_text()
        assert page.url == 'https://reports.example/reports/portfolio.html'
        page.unroute('https://raw.githubusercontent.com/**')
        page.route('https://raw.githubusercontent.com/**', lambda route: route.fulfill(status=503))
        page.evaluate('refreshPublicPortfolio()')
        assert '暂不可用' in page.locator('#message').inner_text()
        assert '+2000.00' in page.locator('[data-account-field=profit] .metric-value').inner_text()
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert not errors
