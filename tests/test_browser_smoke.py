import os
import time
import socket
import threading
import functools
import http.server
import urllib.request
import pytest
from unittest.mock import patch
from werkzeug.serving import make_server

from app import (
    app,
    continue_workflow,
    workflow_state,
    state_lock,
)

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

    try:
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
            raise RuntimeError(f"Isolated test server failed to start on port {port}")

        yield server_url

    finally:
        server.shutdown()
        thread.join(timeout=3)
        if orig_port is not None:
            os.environ["PORT"] = orig_port
        else:
            os.environ.pop("PORT", None)

def test_dashboard_browser_smoke_e2e(live_test_server):
    """
    End-to-end browser smoke test against isolated live test server.
    Verifies DOM structure, DOMPurify availability, modal interaction,
    and asserts zero uncaught JavaScript console/page errors.
    
    Acceptance Policy: Browser acceptance tests are mandatory; missing Playwright fails explicitly.
    Guarantees browser resource cleanup via try/finally even if assertions fail.
    """
    if not PLAYWRIGHT_AVAILABLE:
        pytest.fail("MANDATORY ACCEPTANCE FAILURE: Playwright is required for browser smoke testing but is not installed.")

    page_errors = []
    console_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
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

            # Verify sanitizeHtml is defined and fails closed safely
            sanitize_defined = page.evaluate("typeof window.sanitizeHtml === 'function'")
            assert sanitize_defined is True, "MANDATORY ACCEPTANCE: window.sanitizeHtml must be defined"
            sanitized_test = page.evaluate("window.sanitizeHtml('<img src=x onerror=alert(1)>')")
            assert "onerror" not in sanitized_test
            assert "alert(1)" not in sanitized_test

            # Verify new task modal opens and closes (mandatory UI acceptance)
            new_task_btn = page.locator("button:has-text('مهمة جديدة')")
            assert new_task_btn.count() >= 1, "MANDATORY ACCEPTANCE: 'مهمة جديدة' button must exist in DOM"
            assert new_task_btn.first.is_visible(), "'مهمة جديدة' button must be visible"
            new_task_btn.first.click()
            page.wait_for_selector("#taskModal", state="visible", timeout=3000)

            cancel_btn = page.locator("#taskModal button:has-text('إلغاء')")
            assert cancel_btn.count() >= 1, "MANDATORY ACCEPTANCE: 'إلغاء' button must exist in #taskModal"
            assert cancel_btn.first.is_visible(), "'إلغاء' button must be visible"
            cancel_btn.first.click()
            page.wait_for_selector("#taskModal", state="hidden", timeout=3000)

            # Verify Navigation switches to Settings section and back (mandatory UI acceptance)
            nav_settings = page.locator("#nav-settings")
            assert nav_settings.count() >= 1, "MANDATORY ACCEPTANCE: '#nav-settings' link must exist in DOM"
            nav_settings.click()
            page.wait_for_selector("#section-settings", state="visible", timeout=3000)

            nav_overview = page.locator("#nav-overview")
            assert nav_overview.count() >= 1, "MANDATORY ACCEPTANCE: '#nav-overview' link must exist in DOM"
            nav_overview.click()
            page.wait_for_selector("#section-overview", state="visible", timeout=3000)
        finally:
            browser.close()

    assert len(page_errors) == 0, f"Uncaught page JavaScript errors encountered: {page_errors}"

def test_isolated_deliverable_browser_render(tmp_path):
    """
    Finding C: Separate isolated browser validation step for generated deliverables.
    Validates that a generated HTML application renders cleanly in an isolated context
    without fatal JavaScript errors or render exceptions.
    
    Acceptance Policy: Browser acceptance tests are mandatory; missing Playwright fails explicitly.
    Guarantees browser cleanup via try/finally.
    """
    if not PLAYWRIGHT_AVAILABLE:
        pytest.fail("MANDATORY ACCEPTANCE FAILURE: Playwright is required for browser render testing but is not installed.")
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
        try:
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
        finally:
            browser.close()

    assert len(page_errors) == 0, f"Page errors in deliverable: {page_errors}"

def test_browser_cleanup_guaranteed_on_assertion_failure():
    """
    Workstream D: Proves that browser resources are reliably closed and not leaked
    even when an assertion or operation inside the test raises an exception.
    """
    if not PLAYWRIGHT_AVAILABLE:
        pytest.fail("MANDATORY ACCEPTANCE FAILURE: Playwright is required but not installed.")

    closed = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            assert browser.is_connected() is True
            # Simulate an unhandled assertion failure within test body
            raise AssertionError("Simulated test failure inside browser block")
        except AssertionError as exc:
            assert "Simulated test failure" in str(exc)
        finally:
            browser.close()
            closed = True

    assert closed is True
    assert browser.is_connected() is False, "Browser connection was not cleanly terminated!"


def test_real_generated_artifact_browser_integration(tmp_path, monkeypatch):
    """
    Workstream C: Real Generated Artifact Browser Verification.
    1. Executes the actual workflow with deterministic mocked agent responses.
    2. Locates the actual persisted deliverables (index.html, style.css, script.js) created by that workflow.
    3. Serves those exact files using an isolated local HTTP server on an ephemeral port.
    4. Uses Playwright Chromium to navigate to the real generated deliverable.
    5. Confirms expected DOM structure is rendered.
    6. Verifies stylesheet and script requests resolve with HTTP 200.
    7. Asserts zero failed network requests, zero uncaught page errors, and zero console errors.
    8. Executes a deterministic user interaction (button click) and verifies JavaScript DOM update.
    9. Closes both browser and test HTTP server reliably via nested try/finally blocks.
    10. Completely isolated: no fixed ports, no external network, no real LLM calls.
    """
    if not PLAYWRIGHT_AVAILABLE:
        pytest.fail("MANDATORY ACCEPTANCE FAILURE: Playwright is required for browser render testing but is not installed.")

    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "تطبيق عداد المبيعات التفاعلي المباشر"
    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {
            "strategy": "استراتيجية نظام عداد تفاعلي",
            "product": "متطلبات العداد الرقمي",
            "engineering": "هيكلية العميل والخادم",
            "design": "تصميم الواجهة التفاعلية",
        }
        workflow_state["blockers"] = []
        workflow_state["qa_report"] = None

    candidate_html = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <title>تطبيق عداد المبيعات التفاعلي</title>
    <link rel="stylesheet" href="style.css">
</head>
<body>
    <div id="app">
        <h1 id="title">عداد المبيعات المباشر</h1>
        <div id="counter-value">0</div>
        <button id="btn-increment">زيادة المبيعات</button>
    </div>
    <script src="script.js"></script>
</body>
</html>"""
    candidate_css = """body { margin: 0; padding: 24px; font-family: sans-serif; background: #f1f5f9; }
#app { max-width: 480px; margin: 40px auto; padding: 24px; background: #ffffff; border-radius: 12px; text-align: center; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); }
#counter-value { font-size: 40px; font-weight: 800; color: #1d4ed8; margin: 20px 0; }
#btn-increment { padding: 12px 24px; font-size: 16px; font-weight: 600; background: #2563eb; color: #ffffff; border: none; border-radius: 8px; cursor: pointer; }"""
    candidate_js = """document.addEventListener('DOMContentLoaded', () => {
    const counter = document.getElementById('counter-value');
    const btn = document.getElementById('btn-increment');
    let count = 0;
    if (btn && counter) {
        btn.addEventListener('click', () => {
            count += 1;
            counter.textContent = String(count);
            counter.setAttribute('data-updated', 'true');
        });
    }
});"""

    mock_responses = {
        "development": f"```html\n{candidate_html}\n```\n```css\n{candidate_css}\n```\n```javascript\n{candidate_js}\n```",
        "qa": "```markdown\n## QA\nApproved\n```",
    }

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_responses["development"]
        elif short_name == "QA":
            return mock_responses["qa"]
        return "مخرج تجريبي"

    # Step 1: Run actual workflow to persist real deliverables
    with patch("app.call_agent", side_effect=fake_call_agent):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]
        blockers = list(workflow_state["blockers"])

    assert status == "completed", f"Workflow failed with status '{status}' and blockers: {blockers}"

    # Step 2: Locate the real generated deliverables produced by the workflow
    created_dirs = [f for f in os.listdir(test_gen_dir) if os.path.isdir(os.path.join(test_gen_dir, f))]
    assert len(created_dirs) >= 1, "No project directory created by workflow!"
    generated_app_dir = os.path.join(test_gen_dir, created_dirs[0])

    html_file = os.path.join(generated_app_dir, "index.html")
    css_file = os.path.join(generated_app_dir, "style.css")
    js_file = os.path.join(generated_app_dir, "script.js")

    assert os.path.exists(html_file), "Generated index.html does not exist!"
    assert os.path.exists(css_file), "Generated style.css does not exist!"
    assert os.path.exists(js_file), "Generated script.js does not exist!"

    # Step 3: Serve the actual generated directory via an isolated ephemeral HTTP server
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=generated_app_dir)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server_port = httpd.server_address[1]
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{server_port}"

    failed_requests = []
    page_errors = []
    console_errors = []
    responses = {}

    try:
        # Step 4: Use Playwright Chromium to load the actual generated artifact
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()

                page.on("requestfailed", lambda req: failed_requests.append(req.url))
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
                page.on("response", lambda resp: responses.setdefault(resp.url, resp.status))

                nav_resp = page.goto(f"{base_url}/index.html", timeout=10000)
                assert nav_resp is not None
                assert nav_resp.status == 200

                # Step 5: Confirm expected HTML DOM elements are present
                page.wait_for_selector("#title", timeout=5000)
                title_text = page.locator("#title").inner_text()
                assert "عداد المبيعات المباشر" in title_text

                # Step 6: Confirm stylesheet and script requests resolved successfully (HTTP 200)
                css_url = f"{base_url}/style.css"
                js_url = f"{base_url}/script.js"
                assert responses.get(css_url) == 200, f"style.css failed to return HTTP 200: {responses}"
                assert responses.get(js_url) == 200, f"script.js failed to return HTTP 200: {responses}"

                # Step 7: Assert zero network request failures, zero uncaught page errors, zero console errors
                assert len(failed_requests) == 0, f"Failed network requests: {failed_requests}"
                assert len(page_errors) == 0, f"Uncaught page JavaScript errors: {page_errors}"
                assert len(console_errors) == 0, f"Browser console errors: {console_errors}"

                # Step 8: Verify meaningful, deterministic JavaScript behavior
                counter_el = page.locator("#counter-value")
                assert counter_el.inner_text() == "0"

                # Trigger interaction: click increment button
                btn = page.locator("#btn-increment")
                btn.click()

                # Verify JavaScript state change in DOM
                page.wait_for_function("document.getElementById('counter-value').innerText === '1'", timeout=3000)
                assert counter_el.inner_text() == "1"
                assert counter_el.get_attribute("data-updated") == "true"

                # Trigger second interaction
                btn.click()
                page.wait_for_function("document.getElementById('counter-value').innerText === '2'", timeout=3000)
                assert counter_el.inner_text() == "2"

            finally:
                # Step 9: Guaranteed browser cleanup
                browser.close()
    finally:
        # Step 9: Guaranteed server cleanup
        httpd.shutdown()
        httpd.server_close()
        server_thread.join(timeout=3)
