from pathlib import Path

import pytest

from stock_daily.png_export import export_png
from stock_daily.providers import DataError


def test_browser_failure_does_not_replace_previous_png(tmp_path, monkeypatch):
    import playwright.sync_api

    class BrokenBrowser:
        def __enter__(self):
            raise RuntimeError('browser not installed')

        def __exit__(self, *args):
            pass

    html = tmp_path / 'report.html'
    html.write_text('<html></html>')
    image = html.with_suffix('.png')
    image.write_bytes(b'previous verified image')
    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', BrokenBrowser)
    with pytest.raises(DataError, match='PNG 导出失败'):
        export_png(html)
    assert image.read_bytes() == b'previous verified image'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['report.html', 'report.png']


def test_missing_html_cannot_create_or_replace_png(tmp_path):
    image = tmp_path / 'report.png'
    image.write_bytes(b'previous')
    with pytest.raises(DataError, match='已经生成'):
        export_png(Path(tmp_path / 'absent.html'), image)
    assert image.read_bytes() == b'previous'
