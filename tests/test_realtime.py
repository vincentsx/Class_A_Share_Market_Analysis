import base64
import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_mail_preview import browser as browser
from test_paper_trading import CURRENT, quotes, setup

from stock_daily.db import (
    bars,
    paper_accounts,
    realtime_reports,
    reports,
    signals,
    trade_alerts,
    trading_days,
)
from stock_daily.live_publish import publish_realtime
from stock_daily.operations import state
from stock_daily.providers import DataError
from stock_daily.realtime import due, query_reports, render, run, view
from stock_daily.realtime_ai import SECTIONS, analyze
from stock_daily.web import create_app


@pytest.fixture
def config(settings, db):
    s = replace(settings, source='baostock', news_enabled=True, ai_api_key='test-secret', ai_model='test-model',
                ai_api_base_url='https://ai.example/v1', ai_api_style='chat_completions')
    setup(db, source=s.source, observed=False)
    return s


class Feed:
    def __init__(self, at=CURRENT, missing=(), price=10.3):
        self.at, self.missing, self.price, self.calls = at, missing, price, 0

    def fetch(self, codes, day, slot):
        assert set(codes) == {'600000.SH', '600001.SH'}
        assert slot == 'portfolio'
        self.calls += 1
        payload = quotes(self.at)['quotes']
        for code in self.missing:
            payload.pop(code)
        for code, q in payload.items():
            if code != '000300.SH':
                q.update(price=self.price, high=max(q['high'], self.price))
        return payload, {code: 'missing' for code in self.missing}


class News:
    def fetch(self, provider, captured):
        if provider == 'em':
            return [{'title': '600001人工智能政策发布', 'summary': '公司公告需进一步核对',
                'showTime': (captured - timedelta(minutes=1)).isoformat(), 'code': '123456'}]
        return [{'title': '600001人工智能政策发布', 'content': '公司公告需进一步核对', 'id': 123456,
            'ctime': (captured - timedelta(minutes=1)).timestamp(), 'level': 'B'}]


def fake_analysis(settings, report):
    assert any(e['id'] == 'market' for e in report['evidence'])
    return {'status': 'available', 'sections': {key: [{'text': '依据本次快照，后续变化需验证。',
        'evidence_ids': ['market']}] for key in SECTIONS}, 'as_of': report['captured_at'],
        'generated_at': report['captured_at'], 'model': 'test-model', 'message': 'AI研判需验证'}


def capture(db, config, at=CURRENT, feed=None, analyzer=fake_analysis):
    return run(db, config, feed=feed or Feed(at), news_feed=News(), analyzer=analyzer, current=at)


def test_real_time_scope_news_ai_and_restart_dedup_never_execute_trades(db, config):
    before = {t.name: db.rows(select(t)) for t in (bars, reports, signals, paper_accounts, trade_alerts)}
    feed = Feed()
    assert capture(db, config, feed=feed)['ai_status'] == 'available'
    report = view(db, config, current=CURRENT)['report']
    assert report['market']['count'] == 2 and report['coverage'] == 100
    assert report['news']['count'] == 1 and len(report['news']['items'][0]['sources']) == 2
    assert report['sentiment']['sample_count'] == 1
    assert report['basis_day'] == '20261014' and report['market']['ma20_count'] == 0
    assert capture(db, config, feed=feed)['status'] == 'unchanged' and feed.calls == 1
    assert not due(db, config, CURRENT)
    assert due(db, config, CURRENT + timedelta(minutes=10))
    assert before == {t.name: db.rows(select(t)) for t in (bars, reports, signals, paper_accounts, trade_alerts)}
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize('hour,minute', [(9, 29), (11, 35), (12, 59), (15, 20), (22, 0)])
def test_market_hours_gate_collection(db, config, hour, minute):
    at = CURRENT.replace(hour=hour, minute=minute)
    assert not due(db, config, at)
    assert capture(db, config, at)['status'] == 'skipped'


def test_calendar_unknown_or_closed_does_not_assume_market_open(db, config):
    with db.engine.begin() as c:
        c.execute(trading_days.delete().where(trading_days.c.day == '20261015'))
    assert not due(db, config, CURRENT)
    assert capture(db, config)['status'] == 'skipped'


def test_data_saved_before_ai_and_ai_failure_cannot_discard_market_or_reuse_old_ai(db, config):
    capture(db, config)
    old = deepcopy(db.rows(query_reports(config).limit(1))[0])
    later = CURRENT + timedelta(minutes=10)
    observed = []
    def fail(settings, report):
        saved = view(db, settings, current=later)['report']
        observed.append(saved['ai']['status'])
        raise ValueError('AI服务暂不可用')
    assert capture(db, config, later, Feed(later, price=10.4), fail)['ai_status'] == 'failed'
    latest = view(db, config, current=later)['report']
    assert latest['ai']['status'] == 'failed' and 'sections' not in latest['ai']
    assert latest['leaders'][0]['price'] == 10.4
    assert observed == ['pending']
    assert db.rows(query_reports(config, window=old['window']))[0] == old


def test_partial_coverage_reported_and_missing_benchmark_preserves_previous_analysis(db, config):
    capture(db, config, feed=Feed(missing=('600000.SH',)))
    assert view(db, config, current=CURRENT)['status'] == 'partial'
    old = db.rows(query_reports(config))
    later = CURRENT + timedelta(minutes=10)
    with pytest.raises(DataError, match='沪深300'):
        capture(db, config, later, Feed(later, missing=('000300.SH',)))
    assert db.rows(query_reports(config)) == old
    assert state(db, config, 'realtime_refresh')['status'] == 'failed'
    assert view(db, config, current=later)['refresh']['status'] == 'failed'


def test_stale_future_or_bad_quotes_are_excluded(db, config):
    class Invalid(Feed):
        def fetch(self, *args):
            rows, missing = super().fetch(*args)
            rows['600000.SH']['quote_time'] = (CURRENT + timedelta(minutes=1)).isoformat()
            rows['600001.SH']['amount'] = float('nan')
            return rows, missing
    with pytest.raises(DataError):
        capture(db, config, feed=Invalid())
    assert not db.rows(select(realtime_reports))


def test_news_observed_after_snapshot_never_enters_history(db, config):
    capture(db, config)
    old = deepcopy(view(db, config, current=CURRENT)['report'])
    later = CURRENT + timedelta(minutes=10)
    capture(db, config, later)
    assert view(db, config, window=old['window'], current=later)['report'] == old
    other = replace(config, source='tushare')
    assert view(db, other, current=later)['report'] is None
    assert view(db, other, current=later)['history'] == []
    assert view(db, config, current=later + timedelta(minutes=16))['status'] == 'stale'


def mock_client(monkeypatch, response):
    original = httpx.Client
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return response
    monkeypatch.setattr('stock_daily.realtime_ai.httpx.Client',
        lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    return seen


@pytest.mark.parametrize('problem', ['unknown_reference', 'empty_reference', 'extra_key', 'too_many', 'truncated', 'invalid_json', 'http_error'])
def test_ai_rejects_untraceable_incomplete_and_failed_outputs(db, config, monkeypatch, problem):
    capture(db, config)
    report = view(db, config, current=CURRENT)['report']
    digest = fake_analysis(config, report)['sections']
    if problem == 'unknown_reference':
        digest['overview'][0]['evidence_ids'] = ['invented-news']
    elif problem == 'empty_reference':
        digest['overview'][0]['evidence_ids'] = []
    elif problem == 'extra_key':
        digest['extra'] = []
    elif problem == 'too_many':
        digest['overview'] *= 5
    choice = {'finish_reason': 'length' if problem == 'truncated' else 'stop',
        'message': {'content': 'broken' if problem == 'invalid_json' else json.dumps(digest)}}
    mock_client(monkeypatch, httpx.Response(401 if problem == 'http_error' else 200, json={'choices': [choice]}))
    with pytest.raises(ValueError) as error:
        analyze(config, report)
    assert config.ai_api_key not in str(error.value)


@pytest.mark.parametrize('style', ['chat_completions', 'responses'])
def test_ai_validates_both_api_styles_and_does_not_send_settings(db, config, monkeypatch, style):
    capture(db, config)
    report = view(db, config, current=CURRENT)['report']
    config = replace(config, ai_api_style=style)
    digest = fake_analysis(config, report)['sections']
    response = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(digest)}}]} if style == 'chat_completions' else {
        'status': 'completed', 'output': [{'content': [{'type': 'output_text', 'text': json.dumps(digest)}]}]}
    seen = mock_client(monkeypatch, httpx.Response(200, json=response))
    assert analyze(config, report)['status'] == 'available'
    assert config.ai_api_key not in json.dumps(seen)
    assert 'watched' not in json.dumps(seen)


def test_api_read_is_source_scoped_and_cannot_trigger_collection_or_ai(db, config, monkeypatch):
    capture(db, config)
    def forbidden(*args, **kwargs):
        pytest.fail('GET不能发起采集或AI调用')
    monkeypatch.setattr('stock_daily.realtime.run', forbidden)
    monkeypatch.setattr('stock_daily.realtime_ai.analyze', forbidden)
    with TestClient(create_app(config, db)) as client:
        r = client.get('/api/realtime')
        assert r.status_code == 200 and r.json()['report']['day'] == '20261015'
        assert config.ai_api_key not in r.text and config.ai_api_base_url not in r.text
        assert client.get('/api/realtime?window=20261015-1000').json()['report'] is not None
        assert client.get('/api/realtime?window=unsafe').status_code == 422
        page = client.get('/realtime')
        assert '观市 · 盘中实时分析' in page.text
        assert "script-src 'nonce-" in page.headers['content-security-policy']
        assert config.ai_api_key not in page.text


def test_public_realtime_feed_excludes_secrets_and_preserves_recent_archives(db, config, monkeypatch):
    capture(db, config)
    capture(db, config, CURRENT + timedelta(minutes=10))
    config = replace(config, pages_enabled=True, pages_repository='example/reports')
    seen = []
    def publish(db, settings, payload, filename, key):
        seen.append(payload)
        assert filename == 'realtime.json' and key == 'realtime_publish'
        return {'status': 'success'}
    monkeypatch.setattr('stock_daily.live_publish.publish_feed', publish)
    assert publish_realtime(db, config)['status'] == 'success'
    payload = seen[0]
    assert len(payload['archives']) == 1 and len(payload['history']) == 2
    text = json.dumps(payload, allow_nan=False)
    assert config.ai_api_key not in text and config.ai_api_base_url not in text
    assert 'watched' not in text and 'mail_to' not in text


def test_phone_realtime_ai_news_filters_history_and_refresh_without_reload(browser, db, config):
    capture(db, config)
    first = view(db, config, current=CURRENT)
    first['archives'] = [deepcopy(first['report'])]
    later = CURRENT + timedelta(minutes=10)
    capture(db, config, later)
    latest = view(db, config, current=later)
    latest['archives'] = first['archives']
    config = replace(config, pages_repository='example/reports')
    html = render(first, config, public=True)
    with browser.new_context(viewport={'width': 390, 'height': 844}) as context:
        page = context.new_page()
        page.clock.install(time=CURRENT)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('https://reports.example/**', lambda route: route.fulfill(body=html, content_type='text/html'))
        page.route('https://raw.githubusercontent.com/**', lambda route: route.fulfill(json=latest))
        page.goto('https://reports.example/realtime.html')
        page.wait_for_function('() => window.REALTIME_READY === true')
        page.wait_for_function('() => document.getElementById("history").options.length === 3')
        assert '市场研判' in page.locator('#content').inner_text()
        assert '人工智能政策' in page.locator('#news-list').inner_text()
        page.locator('#news-search').fill('找不到的主题')
        assert '暂无匹配' in page.locator('#news-list').inner_text()
        page.locator('#news-search').fill('')
        page.locator('#history').select_option(first['report']['window'])
        assert '历史分析' in page.locator('#sync-status').inner_text()
        page.locator('#refresh').click()
        page.wait_for_function('() => document.getElementById("history").value === ""')
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        reference = page.locator('.references').first
        target = reference.get_attribute('href')
        assert '本次行情' in reference.inner_text()
        reference.click()
        assert page.locator(target).is_visible()
        assert '沪深300' in page.locator(target).inner_text()
        assert not errors
        page.unroute('https://raw.githubusercontent.com/**')
        page.route('https://raw.githubusercontent.com/**', lambda route: route.fulfill(json=first))
        calls = []
        def fallback(route):
            calls.append(route.request.url)
            route.fulfill(json={'content': base64.b64encode(json.dumps(latest).encode()).decode()})
        page.route('https://api.github.com/**', fallback)
        page.locator('#refresh').click()
        page.wait_for_function('() => !document.getElementById("refresh").disabled')
        assert len(calls) == 1
        assert page.evaluate('data.report.window') == latest['report']['window']


def test_transient_ai_errors_retry_same_evidence_then_succeed(db, config, monkeypatch):
    capture(db, config)
    report = view(db, config, current=CURRENT)['report']
    digest = fake_analysis(config, report)['sections']
    original = httpx.Client
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) < 3:
            return httpx.Response(502 if len(calls) == 1 else 429, json={})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(digest)}}]})
    monkeypatch.setattr('stock_daily.realtime_ai.httpx.Client',
        lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr('stock_daily.realtime_ai.time.sleep', lambda _: None)
    assert analyze(config, report)['status'] == 'available'
    assert len(calls) == 3 and calls[0] == calls[1] == calls[2]


def test_operator_retry_uses_failed_snapshot_without_refetch_or_changing_capture_time(db, config):
    def fail(*args):
        raise ValueError('service unavailable')
    capture(db, config, analyzer=fail)
    old = deepcopy(view(db, config, current=CURRENT)['report'])
    class Forbidden:
        def fetch(self, *args):
            pytest.fail('重试AI不得采集新行情或新闻')
    run(db, config, feed=Forbidden(), news_feed=Forbidden(), analyzer=fake_analysis, current=CURRENT, retry_ai=True)
    saved = view(db, config, current=CURRENT)['report']
    assert saved['ai']['status'] == 'available'
    assert saved['captured_at'] == old['captured_at'] and saved['news'] == old['news']
    assert saved['evidence'] == old['evidence']


def test_after_hours_ai_retry_uses_frozen_snapshot_without_trading_or_new_collection(db, config):
    from stock_daily.realtime import retry_saved

    def fail(*args):
        raise ValueError('引用失败')
    capture(db, config, analyzer=fail)
    old = deepcopy(view(db, config, current=CURRENT)['report'])
    before = {table.name: db.rows(select(table)) for table in (paper_accounts, trade_alerts, signals)}
    result = retry_saved(db, config, old['window'], analyzer=fake_analysis)
    updated = view(db, config, current=CURRENT)['report']
    assert result['ai_status'] == 'available'
    for key in ('captured_at', 'quote_start', 'quote_end', 'news', 'evidence'):
        assert updated[key] == old[key]
    assert before == {table.name: db.rows(select(table)) for table in (paper_accounts, trade_alerts, signals)}


def test_model_switch_reanalyzes_saved_evidence_once_without_changing_trades(db, config):
    from stock_daily.realtime import retry_saved

    capture(db, config)
    old = deepcopy(view(db, config, current=CURRENT)['report'])
    before = {table.name: db.rows(select(table)) for table in (paper_accounts, trade_alerts, signals)}
    switched = replace(config, ai_model='deepseek-flash')
    calls = []
    def model(settings, report):
        calls.append(deepcopy(report))
        return {**fake_analysis(settings, report), 'model': settings.ai_model}
    assert retry_saved(db, switched, old['window'], analyzer=model)['ai_status'] == 'available'
    assert retry_saved(db, switched, old['window'], analyzer=model)['status'] == 'unchanged'
    updated = view(db, switched, current=CURRENT)['report']
    assert len(calls) == 1 and updated['ai']['model'] == 'deepseek-flash'
    assert {k: v for k, v in updated.items() if k != 'ai'} == {k: v for k, v in old.items() if k != 'ai'}
    assert before == {table.name: db.rows(select(table)) for table in (paper_accounts, trade_alerts, signals)}
