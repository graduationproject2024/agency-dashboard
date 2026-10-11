import os
import pytest
from app import app, safe_resolve_project_dir, GENERATED_APPS_DIR

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_sensitive_files_blocked_from_static_serving(client):
    """Verify that private server files cannot be downloaded via direct URL requests."""
    forbidden_files = [
        '/settings.json',
        '/app.py',
        '/budget.json',
        '/.env',
        '/.git',
        '/final_product.md'
    ]
    for path in forbidden_files:
        res = client.get(path)
        assert res.status_code in [403, 404], f"File {path} was not blocked! Status: {res.status_code}"

def test_legitimate_frontend_assets_accessible(client):
    """Verify that index.html, style.css, and script.js are properly served."""
    assert client.get('/').status_code == 200
    assert client.get('/style.css').status_code == 200
    assert client.get('/script.js').status_code == 200

def test_safe_resolve_project_dir_prevents_traversal():
    """Verify safe_resolve_project_dir rejects directory traversal attempts."""
    assert safe_resolve_project_dir(None) is None
    assert safe_resolve_project_dir("") is None
    assert safe_resolve_project_dir("../") is None
    assert safe_resolve_project_dir("../../Windows") is None
    assert safe_resolve_project_dir("..\\..\\Windows") is None
    assert safe_resolve_project_dir("project/subfolder") is None
    assert safe_resolve_project_dir("project\\subfolder") is None
    
    # Valid folder names return absolute path inside GENERATED_APPS_DIR
    resolved = safe_resolve_project_dir("Industrial_Predictive_Maintenance_Platform")
    assert resolved is not None
    assert resolved.startswith(GENERATED_APPS_DIR)

def test_open_folder_blocks_traversal_and_system_paths(client):
    """Verify POST /api/open_folder strictly rejects traversal and arbitrary host paths."""
    # Traversal by name
    res = client.post('/api/open_folder', json={"name": "../../Windows"})
    assert res.status_code == 403

    # Arbitrary absolute path
    res = client.post('/api/open_folder', json={"path": "C:\\Windows\\System32"})
    assert res.status_code == 403

    # Non-existent or empty folder
    res = client.post('/api/open_folder', json={"name": "non_existent_random_xyz"})
    assert res.status_code == 403

def test_serve_project_app_blocks_forbidden_extensions(client):
    """Verify /apps/<name>/<path> denies access to private files like .py or .env."""
    res = client.get('/apps/Industrial_Predictive_Maintenance_Platform/config.py')
    assert res.status_code in [403, 404]

    res = client.get('/apps/Industrial_Predictive_Maintenance_Platform/.env')
    assert res.status_code in [403, 404]

def test_error_handlers_do_not_leak_stack_traces(client):
    """Verify error responses return clean JSON and do not expose internal exceptions."""
    res = client.get('/non_existent_page_12345')
    assert res.status_code == 404
    data = res.get_json()
    assert "error" in data
    assert "Traceback" not in str(res.data)
