import copy
import json
import os
import threading
import types
import pytest
import app as app_module
from app import app, workflow_state, state_lock

class DummyThread:
    def __init__(self, *args, **kwargs):
        pass
    def start(self):
        pass
    def join(self, timeout=None):
        pass
    def is_alive(self):
        return False

@pytest.fixture(autouse=True)
def isolate_csrf_test_environment(monkeypatch, tmp_path):
    # Redirect GENERATED_APPS_DIR and create test_folder
    temp_apps = tmp_path / "generated_apps"
    temp_apps.mkdir(exist_ok=True)
    (temp_apps / "test_folder").mkdir(exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", str(temp_apps))

    # Redirect SETTINGS_FILE
    temp_settings = tmp_path / "settings_csrf_test.json"
    temp_settings.write_text(json.dumps({"provider": "gemini"}), encoding="utf-8")
    monkeypatch.setattr("app.SETTINGS_FILE", str(temp_settings))

    # Mock threading module global
    mock_threading = types.ModuleType("threading_mock")
    mock_threading.__dict__.update(threading.__dict__)
    mock_threading.Thread = DummyThread
    monkeypatch.setattr("app.threading", mock_threading)

    # Patch GUI / process launches
    monkeypatch.setattr("os.startfile", lambda path: None, raising=False)
    monkeypatch.setattr("app.subprocess.Popen", lambda *args, **kwargs: None)

    # Snapshot workflow_state
    with state_lock:
        original_state = copy.deepcopy(workflow_state)

    try:
        yield
    finally:
        with state_lock:
            workflow_state.clear()
            workflow_state.update(copy.deepcopy(original_state))

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

@pytest.mark.parametrize("endpoint,payload", STATE_CHANGING_ENDPOINTS)
def test_hostile_origins_rejected_on_all_endpoints(client, endpoint, payload):
    """Verify that every state-changing endpoint rejects foreign browser origins."""
    hostile_headers = {"Origin": "https://malicious-website.com"}
    res = client.post(endpoint, json=payload, headers=hostile_headers)
    assert res.status_code == 403, f"Endpoint {endpoint} allowed foreign origin!"
    assert "Invalid origin" in res.get_json()["error"]

@pytest.mark.parametrize("endpoint,payload", STATE_CHANGING_ENDPOINTS)
def test_null_origin_rejected_on_all_endpoints(client, endpoint, payload):
    """Verify that Origin: null (sandboxed iframes, data URIs) is rejected on all endpoints."""
    headers = {"Origin": "null"}
    res = client.post(endpoint, json=payload, headers=headers)
    assert res.status_code == 403
    assert "Invalid origin" in res.get_json()["error"]

@pytest.mark.parametrize("endpoint,payload", STATE_CHANGING_ENDPOINTS)
@pytest.mark.parametrize("bad_port", [3000, 5000, 9000, 80, 443])
def test_unexpected_ports_on_localhost_rejected(client, endpoint, payload, bad_port):
    """Verify that localhost/127.0.0.1 origins on unexpected ports are strictly rejected."""
    for host in ["127.0.0.1", "localhost"]:
        headers = {"Origin": f"http://{host}:{bad_port}"}
        res = client.post(endpoint, json=payload, headers=headers)
        assert res.status_code == 403, f"Endpoint {endpoint} allowed unexpected port {bad_port} on {host}!"

@pytest.mark.parametrize("endpoint,payload", STATE_CHANGING_ENDPOINTS)
@pytest.mark.parametrize("untrusted_host", [
    "evil.com",
    "localhost.evil.com",
    "127.0.0.1.nip.io",
    "attacker.com",
    "not-localhost"
])
def test_untrusted_hosts_rejected(client, endpoint, payload, untrusted_host):
    """Verify that non-whitelisted hosts are rejected even on port 8080."""
    headers = {"Origin": f"http://{untrusted_host}:8080"}
    res = client.post(endpoint, json=payload, headers=headers)
    assert res.status_code == 403

@pytest.mark.parametrize("bad_scheme", [
    "ftp://127.0.0.1:8080",
    "javascript:alert(1)",
    "file:///C:/test",
    "ws://127.0.0.1:8080"
])
def test_malformed_and_unsupported_schemes_rejected(client, bad_scheme):
    """Verify that unsupported URL schemes and malformed origins are rejected."""
    res = client.post('/api/settings', json={"provider": "gemini"}, headers={"Origin": bad_scheme})
    assert res.status_code == 403

@pytest.mark.parametrize("endpoint,payload", STATE_CHANGING_ENDPOINTS)
def test_sec_fetch_site_cross_site_rejected(client, endpoint, payload):
    """Verify that requests marked cross-site by browser Fetch Metadata are blocked."""
    headers = {
        "Sec-Fetch-Site": "cross-site",
        "Origin": "http://127.0.0.1:8080"  # Spoofed origin but browser tagged cross-site
    }
    res = client.post(endpoint, json=payload, headers=headers)
    assert res.status_code == 403

@pytest.mark.parametrize("endpoint,payload", STATE_CHANGING_ENDPOINTS)
@pytest.mark.parametrize("bad_referer", [
    "https://attacker.com/evil_page.html",
    "http://localhost:3000/app",
    "http://127.0.0.1:9999/test",
    "ftp://localhost:8080/index.html"
])
def test_foreign_or_unexpected_port_referer_rejected(client, endpoint, payload, bad_referer):
    """Verify that foreign or unexpected port Referer is rejected when Origin is absent."""
    headers = {"Referer": bad_referer}
    res = client.post(endpoint, json=payload, headers=headers)
    assert res.status_code == 403

@pytest.mark.parametrize("endpoint,payload", STATE_CHANGING_ENDPOINTS)
def test_valid_referer_accepted_when_origin_absent(client, endpoint, payload):
    """Verify that valid Referer from 127.0.0.1:8080 is accepted when Origin is absent."""
    import os
    os.makedirs(os.path.join(app_module.GENERATED_APPS_DIR, "test_folder"), exist_ok=True)
    headers = {"Referer": "http://127.0.0.1:8080/index.html"}
    res = client.post(endpoint, json=payload, headers=headers)
    assert res.status_code != 403 or "Invalid origin" not in (res.get_json() or {}).get("error", "")

def test_host_header_injection_does_not_bypass_origin_check(client):
    """Verify that spoofing Host: evil.com does NOT authorize Origin: http://evil.com."""
    headers = {
        "Host": "evil.com",
        "Origin": "http://evil.com"
    }
    res = client.post('/api/settings', json={"provider": "gemini"}, headers=headers)
    assert res.status_code == 403

@pytest.mark.parametrize("content_type", [
    "application/x-www-form-urlencoded",
    "multipart/form-data"
])
def test_browser_form_submission_without_origin_rejected(client, content_type):
    """Verify that browser form POSTs without valid origin are rejected."""
    res = client.post('/api/start_task', data="task=evil_task", content_type=content_type)
    assert res.status_code == 403

def test_valid_exact_local_origins_allowed(client):
    """Verify that requests from legitimate exact configured port origins are accepted."""
    for valid_origin in ["http://127.0.0.1:8080", "http://localhost:8080"]:
        res = client.post('/api/settings', json={"provider": "gemini"}, headers={"Origin": valid_origin})
        assert res.status_code == 200
