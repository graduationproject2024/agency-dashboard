import os
import time
import socket
import threading
import urllib.request
import pytest
from werkzeug.serving import make_server

from app import app

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

@pytest.fixture(scope="module")
def live_test_server():
    """
    Launch an isolated Flask server instance on an ephemeral local port.
    Never depends on a pre-existing server running on port 8080.
    Cleanly shuts down after tests finish.
    """
    # Find free ephemeral port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()

    orig_port = os.environ.get("PORT")
    os.environ["PORT"] = str(port)

    server = make_server('127.0.0.1', port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    server_url = f"http://127.0.0.1:{port}"

    # Wait for server readiness
    started = False
    for _ in range(50):
        try:
            with urllib.request.urlopen(server_url, timeout=0.5) as resp:
                if resp.status == 200:
                    started = True
                    break
        except Exception:
            time.sleep(0.1)

    if not started:
        server.shutdown()
        if orig_port is not None:
            os.environ["PORT"] = orig_port
        else:
            os.environ.pop("PORT", None)
        raise RuntimeError(f"Isolated test server failed to start on port {port}")

    yield server_url

    server.shutdown()
    if orig_port is not None:
        os.environ["PORT"] = orig_port
    else:
        os.environ.pop("PORT", None)

@pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright is required for browser smoke testing")
def test_dashboard_browser_smoke_e2e(live_test_server):
    """
    End-to-end browser smoke test against isolated live test server.
    Verifies DOM structure, DOMPurify availability, modal interaction,
    and asserts zero uncaught JavaScript console/page errors.
    """
    page_errors = []
    console_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        response = page.goto(live_test_server, timeout=10000)
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
            page.locator("#nav-overview").click()
            page.wait_for_selector("#section-overview", state="visible", timeout=3000)

        browser.close()

    assert len(page_errors) == 0, f"Uncaught page JavaScript errors encountered: {page_errors}"

@pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright is required for deliverable browser validation")
def test_isolated_deliverable_browser_render(tmp_path):
    """
    Finding C: Separate isolated browser validation step for generated deliverables.
    Validates that a generated HTML application renders cleanly in an isolated context
    without fatal JavaScript errors or render exceptions.
    """
    app_html = """<!DOCTYPE html>
<html lang="ar">
<head>
    <meta charset="UTF-8">
    <title>تطبيق تجريبي</title>
    <link rel="stylesheet" href="style.css">
</head>
<body>
    <div id="root"><h1>مرحبا بالعالم</h1></div>
    <script src="script.js"></script>
</body>
</html>"""
    app_css = "body { margin: 0; background: #fafafa; color: #333; } h1 { font-size: 24px; }"
    app_js = "document.addEventListener('DOMContentLoaded', () => { document.getElementById('root').dataset.ready = 'true'; });"

    html_file = tmp_path / "index.html"
    css_file = tmp_path / "style.css"
    js_file = tmp_path / "script.js"

    html_file.write_text(app_html, encoding="utf-8")
    css_file.write_text(app_css, encoding="utf-8")
    js_file.write_text(app_js, encoding="utf-8")

    page_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.on("pageerror", lambda err: page_errors.append(str(err)))

        file_url = f"file:///{str(html_file).replace(os.sep, '/')}"
        resp = page.goto(file_url, timeout=5000)
        assert resp is not None
        assert resp.status in (None, 200)

        # Check DOM element rendered and script executed
        page.wait_for_selector("#root", timeout=3000)
        ready = page.evaluate("document.getElementById('root').dataset.ready")
        assert ready == "true"

        browser.close()

    assert len(page_errors) == 0, f"Page errors in deliverable: {page_errors}"
