import pytest
from app import app

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

STATE_CHANGING_ENDPOINTS = [
    ('/api/start_task', {"task": "test task"}),
    ('/api/approve', {"feedback": "approved"}),
    ('/api/open_folder', {"name": "test_folder"}),
    ('/api/settings', {"provider": "gemini"})
]

def test_hostile_origins_rejected_on_all_endpoints(client):
    """Verify that every state-changing endpoint rejects foreign browser origins."""
    hostile_headers = {"Origin": "https://malicious-website.com"}
    for endpoint, payload in STATE_CHANGING_ENDPOINTS:
        res = client.post(endpoint, json=payload, headers=hostile_headers)
        assert res.status_code == 403, f"Endpoint {endpoint} allowed foreign origin!"
        assert "Invalid origin" in res.get_json()["error"]

def test_null_origin_rejected(client):
    """Verify that Origin: null (from sandboxed iframes or data URIs) is rejected."""
    headers = {"Origin": "null"}
    for endpoint, payload in STATE_CHANGING_ENDPOINTS:
        res = client.post(endpoint, json=payload, headers=headers)
        assert res.status_code == 403
        assert "Invalid origin" in res.get_json()["error"]

def test_malformed_origin_rejected(client):
    """Verify that malformed Origin headers (invalid ports, schemes) are rejected."""
    bad_origins = [
        "http://attacker.com:999999",
        "javascript:alert(1)",
        "ftp://localhost",
        "http://",
        "   "
    ]
    for bad in bad_origins:
        res = client.post('/api/settings', json={"provider": "gemini"}, headers={"Origin": bad})
        assert res.status_code == 403

def test_sec_fetch_site_cross_site_rejected(client):
    """Verify that requests flagged as cross-site by browser Fetch Metadata are blocked."""
    headers = {
        "Sec-Fetch-Site": "cross-site",
        "Origin": "http://127.0.0.1:8080"  # Spoofed origin but browser marked cross-site
    }
    res = client.post('/api/settings', json={"provider": "gemini"}, headers=headers)
    assert res.status_code == 403

def test_foreign_referer_rejected_when_origin_absent(client):
    """Verify that foreign Referer is rejected when Origin is not provided."""
    headers = {"Referer": "https://attacker.com/evil_page.html"}
    res = client.post('/api/settings', json={"provider": "gemini"}, headers=headers)
    assert res.status_code == 403

def test_host_header_injection_does_not_bypass_origin_check(client):
    """Verify that spoofing Host: evil.com does NOT authorize Origin: http://evil.com."""
    headers = {
        "Host": "evil.com",
        "Origin": "http://evil.com"
    }
    res = client.post('/api/settings', json={"provider": "gemini"}, headers=headers)
    assert res.status_code == 403

def test_form_submission_without_json_content_type_rejected(client):
    """Verify that browser form POSTs (non-JSON) without valid origin are rejected."""
    res = client.post('/api/start_task', data="task=evil_task", content_type="application/x-www-form-urlencoded")
    assert res.status_code == 403

def test_valid_localhost_origin_allowed(client):
    """Verify that requests from legitimate 127.0.0.1 and localhost origins are accepted."""
    for valid_origin in ["http://127.0.0.1:8080", "http://localhost:8080", "http://127.0.0.1", "http://localhost"]:
        res = client.post('/api/settings', json={"provider": "gemini"}, headers={"Origin": valid_origin})
        assert res.status_code == 200
