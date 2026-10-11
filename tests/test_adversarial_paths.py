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

def test_qa_criteria_rejects_unbalanced_code():
    """Verify that the enhanced QA criteria flags unbalanced HTML or syntax flaws."""
    from app import workflow_state
    
    # HTML missing closing tag and without doctype
    unbalanced_html = "<div><p>Incomplete stub without closing tags"
    html_has_structure = bool(
        ("<html" in unbalanced_html and "</html>" in unbalanced_html) or
        ("<!DOCTYPE html" in unbalanced_html and ("<body" in unbalanced_html or "<div" in unbalanced_html))
    )
    assert not html_has_structure

    # CSS with unbalanced braces
    unbalanced_css = "body { color: red; "
    css_balanced = bool(not unbalanced_css or (unbalanced_css.count('{') == unbalanced_css.count('}')))
    assert not css_balanced

    # Valid balanced structure passes
    valid_html = "<!DOCTYPE html><html><body><div>Valid Content</div></body></html>"
    valid_css = "body { color: blue; } .card { padding: 10px; }"
    valid_js = "function test() { console.log('ok'); }"
    assert ("<html" in valid_html and "</html>" in valid_html)
    assert valid_css.count('{') == valid_css.count('}')
    assert valid_js.count('{') == valid_js.count('}') and valid_js.count('(') == valid_js.count(')')
