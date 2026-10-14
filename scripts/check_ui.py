"""Optional standalone browser smoke test for the locally running app.

Install playwright and its Chromium first; output is stored under ignored data/.
"""
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from stock_daily.config import Settings


def main():
    errors = []
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8000")
        expect(page.locator("#candidate-count")).to_contain_text("1 展示")
        expect(page.locator("#demo-banner")).to_be_visible()
        expect(page.locator("#quality-chip")).to_have_text("行情校验通过")
        page.locator(".row-detail[data-code]").first.click()
        expect(page.locator("#detail-dialog")).to_be_visible()
        expect(page.locator("#detail-chart svg")).to_be_visible()
        expect(page.locator("#detail-body")).to_contain_text("信号失效")
        page.locator("#detail-dialog .close-dialog").click()
        page.locator("#candidate-search").fill("不存在的公司")
        expect(page.locator("#candidate-table")).to_contain_text("没有符合")
        page.locator("#candidate-search").fill("")
        page.locator('[data-view="tracking"]').click()
        expect(page.locator("#tracking-table tr")).to_have_count(32)
        expect(page.locator("#tracking-cards")).to_contain_text("完整观察")
        page.locator('[data-view="watch"]').click()
        expect(page.locator("#watch-table tr")).to_have_count(3)
        page.locator("#add-watch").click()
        page.locator('[name="code"]').fill("600999.SH")
        page.locator('[name="note"]').fill("UI smoke test")
        page.locator('#watch-form [name="key"]').fill(Settings.from_env().api_key)
        page.locator('#watch-form button[type="submit"]').click()
        expect(page.locator("#watch-dialog")).not_to_be_visible()
        expect(page.locator("#watch-table")).to_contain_text("UI smoke test")
        page.once("dialog", lambda dialog: dialog.accept(Settings.from_env().api_key))
        page.locator('.remove-watch[data-code="600999.SH"]').click()
        expect(page.locator("#watch-table tr")).to_have_count(3)
        page.locator('[data-view="overview"]').click()
        Path("data").mkdir(exist_ok=True)
        page.screenshot(path="data/preview-desktop.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator("#market-cards .metric-card")).to_have_count(4)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.locator("#mobile-method").click()
        expect(page.locator("#method-dialog")).to_be_visible()
        page.locator("#method-dialog .close-dialog").click()
        page.screenshot(path="data/preview-mobile.png", full_page=True)
        browser.close()
    assert not errors, errors
    print("UI smoke test passed: overview, details, filters, tracking, watchlist writes and mobile layout")


if __name__ == "__main__":
    main()

