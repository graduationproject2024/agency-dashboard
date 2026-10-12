import os
import pytest

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

INDEX_HTML_PATH = os.path.abspath("index.html")

@pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="Playwright is required for browser-level XSS verification")
def test_production_sanitize_html_xss_prevention_in_browser():
    """
    Exercise the actual production sanitizeHtml and escapeHtml functions in a real
    Chromium browser DOM environment. Verifies active payload neutralization and
    fail-closed fallback when DOMPurify is unavailable.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Load index.html via file:// so script.js and DOMPurify are fully loaded
        file_url = f"file:///{INDEX_HTML_PATH.replace(os.sep, '/')}"
        page.goto(file_url)

        # 1. Verify production sanitizer functions are defined
        assert page.evaluate("typeof window.escapeHtml === 'function'"), "escapeHtml not defined in script.js"
        assert page.evaluate("typeof window.sanitizeHtml === 'function'"), "sanitizeHtml not defined in script.js"
        assert page.evaluate("typeof window.DOMPurify !== 'undefined'"), "DOMPurify must be loaded in index.html"

        # 2. Test <script> injection payload
        script_payload = "<script>window.__xss_executed = true;</script><p>Clean Text</p>"
        result = page.evaluate("""(payload) => {
            const container = document.createElement('div');
            container.id = 'xss-test-container';
            container.innerHTML = window.sanitizeHtml(payload);
            document.body.appendChild(container);
            return {
                executed: Boolean(window.__xss_executed),
                hasScriptTag: container.querySelector('script') !== null,
                html: container.innerHTML
            };
        }""", script_payload)
        assert result["executed"] is False, "Script tag executed inside sanitized container!"
        assert result["hasScriptTag"] is False, "Script tag was not stripped by sanitizer!"
        assert "Clean Text" in result["html"]

        # 3. Test event handler payloads (<img onerror>, <svg onload>)
        event_payloads = [
            '<img src="invalid_image.png" onerror="window.__xss_img_executed=true;">',
            '<svg onload="window.__xss_svg_executed=true;"></svg>',
            '<button onclick="window.__xss_btn_executed=true;">Click</button>'
        ]
        for payload in event_payloads:
            res = page.evaluate("""(payload) => {
                const div = document.createElement('div');
                div.innerHTML = window.sanitizeHtml(payload);
                document.body.appendChild(div);
                const el = div.firstElementChild;
                return {
                    hasOnerror: el ? el.hasAttribute('onerror') : false,
                    hasOnload: el ? el.hasAttribute('onload') : false,
                    hasOnclick: el ? el.hasAttribute('onclick') : false,
                    imgFlag: Boolean(window.__xss_img_executed),
                    svgFlag: Boolean(window.__xss_svg_executed),
                    btnFlag: Boolean(window.__xss_btn_executed)
                };
            }""", payload)
            assert not res["hasOnerror"], f"onerror attribute remained in sanitized output: {payload}"
            assert not res["hasOnload"], f"onload attribute remained in sanitized output: {payload}"
            assert not res["hasOnclick"], f"onclick attribute remained in sanitized output: {payload}"
            assert not res["imgFlag"] and not res["svgFlag"] and not res["btnFlag"]

        # 4. Test dangerous pseudo-protocols (javascript:, data:)
        dangerous_links = [
            '<a href="javascript:window.__xss_link_executed=true;">Evil Link</a>',
            '<a href="vbscript:msgbox(1)">VBScript Link</a>'
        ]
        for link in dangerous_links:
            res = page.evaluate("""(link) => {
                const div = document.createElement('div');
                div.innerHTML = window.sanitizeHtml(link);
                const a = div.querySelector('a');
                return a ? a.getAttribute('href') : null;
            }""", link)
            assert res is None or not res.lower().startswith('javascript:'), f"Dangerous javascript: URI survived: {res}"

        # 5. Test legitimate markup preservation
        safe_markup = '<p class="lead"><strong>مرحبا</strong> بالجميع <code>code</code></p>'
        safe_res = page.evaluate("""(markup) => {
            const div = document.createElement('div');
            div.innerHTML = window.sanitizeHtml(markup);
            return {
                hasP: div.querySelector('p') !== null,
                hasStrong: div.querySelector('strong') !== null,
                hasCode: div.querySelector('code') !== null,
                text: div.textContent.trim()
            };
        }""", safe_markup)
        assert safe_res["hasP"] is True
        assert safe_res["hasStrong"] is True
        assert safe_res["hasCode"] is True
        assert "مرحبا" in safe_res["text"]

        # 6. Test Fail-Closed behavior when DOMPurify is unavailable
        fallback_res = page.evaluate("""() => {
            // Temporarily disable DOMPurify
            const origPurify = window.DOMPurify;
            window.DOMPurify = undefined;
            try {
                const dirty = '<img src=x onerror=alert(1)><b>Hello</b>';
                const sanitized = window.sanitizeHtml(dirty);
                const container = document.createElement('div');
                container.innerHTML = sanitized;
                return {
                    sanitizedText: sanitized,
                    hasImgElement: container.querySelector('img') !== null,
                    hasBElement: container.querySelector('b') !== null,
                    textContent: container.textContent
                };
            } finally {
                window.DOMPurify = origPurify;
            }
        }""")
        assert fallback_res["hasImgElement"] is False, "Fallback failed open: rendered active <img> tag!"
        assert fallback_res["hasBElement"] is False, "Fallback failed open: rendered active <b> tag instead of escaping!"
        assert "&lt;" in fallback_res["sanitizedText"], "Fallback did not escape HTML entities!"
        assert "<img src=x onerror=alert(1)><b>Hello</b>" in fallback_res["textContent"]

        browser.close()
