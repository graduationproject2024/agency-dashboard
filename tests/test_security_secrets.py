import os
import json
import pytest
from app import app, load_settings, get_safe_settings_view, mask_secret, get_api_key

@pytest.fixture(autouse=True)
def isolate_settings_file(monkeypatch, tmp_path):
    temp_settings = tmp_path / "settings_secrets_test.json"
    seed_data = {
        "provider": "gemini",
        "openrouter_key": "mock_secret_key_1234567890abcdef"
    }
    temp_settings.write_text(json.dumps(seed_data), encoding="utf-8")
    monkeypatch.setattr("app.SETTINGS_FILE", str(temp_settings))

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_mask_secret():
    assert mask_secret("") == ""
    assert mask_secret("mock_secret_key_1234567890abcdef") == "mock...cdef"

def test_get_settings_redacts_secrets(client):
    """Verify that GET /api/settings never returns the plaintext API key."""
    res = client.get('/api/settings')
    assert res.status_code == 200
    data = res.get_json()
    
    # Assert raw secret key is NOT in response
    assert 'openrouter_key' not in data
    assert 'has_key' in data
    assert 'key_masked' in data
    if data['has_key']:
        assert '...' in data['key_masked'] or '***' in data['key_masked']
        # Must not be the raw long key
        assert len(data['key_masked']) < 15

def test_post_settings_allowlist_validation(client):
    """Verify that POST /api/settings accepts only allowlisted fields and rejects malicious/unknown keys."""
    payload = {
        "provider": "gemini",
        "openrouter_model": "gemini-3.5-flash-lite",
        "malicious_field": "injected_value",
        "debug_mode": True
    }
    res = client.post('/api/settings', json=payload)
    assert res.status_code == 200
    
    # Verify settings file does not contain malicious_field
    current = load_settings()
    assert "malicious_field" not in current
    assert "debug_mode" not in current

def test_post_settings_rejects_non_json(client):
    """Verify non-JSON payload returns 400 Bad Request."""
    res = client.post('/api/settings', data="not json", content_type="text/plain")
    assert res.status_code == 400

def test_post_settings_preserves_existing_key_when_masked(client):
    """Verify that submitting a masked placeholder does not overwrite the existing key."""
    current = load_settings()
    existing_key = current.get('openrouter_key', '')
    
    res = client.post('/api/settings', json={
        "provider": "gemini",
        "openrouter_key": "mock...cdef"  # masked preview
    })
    assert res.status_code == 200
    
    updated = load_settings()
    # Key must still equal existing key, not the mask
    assert updated.get('openrouter_key') == existing_key

def test_environment_variable_precedence(monkeypatch):
    """Verify environment variables take priority over settings file."""
    monkeypatch.setenv('GEMINI_API_KEY', 'test_env_key_1234567890')
    key = get_api_key('gemini')
    assert key == 'test_env_key_1234567890'
