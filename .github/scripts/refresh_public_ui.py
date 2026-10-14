"""Refresh the public live interface during deployment, using only the public bundle."""

import hashlib
import json
import re
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


def refresh(folder, repository, assets):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository')
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    records = manifest['reports']
    portfolio = next((r for r in records if r['slot'] == 'portfolio'), None)
    endpoint = ''
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
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        for original, replacement in [('<', r'\u003c'), ('>', r'\u003e'), ('&', r'\u0026'),
                                      ('\u2028', r'\u2028'), ('\u2029', r'\u2029')]:
            encoded = encoded.replace(original, replacement)
        content = re.sub(pattern, lambda m: m[1] + encoded + m[3], content, count=1, flags=re.S)
        javascript = (assets / 'static/app.js').read_text()
        content, count = re.subn(r'(<script nonce="[A-Za-z0-9_-]{20,80}">).*?(</script>)',
                                lambda m: m[1] + javascript + m[2], content, count=1, flags=re.S)
        if count != 1:
            raise ValueError('Missing public report script')
        content = content.replace("connect-src 'none'", 'connect-src https://raw.githubusercontent.com')
        content = content.replace('在线展示页每10分钟重载，部署完成后可看到更新。',
                                  '在线持仓每分钟检查轻量数据，行情每10分钟采集。')
        path.write_text(content)
        manifest['files'][portfolio['html']] = hashlib.sha256(path.read_bytes()).hexdigest()
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
    refresh(Path(sys.argv[1]), sys.argv[2], Path(__file__).resolve().parents[2] / 'stock_daily')
