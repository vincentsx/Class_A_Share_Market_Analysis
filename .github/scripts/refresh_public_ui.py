"""Refresh the public live interface during deployment, using only the public bundle."""

import hashlib
import importlib.util
import json
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape


def encode_snapshot(payload):
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    for original, replacement in [('<', r'\u003c'), ('>', r'\u003e'), ('&', r'\u0026'),
                                  ('\u2028', r'\u2028'), ('\u2029', r'\u2029')]:
        encoded = encoded.replace(original, replacement)
    return encoded


def refresh(folder, repository, assets, live_plan=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository')
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    records = manifest['reports']
    portfolio = next((r for r in records if r['slot'] == 'portfolio'), None)
    endpoint = ''
    # Render the complete interface, including new panels and CSS, from public data only.
    spec = importlib.util.spec_from_file_location('public_static_report', assets / 'static_report.py')
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    fill_spec = importlib.util.spec_from_file_location('public_fill_history', assets / 'fills.py')
    fill_helpers = importlib.util.module_from_spec(fill_spec)
    fill_spec.loader.exec_module(fill_helpers)
    env = Environment(loader=FileSystemLoader(assets / 'templates'), autoescape=select_autoescape(['html']))
    helpers.template_helpers(env)
    for record in records:
        if record['slot'] not in {'evening', 'portfolio'}:
            continue
        path = (folder / record['html']).resolve()
        if not path.is_relative_to(folder.resolve()) or path.is_symlink():
            raise ValueError('Invalid public report path')
        match = re.search(r'<script id="offline-snapshot" type="application/json">(.*?)</script>', path.read_text(), re.S)
        payload = json.loads(match[1])
        if not payload.get('public'):
            raise ValueError('Daily report must be public')
        if (live_plan and live_plan.get('source') == payload['dashboard']['source']
                and live_plan.get('report_day') == payload['dashboard']['report']['day']
                and live_plan.get('trade_plan', {}).get('basis_day') == payload['dashboard']['report']['day']):
            # A newer-day plan must never be attached to an older archive.
            payload['dashboard']['report']['trade_plan'] = live_plan['trade_plan']
        if payload.get('portfolio'):
            endpoint = f'https://raw.githubusercontent.com/{repository}/pages-live/portfolio.json'
            payload['live_url'] = endpoint
        exported = datetime.fromisoformat(payload['exported_at']).astimezone(ZoneInfo('Asia/Shanghai'))
        html = env.get_template('index.html').render(
            source=payload['dashboard']['source'], public_report=True,
            portfolio_report=bool(payload.get('portfolio')), live_feed=bool(payload.get('live_url')),
            offline=True, nonce=secrets.token_urlsafe(24),
            exported_at=exported.strftime('%Y-%m-%d %H:%M（北京时间）'),
            css=(assets / 'static/style.css').read_text() + (assets / 'static/mail_report.css').read_text(),
            static_report=helpers.daily_view(payload, fill_history=fill_helpers.history_fills(
                payload.get('backtest'), payload['dashboard']['source'])),
            javascript=(assets / 'static/app.js').read_text(),
            snapshot=encode_snapshot(payload))
        path.write_text(html)
        manifest['files'][record['html']] = hashlib.sha256(path.read_bytes()).hexdigest()
    if portfolio:
        path = (folder / portfolio['html']).resolve()
        if not path.is_relative_to(folder.resolve()) or path.is_symlink():
            raise ValueError('Invalid public report path')
        content = path.read_text()
        pattern = r'(<script id="offline-snapshot" type="application/json">)(.*?)(</script>)'
        match = re.search(pattern, content, re.S)
        payload = json.loads(match[2])
        if not payload.get('public') or not payload.get('portfolio'):
            raise ValueError('Portfolio must be a public report')
        endpoint = f'https://raw.githubusercontent.com/{repository}/pages-live/portfolio.json'
        payload['live_url'] = endpoint
        encoded = encode_snapshot(payload)
        content = re.sub(pattern, lambda m: m[1] + encoded + m[3], content, count=1, flags=re.S)
        javascript = (assets / 'static/app.js').read_text()
        content, count = re.subn(r'(<script nonce="[A-Za-z0-9_-]{20,80}">).*?(</script>)',
                                lambda m: m[1] + javascript + m[2], content, count=1, flags=re.S)
        if count != 1:
            raise ValueError('Missing public report script')
        content = re.sub(r'connect-src [^;]+', 'connect-src https://raw.githubusercontent.com https://api.github.com', content)
        content = content.replace('在线展示页每10分钟重载，部署完成后可看到更新。',
                                  '在线持仓每分钟检查轻量数据，行情每10分钟采集。')
        path.write_text(content)
        manifest['files'][portfolio['html']] = hashlib.sha256(path.read_bytes()).hexdigest()
        source = payload['dashboard']['source']
        days = {r['day'] for r in records}
        quote_day = payload.get('backtest', {}).get('valuation', {}).get('day')
        if quote_day and re.fullmatch(r'\d{8}', quote_day):
            days.add(quote_day)
        for day in days:
            alias = f'reports/{source}-{day}-portfolio.html'
            alias_path = folder / alias
            if not alias_path.exists():
                alias_path.write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                                     '<meta http-equiv="refresh" content="0;url=../portfolio.html">'
                                     '<title>最新模拟持仓</title><a href="../portfolio.html">打开最新模拟持仓</a></html>')
                manifest['files'][alias] = hashlib.sha256(alias_path.read_bytes()).hexdigest()

    slots = {slot: next((r for r in records if r['slot'] == slot), None)
             for slot in ['portfolio', 'midday', 'close', 'evening']}
    env = Environment(loader=FileSystemLoader(assets / 'templates'), autoescape=select_autoescape(['html']))
    index = folder / 'index.html'
    index.write_text(env.get_template('pages.html').render(
        latest=records[0], slots=slots, dates=sorted({r['day'] for r in records}, reverse=True),
        reports=records, built_at=manifest['built_at'], live_url=endpoint,
        live_javascript=(assets / 'static/public_live.js').read_text()))
    manifest['files']['index.html'] = hashlib.sha256(index.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    repository = sys.argv[2]
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository')
    try:
        url = f'https://raw.githubusercontent.com/{repository}/pages-live/portfolio.json?v={secrets.token_hex(8)}'
        with urlopen(url, timeout=15) as response:
            live_plan = json.load(response)
    except Exception:
        live_plan = None  # Bundled plans remain usable if the optional public feed is unavailable.
    refresh(Path(sys.argv[1]), repository, Path(__file__).resolve().parents[2] / 'stock_daily', live_plan)
