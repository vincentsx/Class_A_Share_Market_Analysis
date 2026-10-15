from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select
from test_mail_preview import snapshot_payload
from test_realtime import fake_analysis

from stock_daily.db import report_analyses
from stock_daily.realtime_ai import analyze
from stock_daily.report_ai import cached, generate, research_input


@pytest.mark.parametrize('kind', ['midday', 'close', 'daily'])
def test_report_ai_uses_frozen_evidence_and_caches_without_mutating_reports(db, settings, kind):
    payload = snapshot_payload()
    if kind == 'daily':
        payload.update(generated_at=payload['captured_at'], evidence={'items': []})
    original = deepcopy(payload)
    calls = []
    def model(config, report):
        calls.append(report)
        return fake_analysis(config, report)
    result = generate(db, settings, payload, kind, analyzer=model)
    assert result['status'] == 'available' and result['evidence']
    assert generate(db, settings, payload, kind, analyzer=model) == result
    assert cached(db, settings, payload, kind) == result
    assert len(calls) == 1 and payload == original
    assert cached(db, replace(settings, source='tushare'), payload, kind)['status'] == 'not_generated'
    assert len(db.rows(select(report_analyses))) == 1


def test_failed_report_ai_is_visible_and_retry_preserves_quote_cutoff(db, settings):
    payload = snapshot_payload()
    def fail(*args):
        raise ValueError('引用未通过校验')
    result = generate(db, settings, payload, 'close', analyzer=fail)
    assert result['status'] == 'failed' and result['as_of'] == payload['captured_at']
    recovered = generate(db, settings, payload, 'close', retry=True, analyzer=fake_analysis)
    assert recovered['status'] == 'available' and recovered['as_of'] == result['as_of']


def test_short_evidence_ids_restore_original_references_and_validate_stock_opinions(monkeypatch, settings):
    config = replace(settings, ai_api_key='secret-key', ai_model='test-model', ai_api_style='chat_completions', ai_trading_enabled=True)
    report = research_input(snapshot_payload(), 'close')
    report['decision_universe'] = ['600000.SH']
    report['evidence'].append({'id': 'stock-600000.SH', 'title': '模拟观察股', 'text': '报价与信号'})
    seen = []
    def respond(request):
        import json
        request_data = json.loads(request.content)
        seen.append(request_data)
        evidence = json.loads(request_data['messages'][1]['content'])['evidence']
        stock = next(e['id'] for e in evidence if e['title'] == '模拟观察股')
        digest = {k: [{'text': '量价事实需核对', 'evidence_ids': ['E001']}] for k in ('overview','focus','risks','watchpoints')}
        digest['trade_policy'] = {'market_action': 'reduce', 'text': '市场风险升高', 'evidence_ids': ['E001'],
            'stocks': [{'code': '600000.SH', 'action': 'wait', 'score': 70, 'text': '等待报价确认', 'evidence_ids': [stock]}]}
        return httpx.Response(200, json={'choices': [{'finish_reason':'stop','message':{'content':json.dumps(digest)}}]})
    original = httpx.Client
    monkeypatch.setattr('stock_daily.realtime_ai.httpx.Client', lambda **kw:original(transport=httpx.MockTransport(respond)))
    result = analyze(config, report)
    assert result['sections']['overview'][0]['evidence_ids'] == ['market']
    assert result['trade_policy']['stocks'][0]['evidence_ids'] == ['stock-600000.SH']
    assert result['trade_policy']['status'] == 'available'
    assert 'secret-key' not in str(seen)


def test_deployment_injects_only_matching_report_cutoff_from_public_ai_feed(populated, tmp_path):
    import importlib.util
    import json
    from pathlib import Path

    from test_pages import fake_png

    from stock_daily.db import snapshots
    from stock_daily.live_publish import public_portfolio
    from stock_daily.pages import export_pages

    db, settings, _ = populated
    payload = snapshot_payload()
    payload['source'] = settings.source
    with db.engine.begin() as connection:
        db.upsert(connection, snapshots, {'source': settings.source, 'day': payload['day'],
            'slot': payload['slot'], 'payload': payload})
    folder = tmp_path / 'public'
    export_pages(db, settings, folder, render_image=fake_png)
    def model(config, data):
        result = fake_analysis(config, data)
        result['sections']['overview'][0]['text'] = '按原截止证据生成的研判'
        return result
    generate(db, settings, payload, 'close', analyzer=model)
    report = db.latest_report(settings.source)
    generate(db, settings, report, 'daily', analyzer=model)
    live = public_portfolio(db, settings)
    spec = importlib.util.spec_from_file_location('refresh_ui', '.github/scripts/refresh_public_ui.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.refresh(folder, 'example/reports', Path('stock_daily').resolve(), live)
    manifest = json.loads((folder / 'manifest.json').read_text())
    for record in manifest['reports']:
        assert '按原截止证据生成的研判' in (folder / record['html']).read_text()
    live['report_analyses']['close']['analysis']['as_of'] = '2026-10-15T15:00:00+08:00'
    live['report_analyses']['close']['analysis']['sections']['overview'][0]['text'] = '未来证据不能写入旧报告'
    module.refresh(folder, 'example/reports', Path('stock_daily').resolve(), live)
    for record in manifest['reports']:
        assert '未来证据不能写入旧报告' not in (folder / record['html']).read_text()
