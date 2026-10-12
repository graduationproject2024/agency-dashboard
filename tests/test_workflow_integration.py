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


def test_persistence_failure_restores_pre_existing_files(tmp_path, mock_agent_responses, monkeypatch):
    """
    Finding E: Verify that when persistence fails midway on an existing directory,
    pre-existing files are restored from backup rather than deleted, while newly
    created files are cleaned up, and the workflow transitions to 'failed'.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "تطبيق الحفاظ على الملفات القديمة"
    app_dir, _ = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)

    # Create pre-existing index.html that must be preserved on rollback
    pre_existing_html_path = os.path.join(app_dir, "index.html")
    original_content = "<!-- PRE-EXISTING FILE VERSION 1.0 -->\n<!DOCTYPE html><html><head><title>Old</title></head><body><h1>Old</h1></body></html>"
    with open(pre_existing_html_path, "w", encoding="utf-8") as f:
        f.write(original_content)

    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    # Simulate failure on qa_report.json (after index.html, style.css, script.js have been staged)
    def mock_write_json(filepath, data):
        if filepath.endswith("qa_report.json"):
            raise IOError("Simulated failure writing qa_report.json")
        from app import atomic_write_json
        return atomic_write_json(filepath, data)

    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("app.atomic_write_json", side_effect=mock_write_json):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]
        blockers = list(workflow_state["blockers"])

    assert status == "failed", f"Expected workflow to fail, got {status}"
    assert any("Simulated failure writing qa_report.json" in b for b in blockers)

    # PRE-EXISTING file must NOT be destroyed; it must be restored to its original content
    assert os.path.exists(pre_existing_html_path), "Pre-existing index.html was deleted by rollback!"
    with open(pre_existing_html_path, "r", encoding="utf-8") as f:
        restored_content = f.read()
    assert restored_content == original_content, "Pre-existing index.html was not restored to original content!"

    # Newly written files should be cleaned up
    assert not os.path.exists(os.path.join(app_dir, "style.css")), "Newly created style.css was not rolled back!"
    assert not os.path.exists(os.path.join(app_dir, "script.js")), "Newly created script.js was not rolled back!"
    assert not os.path.exists(os.path.join(app_dir, "plan.md")), "Newly created plan.md was not rolled back!"
    assert not os.path.exists(os.path.join(app_dir, "qa_report.json")), "qa_report.json unexpectedly exists!"

    # Verify no dangling .bak files remained
    bak_files = [f for f in os.listdir(app_dir) if f.endswith(".bak")]
    assert len(bak_files) == 0, f"Dangling backup files left behind: {bak_files}"


def test_persisted_html_identity_mismatch_triggers_rollback(tmp_path, mock_agent_responses, monkeypatch):
    """
    Finding A & E: Verify that if the persisted HTML file on disk differs from the
    exact candidate that passed QA, identity verification raises an error, triggers
    safe rollback, and fails the workflow.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "تطبيق فحص تطابق الهوية"
    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    from app import atomic_write_file
    real_atomic_write = atomic_write_file

    def tampered_atomic_write(filepath, content, encoding="utf-8"):
        # Corrupt the HTML file when writing index.html to simulate mutation post-QA
        if filepath.endswith("index.html"):
            content = content + "\n<!-- unauthorized post-QA mutation -->"
        return real_atomic_write(filepath, content, encoding)

    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("app.atomic_write_file", side_effect=tampered_atomic_write):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]
        blockers = list(workflow_state["blockers"])

    assert status == "failed"
    assert any("does not match the QA-validated candidate" in b for b in blockers)


def test_workflow_validates_and_persists_html_with_injected_assets(tmp_path, mock_agent_responses, monkeypatch):
    """
    Finding A: Proves that in a full successful workflow run:
    1. The candidate validated by run_qa_checks already includes injected asset links.
    2. The exact validated candidate is written to disk.
    3. The persisted index.html contains both style.css and script.js references.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "تطبيق التحقق من تسلسل حقن الروابط"
    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    validated_html_capture = []

    from app import run_qa_checks as real_run_qa
    def captured_run_qa(html, css, js):
        validated_html_capture.append(html)
        return real_run_qa(html, css, js)

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("app.run_qa_checks", side_effect=captured_run_qa):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]

    assert status == "completed"
    assert len(validated_html_capture) == 1
    validated_html = validated_html_capture[0]

    # QA MUST validate the HTML with asset links already injected!
    assert '<link rel="stylesheet" href="style.css">' in validated_html
    assert '<script src="script.js"></script>' in validated_html

    # Persisted HTML MUST match the validated HTML exactly
    created_folders = [f for f in os.listdir(test_gen_dir) if os.path.isdir(os.path.join(test_gen_dir, f))]
    assert len(created_folders) >= 1
    app_dir = os.path.join(test_gen_dir, created_folders[0])
    persisted_path = os.path.join(app_dir, "index.html")
    assert os.path.exists(persisted_path)
    with open(persisted_path, "r", encoding="utf-8") as f:
        persisted_content = f.read()

    assert persisted_content == validated_html
