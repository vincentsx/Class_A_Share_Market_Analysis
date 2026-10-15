"""Cached interpretations of frozen reports; reads never invoke the model."""

import hashlib
import json

from sqlalchemy import select

from .config import now
from .db import report_analyses

LABELS = {'midday': '午间观察', 'close': '收盘观察', 'daily': '日线分析'}


def research_input(report, kind):
    daily = kind == 'daily'
    captured = report.get('generated_at') if daily else report.get('captured_at')
    captured = captured or report.get('acquired_at')
    news = report.get('news', {})
    items = news.get('items', [])
    evidence = [{'id': 'market', 'title': LABELS[kind] + '行情与数据范围',
        'text': json.dumps({'market': report['market'], 'quality': report.get('quality'),
            'coverage': report.get('coverage'), 'as_of': captured}, ensure_ascii=False)},
        {'id': 'observations', 'title': '行业与量价观察', 'text': json.dumps({
            'industries': report.get('industries', [])[:12],
            'candidates': [{k: r.get(k) for k in ('code', 'name', 'industry', 'reasons', 'risks', 'relative20')}
                for r in report.get('candidates', [])[:20]],
            'analysis': {k: report.get('analysis', {}).get(k) for k in ('summaries', 'counts', 'notes')}}, ensure_ascii=False)}]
    for item in items[:24]:
        evidence.append({'id': 'news-' + (item.get('id') or hashlib.sha256((item['title'] + item['published_at']).encode()).hexdigest()[:16]), 'title': item['title'], 'text': item['excerpt'],
            'published_at': item['published_at'], 'sources': item['sources'], 'kind': item['kind']})
    if daily:
        for card in report.get('evidence', {}).get('items', [])[:20]:
            evidence.append({'id': card['id'], 'title': card['title'], 'text': card.get('summary', ''),
                'sources': card.get('sources', [])})
    return {'day': report['day'], 'captured_at': captured, 'quote_start': captured, 'quote_end': captured,
        'report_label': LABELS[kind], 'coverage': report.get('coverage', report.get('quality', {}).get('coverage', 0)),
        'news': {'as_of': news.get('as_of', captured), 'status': news.get('status', 'not_collected')},
        'sentiment': {'sample_count': len(items), 'method': '仅包含该报告截止时已经留存的新闻和公告证据'},
        'evidence': evidence}


def fingerprint(settings, report, kind):
    return hashlib.sha256(json.dumps([research_input(report, kind), settings.ai_model,
        settings.ai_api_base_url, settings.ai_api_style, settings.realtime_ai_enabled, 'report-analysis-v2'],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def cached(db, settings, report, kind):
    rows = db.rows(select(report_analyses.c.payload).where(report_analyses.c.source == settings.source,
        report_analyses.c.day == report['day'], report_analyses.c.kind == kind,
        report_analyses.c.fingerprint == fingerprint(settings, report, kind)))
    return rows[0]['payload'] if rows else {'status': 'not_generated',
        'message': '该报告的AI研判尚未生成；后台任务生成后会随报告发布，刷新页面不会调用模型。'}


def generate(db, settings, report, kind, *, retry=False, analyzer=None):
    from .realtime_ai import analyze

    with db.lock('report-ai:' + settings.source, lease_minutes=10):
        previous = cached(db, settings, report, kind)
        if previous['status'] == 'available' or previous['status'] == 'failed' and not retry:
            return previous
        data = research_input(report, kind)
        try:
            result = (analyzer or analyze)(settings, data)
        except ValueError as error:
            result = {'status': 'failed', 'message': str(error)}
        result = {**result, 'as_of': data['captured_at'], 'generated_at': now().isoformat(),
            'label': LABELS[kind], 'evidence': data['evidence']}
        with db.engine.begin() as connection:
            db.upsert(connection, report_analyses, {'source': settings.source, 'day': report['day'],
                'kind': kind, 'fingerprint': fingerprint(settings, report, kind), 'payload': result})
        return result


def generate_saved(db, settings, day=None, *, retry=False):
    from .db import reports, snapshots
    from .providers import parse_day

    if day:
        parse_day(day)
    results = []
    for kind in ('midday', 'close'):
        rows = db.rows(select(snapshots).where(snapshots.c.source == settings.source,
            snapshots.c.slot == kind, snapshots.c.day == day if day else True)
            .order_by(snapshots.c.day.desc()).limit(1))
        if not rows:
            continue
        row = rows[0]
        payload = row['payload']
        payload['ai_analysis'] = generate(db, settings, payload, kind, retry=retry)
        with db.engine.begin() as connection:
            db.upsert(connection, snapshots, {**row, 'payload': payload})
        results.append({'kind': kind, 'day': row['day'], 'status': payload['ai_analysis']['status']})
    rows = db.rows(select(reports).where(reports.c.source == settings.source,
        reports.c.day == day if day else True).order_by(reports.c.day.desc()).limit(1))
    if rows:
        payload = rows[0]['payload']
        result = generate(db, settings, payload, 'daily', retry=retry)
        results.append({'kind': 'daily', 'day': payload['day'], 'status': result['status']})
    return {'status': 'success', 'analyses': results}


def public_analyses(db, settings):
    """Latest completed report of each kind, for lightweight publication updates."""
    rows = db.rows(select(report_analyses).where(report_analyses.c.source == settings.source))
    result = {}
    for row in sorted(rows, key=lambda r: (r['day'], r['payload'].get('generated_at', '')), reverse=True):
        if row['kind'] not in result and row['payload'].get('status') == 'available':
            result[row['kind']] = {'day': row['day'], 'analysis': row['payload']}
    return result
