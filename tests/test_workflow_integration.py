import os
import json
import pytest
from unittest.mock import patch

from app import (
    continue_workflow,
    workflow_state,
    state_lock,
    GENERATED_APPS_DIR,
    get_project_folder_name
)

@pytest.fixture
def mock_agent_responses():
    """Mock agent outputs so continue_workflow runs through completion without external APIs."""
    valid_html = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <title>تطبيق الاختبار</title>
</head>
<body>
    <div id="app"><h1>مرحبا بالعالم</h1></div>
</body>
</html>"""
    valid_css = "body { margin: 0; padding: 0; background: #fff; } #app { color: #333; }"
    valid_js = "function init() { console.log('Ready'); } document.addEventListener('DOMContentLoaded', init);"
    
    mock_dict = {
        "engineering": "```markdown\n## Arch\nDetails\n```",
        "design": "```markdown\n## UI/UX\nDesign spec\n```",
        "development": f"```html\n{valid_html}\n```\n```css\n{valid_css}\n```\n```javascript\n{valid_js}\n```",
        "animation": "```javascript\n/* Animation */\nfunction animate() {}\n```",
        "seo": "```html\n<meta name=\"description\" content=\"Test\">\n```",
        "qa": "```markdown\n## QA\nApproved\n```"
    }
    return mock_dict

def test_workflow_completion_produces_artifacts_and_completed_status(tmp_path, mock_agent_responses, monkeypatch):
    """
    Exercise the workflow from approval through QA gate and artifact persistence.
    Verifies that a successful workflow produces index.html, style.css, script.js, plan.md,
    qa_report.json, and transitions workflow_state['status'] to 'completed'.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)
    
    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = "تطبيق متجر إلكتروني تجريبي"
        workflow_state["outputs"] = {
            "strategy": "استراتيجية واضحة ومحددة",
            "product": "وثيقة متطلبات كاملة",
            "engineering": "معمارية برمجية",
            "design": "تصاميم واجهات"
        }
        workflow_state["blockers"] = []
        workflow_state["qa_report"] = None

    def fake_call_agent(agent_file, role_name, short_name, color, prompt, context_text=""):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        elif short_name == "QA":
            return mock_agent_responses["qa"]
        elif short_name == "MOTION":
            return mock_agent_responses["animation"]
        elif short_name == "SEO":
            return mock_agent_responses["seo"]
        return "مخرج تجريبي"

    with patch("app.call_agent", side_effect=fake_call_agent):
        continue_workflow(feedback="معتمد بالكامل")

    with state_lock:
        status = workflow_state["status"]
        blockers = list(workflow_state["blockers"])
        qa_rep = workflow_state.get("qa_report")

    # Assert workflow successfully completed without crashing on undefined variables
    assert status == "completed", f"Workflow failed with status '{status}' and blockers: {blockers}"
    assert qa_rep is not None
    assert qa_rep["passed"] is True

    # Inspect created folder in generated_apps
    created_folders = [f for f in os.listdir(test_gen_dir) if os.path.isdir(os.path.join(test_gen_dir, f))]
    assert len(created_folders) >= 1, "No project folder was created in generated_apps!"
    app_dir = os.path.join(test_gen_dir, created_folders[0])
    
    plan_path = os.path.join(app_dir, "plan.md")
    assert os.path.exists(plan_path), "plan.md was not created!"
    with open(plan_path, "r", encoding="utf-8") as f:
        plan_content = f.read()
    assert "تقرير فحص الجودة (QA Report)" in plan_content
    assert "checks" in plan_content

    qa_report_file = os.path.join(app_dir, "qa_report.json")
    assert os.path.exists(qa_report_file), "qa_report.json was not persisted!"
    with open(qa_report_file, "r", encoding="utf-8") as f:
        saved_qa = json.load(f)
    assert saved_qa["passed"] is True
    assert "checks" in saved_qa

    assert os.path.exists(os.path.join(app_dir, "index.html"))
    assert os.path.exists(os.path.join(app_dir, "style.css"))
    assert os.path.exists(os.path.join(app_dir, "script.js"))

def test_failed_qa_prevents_completed_status_and_deliverables(tmp_path, monkeypatch):
    """
    Verify that when QA fails (e.g. malformed HTML / JS syntax error),
    workflow transitions to 'failed', records blocker, and does NOT write plan.md.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)
    
    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = "تطبيق فاشل في فحص الجودة"
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []
        workflow_state["qa_report"] = None

    # Broken HTML and invalid JS syntax
    broken_dev = "```html\n<div>Unclosed without body\n```\n```css\nbody { color: red; }\n```\n```javascript\nconst x = ;\n```"

    def fake_call_agent(agent_file, role_name, short_name, color, prompt, context_text=""):
        if short_name == "DEV":
            return broken_dev
        return "مخرج تجريبي"

    with patch("app.call_agent", side_effect=fake_call_agent):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]
        blockers = list(workflow_state["blockers"])
        qa_rep = workflow_state.get("qa_report")

    assert status == "failed", f"Expected workflow to fail QA, got {status}"
    assert qa_rep is not None
    assert qa_rep["passed"] is False
    assert len(blockers) >= 1
    assert any("فحص الجودة" in b for b in blockers)

    # Assert no deliverables were written
    created_folders = [f for f in os.listdir(test_gen_dir) if os.path.isdir(os.path.join(test_gen_dir, f))]
    assert len(created_folders) == 0, "Deliverables were created despite QA failure!"

def test_persistence_exception_results_in_honest_failed_state(tmp_path, mock_agent_responses, monkeypatch):
    """
    Verify that if a filesystem error occurs during file writing,
    the workflow honestly records failure instead of falsely reporting success.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = "تطبيق فحص استثناء الحفظ"
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    # Simulate an OS error during os.makedirs
    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("os.makedirs", side_effect=OSError("Read-only filesystem simulated")):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]
        blockers = list(workflow_state["blockers"])

    assert status == "failed"
    assert any("Read-only filesystem simulated" in b for b in blockers)

def test_persistence_failure_at_plan_md_cleans_up_and_fails_honestly(tmp_path, mock_agent_responses, monkeypatch):
    """
    Finding 4: Verify that if atomic write fails at an intermediate deliverable (plan.md),
    all previously written files (index.html, style.css, script.js) are cleanly rolled back,
    no partial deliverable remains, and the workflow transitions to 'failed'.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = "تطبيق فحص التراجع عند فشل التوثيق"
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    from app import atomic_write_file
    real_atomic_write_file = atomic_write_file

    def mock_write_file(filepath, content, encoding="utf-8"):
        if filepath.endswith("plan.md"):
            raise IOError("Simulated disk error while persisting plan.md")
        return real_atomic_write_file(filepath, content, encoding)

    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("app.atomic_write_file", side_effect=mock_write_file):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]
        blockers = list(workflow_state["blockers"])

    assert status == "failed"
    assert any("Simulated disk error while persisting plan.md" in b for b in blockers)

    # Check project directory: ensure partial index.html, style.css were cleaned up
    created_folders = [f for f in os.listdir(test_gen_dir) if os.path.isdir(os.path.join(test_gen_dir, f))]
    if created_folders:
        app_dir = os.path.join(test_gen_dir, created_folders[0])
        assert not os.path.exists(os.path.join(app_dir, "index.html")), "index.html was not cleaned up after persistence failure!"
        assert not os.path.exists(os.path.join(app_dir, "style.css")), "style.css was not cleaned up after persistence failure!"
        assert not os.path.exists(os.path.join(app_dir, "plan.md")), "plan.md was unexpectedly found!"

def test_persistence_failure_at_qa_report_json_cleans_up_and_fails_honestly(tmp_path, mock_agent_responses, monkeypatch):
    """
    Finding 4: Verify that if atomic write fails at the final audit report (qa_report.json),
    the workflow cleanly rolls back all deliverables and transitions to 'failed'.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = "تطبيق فحص فشل تقرير الجودة"
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    def mock_write_json(filepath, data):
        if filepath.endswith("qa_report.json"):
            raise OSError("Permission denied simulated for qa_report.json")
        from app import atomic_write_json
        return atomic_write_json(filepath, data)

    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("app.atomic_write_json", side_effect=mock_write_json):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]
        blockers = list(workflow_state["blockers"])

    assert status == "failed"
    assert any("Permission denied simulated for qa_report.json" in b for b in blockers)

    created_folders = [f for f in os.listdir(test_gen_dir) if os.path.isdir(os.path.join(test_gen_dir, f))]
    if created_folders:
        app_dir = os.path.join(test_gen_dir, created_folders[0])
        assert not os.path.exists(os.path.join(app_dir, "index.html")), "index.html was not cleaned up after json write failure!"
        assert not os.path.exists(os.path.join(app_dir, "qa_report.json")), "qa_report.json was unexpectedly found!"
