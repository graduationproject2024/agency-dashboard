import os
import pytest
from app import app, safe_resolve_project_dir, GENERATED_APPS_DIR, WINDOWS_RESERVED

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_windows_reserved_device_names_blocked():
    """Verify that Windows reserved device names cannot be resolved or accessed."""
    for reserved in ["CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "con.txt", "aux.html"]:
        assert safe_resolve_project_dir(reserved) is None

def test_safe_resolve_project_dir_blocks_root_generated_apps_dir():
    """Verify safe_resolve_project_dir never resolves to GENERATED_APPS_DIR root itself."""
    assert safe_resolve_project_dir(".") is None
    assert safe_resolve_project_dir("") is None

def test_csrf_origin_validation(client):
    """Verify that requests from foreign origins are blocked with 403 Forbidden."""
    untrusted_headers = {"Origin": "https://malicious-attacker.com"}
    trusted_headers = {"Origin": "http://127.0.0.1:8080"}

    # Foreign origin blocked on /api/start_task
    res = client.post('/api/start_task', json={"task": "malicious task"}, headers=untrusted_headers)
    assert res.status_code == 403
    assert "Invalid origin" in res.get_json()["error"]

    # Foreign origin blocked on /api/settings
    res = client.post('/api/settings', json={"provider": "gemini"}, headers=untrusted_headers)
    assert res.status_code == 403

    # Foreign origin blocked on /api/open_folder
    res = client.post('/api/open_folder', json={"name": "test"}, headers=untrusted_headers)
    assert res.status_code == 403

    # Foreign origin blocked on /api/approve
    res = client.post('/api/approve', json={"feedback": "approved"}, headers=untrusted_headers)
    assert res.status_code == 403

    # Trusted local origin allowed through CSRF check (will fail or succeed based on payload validation)
    res = client.post('/api/settings', json={"provider": "gemini"}, headers=trusted_headers)
    assert res.status_code == 200

def test_settings_rejects_invalid_types(client):
    """Verify that /api/settings strictly requires string values and rejects non-strings."""
    # Integer value for openrouter_model
    res = client.post('/api/settings', json={"openrouter_model": 12345})
    assert res.status_code == 400
    assert "must be a string" in res.get_json()["error"]

    # Boolean value for opencode_url
    res = client.post('/api/settings', json={"opencode_url": True})
    assert res.status_code == 400
    assert "must be a string" in res.get_json()["error"]

    # Array value for provider
    res = client.post('/api/settings', json={"provider": ["invalid", "list"]})
    assert res.status_code == 400
    assert "must be a string" in res.get_json()["error"]

def test_open_folder_blocks_root_directory_access(client):
    """Verify POST /api/open_folder rejects path equal to GENERATED_APPS_DIR itself."""
    res = client.post('/api/open_folder', json={"path": GENERATED_APPS_DIR})
    assert res.status_code == 403

def test_production_run_qa_checks_directly():
    """Verify that the production run_qa_checks function directly flags invalid code and fatal errors."""
    from app import run_qa_checks
    
    valid_html = "<!DOCTYPE html><html><head><title>Test</title></head><body><div>Valid App Content</div></body></html>"
    valid_css = "body { color: blue; } .card { padding: 10px; }"
    valid_js = "function test() { console.log('ok'); }"

    # 1. Missing or empty HTML
    res_empty = run_qa_checks("", valid_css, valid_js)
    assert not res_empty["passed"]
    assert any("missing or empty" in e for e in res_empty["errors"])

    # 2. Truncated / short HTML (< 50 chars)
    res_short = run_qa_checks("<html>stub</html>", valid_css, valid_js)
    assert not res_short["passed"]
    assert any("truncated" in e for e in res_short["errors"])

    # 3. Incomplete structure lacking closing tags
    res_unclosed = run_qa_checks("<div><p>Incomplete stub without doctype or closing html tags " * 3, valid_css, valid_js)
    assert not res_unclosed["passed"]
    assert any("structure" in e for e in res_unclosed["errors"])

    # 4. Fatal error placeholder strings in HTML
    res_fatal = run_qa_checks(f"<!DOCTYPE html><html><body>فشل الاتصال بالخادم {'x'*50}</body></html>", valid_css, valid_js)
    assert not res_fatal["passed"]
    assert any("Fatal error" in e for e in res_fatal["errors"])

    # 5. Unbalanced CSS braces
    res_bad_css = run_qa_checks(valid_html, "body { color: red; ", valid_js)
    assert not res_bad_css["passed"]
    assert any("CSS" in e for e in res_bad_css["errors"])

    # 6. Unbalanced JS delimiters
    res_bad_js = run_qa_checks(valid_html, valid_css, "function test() { if (true) { console.log('unclosed'); }")
    assert not res_bad_js["passed"]
    assert any("JavaScript" in e for e in res_bad_js["errors"])

    # 7. Valid complete deliverable passes
    res_valid = run_qa_checks(valid_html, valid_css, valid_js)
    assert res_valid["passed"]
    assert len(res_valid["errors"]) == 0
    assert all(res_valid["checks"].values())
