"""Render standalone HTML locally and atomically replace its matching PNG."""

import os
import tempfile
from pathlib import Path

from .providers import DataError


def export_png(html_path, output=None):
    from playwright.sync_api import sync_playwright

    html = Path(html_path).resolve()
    destination = Path(output).resolve() if output else html.with_suffix(".png")
    if not html.is_file() or html.suffix.lower() != ".html":
        raise DataError("请提供已经生成的本地 HTML 文件")
    if destination.suffix.lower() != ".png":
        raise DataError("PNG 输出文件必须以 .png 结尾")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", dir=destination.parent, delete=False) as file:
            temporary = file.name
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("http://**/*", lambda route: route.abort())
                page.route("https://**/*", lambda route: route.abort())
                page.goto(html.as_uri(), wait_until="load")
                page.wait_for_function("() => window.REPORT_READY === true", timeout=30000)
                # A bitmap cannot switch tabs or open collapsed sections later.
                # Let the report expose its dedicated, fully visible export view.
                try:
                    page.evaluate("""async () => {
                        if (typeof window.prepareReportForExport === 'function') {
                            await window.prepareReportForExport();
                        } else if (document.getElementById('quote-snapshot')) {
                            throw new Error('旧版实时HTML需要重新导出');
                        }
                    }""")
                except Exception as error:
                    raise DataError("图片内容展开失败；请用 export-snapshot 重新导出该时段HTML，原PNG已保留") from error
                page.evaluate("document.fonts.ready")
                if errors:
                    raise DataError("HTML 页面脚本异常，未替换 PNG")
                page.screenshot(path=temporary, full_page=True, animations="disabled")
            finally:
                browser.close()
        os.replace(temporary, destination)
    except DataError:
        raise
    except Exception as error:
        raise DataError("PNG 导出失败；请运行 .venv/bin/python -m playwright install chromium，并检查浏览器依赖") from error
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()
    return {"status": "success", "path": str(destination)}
