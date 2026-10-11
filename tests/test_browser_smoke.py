import pytest
import urllib.request

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

DASHBOARD_URL = "http://127.0.0.1:8080"

def is_server_online(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False

@pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="playwright is not installed")
def test_dashboard_browser_smoke_e2e():
    if not is_server_online(DASHBOARD_URL):
        pytest.skip(f"Dashboard server at {DASHBOARD_URL} is not reachable")

    page_errors = []
    console_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        response = page.goto(DASHBOARD_URL, timeout=10000)
        assert response is not None
        assert response.status == 200

        # Assert Core DOM structure
        page.wait_for_selector("#stat-total-agents", timeout=5000)
        assert page.locator("#taskModal").count() == 1
        assert page.locator("#task-input").count() == 1
        
        # Verify DOMPurify is loaded and available for safe rendering
        purify_exists = page.evaluate("typeof window.DOMPurify !== 'undefined'")
        assert purify_exists is True, "DOMPurify is expected to be loaded in the browser environment"

        # Verify sanitizeHtml fails closed safely
        sanitized_test = page.evaluate("window.sanitizeHtml ? window.sanitizeHtml('<img src=x onerror=alert(1)>') : null")
        if sanitized_test is not None:
            assert "onerror" not in sanitized_test
            assert "alert(1)" not in sanitized_test

        # Verify new task modal opens and closes
        new_task_btn = page.locator("button:has-text('مهمة جديدة')")
        if new_task_btn.count() > 0:
            new_task_btn.click()
            page.wait_for_selector("#taskModal", state="visible", timeout=3000)
            cancel_btn = page.locator("button:has-text('إلغاء')")
            if cancel_btn.count() > 0:
                cancel_btn.click()
                page.wait_for_selector("#taskModal", state="hidden", timeout=3000)

        # Verify Navigation switches to Settings section
        nav_settings = page.locator("#nav-settings")
        if nav_settings.count() > 0:
            nav_settings.click()
            page.wait_for_selector("#section-settings", state="visible", timeout=3000)
            # Switch back to overview
            page.locator("#nav-overview").click()
            page.wait_for_selector("#section-overview", state="visible", timeout=3000)

        browser.close()

    # Verify no uncaught JavaScript exceptions occurred during smoke execution
    assert len(page_errors) == 0, f"Uncaught page JavaScript errors encountered: {page_errors}"
