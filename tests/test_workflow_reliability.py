import pytest
from app import app, workflow_state, state_lock, call_agent

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_missing_api_key_raises_explicit_error(monkeypatch, tmp_path):
    """Verify that calling an agent with no API key raises an explicit error and does not produce mock success."""
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    
    # Temporarily override settings with empty key in isolated temporary file
    import json
    temp_settings = tmp_path / "settings_test.json"
    temp_settings.write_text(json.dumps({"provider": "gemini", "openrouter_key": ""}), encoding="utf-8")
    monkeypatch.setattr("app.SETTINGS_FILE", str(temp_settings))
            
    with pytest.raises(RuntimeError) as exc_info:
        call_agent("specialized/business-strategist.md", "مستشار البزنس", "BS", "blue", "مهمة اختبارية")
    assert "لم يتم العثور على مفتاح API صالح" in str(exc_info.value)

def test_start_task_validation(client):
    """Verify task description validation on POST /api/start_task."""
    # Empty task returns 400
    res = client.post('/api/start_task', json={"task": ""})
    assert res.status_code == 400
    assert "Task description is required" in res.get_json()["error"]

    # Massive payload returns 400
    res = client.post('/api/start_task', json={"task": "a" * 60000})
    assert res.status_code == 400
    assert "exceeds maximum allowed length" in res.get_json()["error"]

def test_concurrent_workflow_rejection(client):
    """Verify that starting a task when one is already running returns 409 Conflict."""
    with state_lock:
        prev_status = workflow_state["status"]
        workflow_state["status"] = "running"
        
    try:
        res = client.post('/api/start_task', json={"task": "مشروع متزامن"})
        assert res.status_code == 409
        assert "already in progress" in res.get_json()["error"]
    finally:
        with state_lock:
            workflow_state["status"] = prev_status

def test_approve_when_not_pending_returns_400(client):
    """Verify that calling /api/approve when status is not approval_needed returns 400."""
    with state_lock:
        prev_status = workflow_state["status"]
        workflow_state["status"] = "idle"
        
    try:
        res = client.post('/api/approve', json={"feedback": "موافقة عشوائية"})
        assert res.status_code == 400
        assert "No pending approval required" in res.get_json()["error"]
    finally:
        with state_lock:
            workflow_state["status"] = prev_status
