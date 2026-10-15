from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select
from test_mail_preview import browser as browser
from test_mail_preview import snapshot_payload
from test_realtime import fake_analysis

from stock_daily.db import report_analyses
from stock_daily.realtime_ai import analyze
from stock_daily.report_ai import cached, generate, research_input


@pytest.mark.parametrize('javascript', [False, True])
@pytest.mark.parametrize('kind', ['snapshot', 'daily'])
def test_report_citations_open_readable_sources_without_internal_ids_or_json(browser, populated, tmp_path, kind, javascript):
    from stock_daily.html_export import daily_payload, render_daily
    from stock_daily.intraday import render_snapshot

    db, settings, _ = populated
    payload = snapshot_payload()
    evidence = research_input(payload, 'close')['evidence']
    original = deepcopy(evidence)
    analysis = fake_analysis(settings, {'captured_at': payload['captured_at'], 'evidence': evidence})
    analysis['evidence'] = evidence
    analysis['sections']['focus'] = [{'text': '核对相关报道与行业样本。',
        'evidence_ids': [evidence[1]['id'], evidence[2]['id']]}]
    if kind == 'snapshot':
        payload['ai_analysis'] = analysis
        html = render_snapshot(payload, settings)
    else:
        data = daily_payload(db, settings)
        data['dashboard']['report_analysis'] = analysis
        html = render_daily(data, settings)
    path = tmp_path / (kind + '.html')
    path.write_text(html)
    with browser.new_context(viewport={'width': 390, 'height': 844}, java_script_enabled=javascript) as context:
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(path.as_uri())
        if javascript:
            page.wait_for_function('() => window.REPORT_READY === true')
        panel = page.locator('.ai-report-panel:visible').first
        link = panel.locator('.evidence-ref').first
        assert '行情与数据范围' in link.inner_text()
        target = link.get_attribute('href')
        if javascript:
            link.click()
            assert page.locator(target).is_visible()
            page.wait_for_function("id => { const r=document.getElementById(id).getBoundingClientRect(); return r.top>=0 && r.top<innerHeight; }", arg=target[1:])
        else:
            panel.locator('.analysis-sources > summary').click()
        text = panel.inner_text()
        assert '4,000.00 点' in text and '0.80 亿元' in text
        assert '行业观察' in text and '示例行业' in text
        assert 'news-' not in text and '"benchmark"' not in text and '"industries"' not in text
        assert not panel.locator('a[href^="javascript:"]').count()
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert not page.evaluate('Boolean(window.UNSAFE)')
        assert not errors
    assert evidence == original


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


@pytest.mark.parametrize('stream_finish', ['json', 'repair', 'stop', 'length', None])
def test_short_evidence_ids_restore_original_references_and_validate_stock_opinions(monkeypatch, settings, stream_finish):
    config = replace(settings, ai_api_key='secret-key', ai_model='deepseek-flash', ai_api_style='chat_completions', ai_trading_enabled=True)
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
        if stream_finish == 'repair' and len(seen) == 1:
            digest['overview'][0]['evidence_ids'] = ['invented-reference']
        if stream_finish in ('json', 'repair'):
            return httpx.Response(200, json={'choices': [{'finish_reason':'stop','message':{'content':json.dumps(digest)}}]})
        chunks = [': heartbeat', 'data: ' + json.dumps({'choices': [{'delta': {'reasoning_content': '不应展示的思考'}}]})]
        content = json.dumps(digest)
        for offset in range(0, len(content), 37):
            chunks.append('data: ' + json.dumps({'choices': [{'delta': {'content': content[offset:offset+37]}}]}))
        if stream_finish:
            chunks.append('data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': stream_finish}]}))
        chunks.append('data: [DONE]')
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content='\n\n'.join(chunks))
    original = httpx.Client
    monkeypatch.setattr('stock_daily.realtime_ai.httpx.Client', lambda **kw:original(transport=httpx.MockTransport(respond)))
    if stream_finish in ('length', None):
        with pytest.raises(ValueError, match='未完整结束'):
            analyze(config, report)
        assert len(seen) == 2
        return
    result = analyze(config, report)
    assert result['sections']['overview'][0]['evidence_ids'] == ['market']
    assert result['trade_policy']['stocks'][0]['evidence_ids'] == ['stock-600000.SH']
    assert result['trade_policy']['status'] == 'available'
    assert 'secret-key' not in str(seen)
    assert seen[0]['reasoning_effort'] == 'high'
    assert seen[0]['thinking'] == {'type': 'enabled'}
    assert seen[0]['max_tokens'] == 16384
    assert '不应展示的思考' not in str(result)
    assert len(seen) == (2 if stream_finish == 'repair' else 1)


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


@pytest.mark.parametrize('partial_failure', [False, True])
def test_large_decision_pool_batches_preserve_complete_coverage_or_block_buys(settings, partial_failure):
    from stock_daily.realtime_ai import analyze_batches

    report = research_input(snapshot_payload(), 'close')
    report['decision_universe'] = [f'{600000+i:06d}.SH' for i in range(25)]
    report['evidence'] += [{'id': 'stock-' + code, 'title': code, 'text': '报价'} for code in report['decision_universe']]
    seen = []
    def model(config, part):
        seen.append(part)
        if partial_failure and part['decision_universe'][0] == '600012.SH':
            raise ValueError('网关暂不可用')
        result = fake_analysis(config, part)
        result['trade_policy'] = {'status': 'available', 'version': 'evidence-decision-v1',
            'market_action': 'reduce' if '600024.SH' in part['decision_universe'] else 'normal',
            'text': '风险核验', 'evidence_ids': ['market'], 'stocks': [
                {'code': code, 'action': 'wait', 'score': 60, 'text': '等待确认',
                    'evidence_ids': ['stock-' + code]} for code in part['decision_universe']]}
        return result
    result = analyze_batches(settings, report, analyzer=model)
    assert result['status'] == 'available' and result['batch_count'] == 3
    assert len(seen) == 3 and all(len(p['decision_universe']) <= 12 for p in seen)
    for part in seen:
        assert {e['id'][6:] for e in part['evidence'] if e['id'].startswith('stock-')} == set(part['decision_universe'])
    if partial_failure:
        assert result['trade_policy']['status'] == 'failed'
    else:
        assert result['trade_policy']['market_action'] == 'reduce'
        assert {i['code'] for i in result['trade_policy']['stocks']} == set(report['decision_universe'])
