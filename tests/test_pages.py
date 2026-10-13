import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_mail_preview import browser as browser
from test_mail_preview import snapshot_payload

from stock_daily.db import snapshots, watchlist
from stock_daily.pages import export_pages, public_snapshot, publish_pages
from stock_daily.providers import DataError


def fake_png(path):
    Path(path).with_suffix('.png').write_bytes(b'\x89PNG\r\n\x1a\npublic-image')


def save_snapshot(db, payload):
    with db.engine.begin() as connection:
        db.upsert(connection, snapshots, {'source': payload['source'], 'day': payload['day'],
                                          'slot': payload['slot'], 'payload': payload})


def test_public_snapshot_removes_personal_markers_without_mutating_saved_data():
    payload = snapshot_payload()
    payload['focus'][0]['note'] = 'private_note'
    payload['news']['items'][0]['mentioned_stocks'][0]['watched'] = True
    original = copy.deepcopy(payload)
    result = public_snapshot(payload)
    text = json.dumps(result)
    assert result['focus'] == []
    assert 'private_note' not in text and 'watched' not in text and '自选股' not in text
    assert result['analysis']['volume_up'] and result['quotes']
    assert payload == original


def test_public_site_retains_dates_caches_images_and_clears_old_files(db, settings, tmp_path):
    payload = snapshot_payload()
    for day, slot in [('20261009', 'midday'), ('20261012', 'close'), ('20261013', 'midday')]:
        row = {**payload, 'day': day, 'slot': slot}
        save_snapshot(db, row)
    config = replace(settings, source='baostock', pages_retain_days=2)
    output = tmp_path / 'site'
    first = export_pages(db, config, output, render_image=fake_png)
    manifest = json.loads((output / 'manifest.json').read_text())
    assert first['reports'] == 2 and first['latest_day'] == '20261013'
    assert {r['day'] for r in manifest['reports']} == {'20261012', '20261013'}
    assert (output / 'latest.html').read_text().find('reports/baostock-20261013-midday.html') >= 0
    assert not (output / 'evening.html').exists()
    assert 'reports/baostock-20261012-close.html' in (output / 'index.html').read_text()
    for file in output.rglob('*.html'):
        assert 'private_note' not in file.read_text()
    (output / 'do-not-publish.txt').write_text('unexpected content')

    def no_render(_):
        pytest.fail('Unchanged public report should reuse the validated PNG')

    again = export_pages(db, config, output, render_image=no_render)
    assert first['signature'] == again['signature']
    assert not (output / 'do-not-publish.txt').exists()
    assert (output / '.nojekyll').is_file()


def test_failed_public_render_preserves_previous_site(db, settings, tmp_path):
    payload = snapshot_payload()
    save_snapshot(db, payload)
    config = replace(settings, source='baostock')
    output = tmp_path / 'site'
    export_pages(db, config, output, render_image=fake_png)
    original = (output / 'manifest.json').read_bytes()
    payload['quotes'][0]['price'] = 11
    save_snapshot(db, payload)

    def fail(_):
        raise DataError('render failed')

    with pytest.raises(DataError, match='render failed'):
        export_pages(db, config, output, render_image=fail)
    assert (output / 'manifest.json').read_bytes() == original
    (tmp_path / 'unsafe').mkdir()
    (tmp_path / 'unsafe' / 'private.txt').write_text('private')
    with pytest.raises(DataError, match='独立目录'):
        export_pages(db, config, tmp_path / 'unsafe', render_image=fake_png)


def test_public_daily_report_excludes_watch_notes_mail_and_task_errors(populated, tmp_path):
    db, settings, _ = populated
    db.save_watch('600000.SH', 'private_watch_note')
    task = db.start_run(settings.source, '20261009', 'private_task')
    db.finish_run(task, 'failed', 'private_task_error')
    config = replace(settings, smtp_password='private_smtp_password', api_key='private_api_key',
                     mail_to='private-recipient@example.com')
    output = tmp_path / 'site'
    export_pages(db, config, output, render_image=fake_png)
    html = next((output / 'reports').glob('*.html')).read_text()
    for private in ('private_watch_note', 'private_task_error', 'private-recipient@example.com',
                    'private_smtp_password', 'private_api_key'):
        assert private not in html
    assert '"watchlist":[]' in html and '"runs":[]' in html
    assert db.rows(select(watchlist))


def test_publish_uploads_only_public_manifest_files_and_dispatches(monkeypatch, db, settings, tmp_path):
    import stock_daily.pages as pages

    config = replace(settings, html_report_dir=str(tmp_path / 'html'))
    output = tmp_path / 'pages'
    output.mkdir()
    (output / 'index.html').write_text('public')
    (output / 'private.env').write_text('secret')
    manifest = {'files': {'index.html': pages.digest(b'public')}}
    (output / 'manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(pages, 'export_pages', lambda *args: {'status': 'success', 'signature': 'signature'})
    calls = []

    def gh(*arguments, **kwargs):
        calls.append(arguments)
        if arguments[:2] == ('release', 'upload'):
            import tarfile
            with tarfile.open(arguments[3]) as archive:
                assert set(archive.getnames()) == {'index.html', 'manifest.json'}
        if arguments[0] == 'api':
            return SimpleNamespace(returncode=0, stdout=json.dumps({'build_type': 'workflow', 'state': 'active'}))
        return SimpleNamespace(returncode=0, stdout='[]')

    monkeypatch.setattr(pages, 'gh', gh)
    result = publish_pages(db, config, 'example/report')
    assert result['status'] == 'dispatched'
    assert ('workflow', 'run', 'pages.yml', '--repo', 'example/report', '--ref', 'main') in calls
    assert json.loads((tmp_path / '.pages-published.json').read_text())['signature'] == 'signature'


def test_empty_database_does_not_publish_placeholder_or_replace_site(db, settings, tmp_path):
    with pytest.raises(DataError, match='没有已发布报告'):
        export_pages(db, settings, tmp_path / 'site', render_image=fake_png)
    assert not (tmp_path / 'site').exists()


def test_phone_public_home_and_report_keep_relative_links_and_filters(browser, db, settings, tmp_path):
    save_snapshot(db, snapshot_payload())
    config = replace(settings, source='baostock')
    output = tmp_path / 'project'
    export_pages(db, config, output, render_image=fake_png)
    with browser.new_context(viewport={'width': 390, 'height': 844}) as context:
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto((output / 'index.html').as_uri())
        assert page.get_by_role('heading', name='最新报告 · 收盘观察').is_visible()
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        for href in page.locator('a').evaluate_all('(links) => links.map(a => a.getAttribute("href"))'):
            assert not href.startswith('/')
        page.get_by_role('link', name='查看完整报告', exact=True).click()
        page.wait_for_function('() => window.REPORT_READY === true')
        assert page.locator('#interactive-report').is_visible()
        assert page.locator('#focus').is_hidden()
        page.locator('#quote-section').evaluate('(e) => e.open = true')
        page.locator('#search').fill('600000')
        assert page.locator('#quotes tr').count() == 1
        assert not errors
