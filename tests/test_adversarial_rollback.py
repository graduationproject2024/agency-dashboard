import os
import json
import pytest
from unittest.mock import patch

from app import (
    continue_workflow,
    workflow_state,
    state_lock,
    GENERATED_APPS_DIR,
    get_project_folder_name,
    atomic_write_file,
)


@pytest.fixture
def mock_agent_responses():
    """Deterministic mock responses for complete workflow execution."""
    valid_html = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <title>تطبيق اختبار الاسترجاع العدائي</title>
</head>
<body>
    <div id="app"><h1>اختبار الموثوقية</h1></div>
</body>
</html>"""
    valid_css = "body { margin: 0; padding: 0; background: #f9f9f9; } #app { color: #111; }"
    valid_js = "function init() { console.log('Ready'); } document.addEventListener('DOMContentLoaded', init);"

    return {
        "engineering": "```markdown\n## Arch\nDetails\n```",
        "design": "```markdown\n## UI/UX\nDesign spec\n```",
        "development": f"```html\n{valid_html}\n```\n```css\n{valid_css}\n```\n```javascript\n{valid_js}\n```",
        "animation": "```javascript\n/* Animation */\nfunction animate() {}\n```",
        "seo": "```html\n<meta name=\"description\" content=\"Test\">\n```",
        "qa": "```markdown\n## QA\nApproved\n```",
    }


def test_write_fails_before_changing_destination(tmp_path, mock_agent_responses, monkeypatch):
    """
    Scenario 1: A write operation fails before modifying the destination.
    Proves:
    - workflow fails honestly.
    - pre-existing file content is untouched.
    - failed destination file is not created.
    - workflow state is 'failed' and never 'completed'.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "فشل الكتابة قبل تعديل الوجهة"
    app_dir, _ = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)

    # Pre-existing file
    pre_existing_path = os.path.join(app_dir, "index.html")
    original_bytes = b"<!-- ORIGINAL UNTOUCHED CONTENT -->"
    with open(pre_existing_path, "wb") as f:
        f.write(original_bytes)

    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    # Simulate write failure before destination is changed
    real_atomic_write = atomic_write_file

    def failing_atomic_write(filepath, content, encoding="utf-8"):
        if filepath.endswith("style.css"):
            raise IOError("Simulated disk failure before destination change")
        return real_atomic_write(filepath, content, encoding)

    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("app.get_project_folder_name", return_value=(app_dir, "clean")), \
         patch("app.atomic_write_file", side_effect=failing_atomic_write):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]
        blockers = list(workflow_state["blockers"])

    assert status == "failed", f"Expected failed status, got: {status}"
    assert any("Simulated disk failure before destination change" in b for b in blockers)

    # Pre-existing file must retain exact original bytes
    with open(pre_existing_path, "rb") as f:
        assert f.read() == original_bytes

    # Newly staged destination that failed must not exist
    assert not os.path.exists(os.path.join(app_dir, "style.css"))


def test_write_replaces_destination_then_raises_before_bookkeeping(tmp_path, mock_agent_responses, monkeypatch):
    """
    Scenario 2: A write operation replaces the destination on disk and then raises
    an exception before normal bookkeeping completes.
    Proves:
    - rollback safely detects the modified file.
    - pre-existing file is restored to its exact original bytes.
    - workflow status transitions to 'failed'.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "فشل بعد استبدال الوجهة مباشرة"
    app_dir, _ = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)

    pre_existing_path = os.path.join(app_dir, "index.html")
    original_bytes = b"<!-- PRE-EXISTING ORIGINAL BYTES -->"
    with open(pre_existing_path, "wb") as f:
        f.write(original_bytes)

    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    real_atomic_write = atomic_write_file

    def write_then_crash(filepath, content, encoding="utf-8"):
        # Successfully replace the destination file
        real_atomic_write(filepath, content, encoding)
        # Crash immediately on index.html after disk replacement
        if filepath.endswith("index.html"):
            raise RuntimeError("Crash immediately after disk replacement before bookkeeping")

    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("app.get_project_folder_name", return_value=(app_dir, "clean")), \
         patch("app.atomic_write_file", side_effect=write_then_crash):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]
        blockers = list(workflow_state["blockers"])

    assert status == "failed"
    assert any("Crash immediately after disk replacement" in b for b in blockers)

    # Rollback must restore original bytes even though crash occurred immediately after write
    with open(pre_existing_path, "rb") as f:
        assert f.read() == original_bytes


def test_failure_after_several_deliverables_written(tmp_path, mock_agent_responses, monkeypatch):
    """
    Scenario 3: Failure occurs after multiple deliverables (HTML, CSS, JS) have been written.
    Proves:
    - rollback cleans up all earlier written deliverables.
    - no partial deliverables remain.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "فشل بعد كتابة مخرجات متعددة"
    app_dir, _ = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)

    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    real_atomic_write = atomic_write_file

    def fail_on_plan(filepath, content, encoding="utf-8"):
        if filepath.endswith("plan.md"):
            raise IOError("Simulated failure at plan.md")
        return real_atomic_write(filepath, content, encoding)

    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("app.get_project_folder_name", return_value=(app_dir, "clean")), \
         patch("app.atomic_write_file", side_effect=fail_on_plan):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]

    assert status == "failed"
    # All staged files must be removed
    assert not os.path.exists(os.path.join(app_dir, "index.html"))
    assert not os.path.exists(os.path.join(app_dir, "style.css"))
    assert not os.path.exists(os.path.join(app_dir, "css", "style.css"))
    assert not os.path.exists(os.path.join(app_dir, "script.js"))
    assert not os.path.exists(os.path.join(app_dir, "js", "script.js"))
    assert not os.path.exists(os.path.join(app_dir, "plan.md"))


def test_restoring_pre_existing_file_fails_preserves_backup_and_surfaces_path(tmp_path, mock_agent_responses, monkeypatch):
    """
    Scenario 4 & 8: Restoring a pre-existing file fails during rollback.
    Proves:
    - CRITICAL INVARIANT: The backup file is NOT deleted!
    - The retained recovery backup file still contains the original data.
    - The error surfaces the exact retained backup path and affected file.
    - Workflow fails honestly.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "فشل استرجاع النسخة الاحتياطية وحفظها"
    app_dir, _ = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)

    pre_existing_path = os.path.join(app_dir, "index.html")
    original_bytes = b"<!-- PRECIOUS USER DATA THAT MUST NEVER BE DESTROYED -->"
    with open(pre_existing_path, "wb") as f:
        f.write(original_bytes)

    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    real_atomic_write = atomic_write_file
    real_os_replace = os.replace

    def fail_on_plan(filepath, content, encoding="utf-8"):
        if filepath.endswith("plan.md"):
            raise IOError("Simulated failure at plan.md")
        return real_atomic_write(filepath, content, encoding)

    def failing_os_replace(src, dst):
        # Fail when restoring the pre-existing index.html from its backup
        if src.endswith(".bak") and dst.endswith("index.html"):
            raise PermissionError("Access denied restoring pre-existing index.html")
        return real_os_replace(src, dst)

    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("app.get_project_folder_name", return_value=(app_dir, "clean")), \
         patch("app.atomic_write_file", side_effect=fail_on_plan), \
         patch("os.replace", side_effect=failing_os_replace):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]
        blockers = list(workflow_state["blockers"])

    assert status == "failed"

    # Must explicitly surface restoration failure and retained recovery backup path
    assert any("retained recovery backup at" in b for b in blockers)
    assert any("Failed to rollback index.html" in b for b in blockers)

    # CRITICAL: Find the backup file on disk and verify it WAS NOT DELETED
    bak_files = [f for f in os.listdir(app_dir) if f.startswith("index.html.pre_") and f.endswith(".bak")]
    assert len(bak_files) == 1, f"Expected retained backup file, found: {bak_files}"

    retained_bak_path = os.path.join(app_dir, bak_files[0])
    assert os.path.exists(retained_bak_path)

    # Verify that the backup file contains the exact original bytes
    with open(retained_bak_path, "rb") as f:
        assert f.read() == original_bytes


def test_backup_cleanup_fails_after_rollback_surfaces_error(tmp_path, mock_agent_responses, monkeypatch):
    """
    Scenario 5: Backup cleanup fails during rollback.
    Proves:
    - Cleanup failure is reported and not silently swallowed.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "فشل تنظيف النسخة الاحتياطية بعد الاسترجاع"
    app_dir, _ = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)

    pre_existing_path = os.path.join(app_dir, "index.html")
    with open(pre_existing_path, "wb") as f:
        f.write(b"<!-- pre-existing -->")

    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    from app import atomic_write_file, cleanup_backups as real_cleanup
    real_atomic_write = atomic_write_file

    def fail_on_plan(filepath, content, encoding="utf-8"):
        if filepath.endswith("plan.md"):
            raise IOError("Simulated failure at plan.md")
        return real_atomic_write(filepath, content, encoding)

    def failing_cleanup(backups_dict, failed_restorations=None):
        errs = real_cleanup(backups_dict, failed_restorations)
        errs.append("Failed to clean up obsolete backup index.html.pre_abc123.bak: Locked by system")
        return errs

    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("app.get_project_folder_name", return_value=(app_dir, "clean")), \
         patch("app.atomic_write_file", side_effect=fail_on_plan), \
         patch("app.cleanup_backups", side_effect=failing_cleanup):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]
        blockers = list(workflow_state["blockers"])

    assert status == "failed"
    # The cleanup error must be captured in blockers rather than swallowed
    assert any("Failed to clean up obsolete backup" in b for b in blockers)


def test_cleanup_backups_standalone_preserves_failed_restorations(tmp_path):
    """
    Dedicated test for cleanup_backups:
    Proves that a backup marked as a failed restoration is NEVER deleted,
    while obsolete backups are safely deleted.
    """
    from app import cleanup_backups

    file_a = str(tmp_path / "a.html")
    bak_a = str(tmp_path / "a.html.pre_123.bak")
    with open(bak_a, "wb") as f:
        f.write(b"BACKUP A")

    file_b = str(tmp_path / "b.css")
    bak_b = str(tmp_path / "b.css.pre_456.bak")
    with open(bak_b, "wb") as f:
        f.write(b"BACKUP B")

    backups_dict = {
        file_a: bak_a,
        file_b: bak_b,
    }
    failed_restorations = {
        file_a: (bak_a, "PermissionError"),
    }

    errors = cleanup_backups(backups_dict, failed_restorations)

    assert len(errors) == 0
    # bak_a MUST STILL EXIST because restoration failed!
    assert os.path.exists(bak_a), "CRITICAL: Backup A was deleted despite failed restoration!"
    with open(bak_a, "rb") as f:
        assert f.read() == b"BACKUP A"

    # bak_b must be cleaned up because it was not in failed_restorations
    assert not os.path.exists(bak_b), "Obsolete Backup B was not cleaned up!"


def test_cleanup_backups_standalone_captures_removal_errors(tmp_path, monkeypatch):
    """
    Dedicated test for cleanup_backups:
    Proves that an OSError during os.remove is captured in returned errors and not swallowed.
    """
    from app import cleanup_backups

    file_a = str(tmp_path / "test.html")
    bak_a = str(tmp_path / "test.html.pre_999.bak")
    with open(bak_a, "wb") as f:
        f.write(b"TEST")

    def failing_remove(path):
        raise PermissionError("Access denied deleting backup")

    monkeypatch.setattr("os.remove", failing_remove)

    errors = cleanup_backups({file_a: bak_a})
    assert len(errors) == 1
    assert "Failed to clean up obsolete backup" in errors[0]
    assert "Access denied" in errors[0]


def test_multiple_pre_existing_files_restored_to_exact_bytes(tmp_path, mock_agent_responses, monkeypatch):
    """
    Scenario 6: Multiple files pre-existed before workflow execution.
    Proves:
    - All pre-existing files are restored to their exact initial byte content.
    - Newly created deliverables are removed.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "استرجاع ملفات متعددة إلى محتواها الدقيق"
    app_dir, _ = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)

    original_html = b"<!DOCTYPE html><html><head><title>Old V1</title></head></html>"
    original_css = b"body { background: red; }"
    original_js = b"console.log('old v1');"

    with open(os.path.join(app_dir, "index.html"), "wb") as f:
        f.write(original_html)
    with open(os.path.join(app_dir, "style.css"), "wb") as f:
        f.write(original_css)
    with open(os.path.join(app_dir, "script.js"), "wb") as f:
        f.write(original_js)

    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    real_atomic_write = atomic_write_file

    def fail_on_plan(filepath, content, encoding="utf-8"):
        if filepath.endswith("plan.md"):
            raise IOError("Simulated disk failure at plan.md")
        return real_atomic_write(filepath, content, encoding)

    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("app.get_project_folder_name", return_value=(app_dir, "clean")), \
         patch("app.atomic_write_file", side_effect=fail_on_plan):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]

    assert status == "failed"

    # Check that ALL three pre-existing files were restored to exact bytes
    with open(os.path.join(app_dir, "index.html"), "rb") as f:
        assert f.read() == original_html
    with open(os.path.join(app_dir, "style.css"), "rb") as f:
        assert f.read() == original_css
    with open(os.path.join(app_dir, "script.js"), "rb") as f:
        assert f.read() == original_js

    # Verify newly created files (like plan.md or duplicate css) do not linger
    assert not os.path.exists(os.path.join(app_dir, "plan.md"))


def test_newly_created_files_removed_when_rollback_succeeds(tmp_path, mock_agent_responses, monkeypatch):
    """
    Scenario 7: In a fresh run with no pre-existing files, a late write failure cleanly removes all files.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "تنظيف الملفات الجديدة كليا"
    app_dir, _ = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)

    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    real_atomic_write = atomic_write_file

    def fail_on_duplicate_js(filepath, content, encoding="utf-8"):
        norm = os.path.normpath(filepath)
        if norm.endswith(os.path.normpath("js/script.js")):
            raise IOError("Simulated disk error writing duplicate JS")
        return real_atomic_write(filepath, content, encoding)

    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("app.get_project_folder_name", return_value=(app_dir, "clean")), \
         patch("app.atomic_write_file", side_effect=fail_on_duplicate_js):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]

    assert status == "failed"

    # All files staged prior to failure must be removed
    assert not os.path.exists(os.path.join(app_dir, "index.html"))
    assert not os.path.exists(os.path.join(app_dir, "style.css"))
    assert not os.path.exists(os.path.join(app_dir, "css", "style.css"))
    assert not os.path.exists(os.path.join(app_dir, "script.js"))


def test_no_unintended_modifications_outside_test_directory(tmp_path, mock_agent_responses, monkeypatch):
    """
    Scenario 10: Proves that rollback operations never touch or delete files
    outside the designated project directory.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    # Create sibling project and parent control file
    sibling_dir = os.path.join(test_gen_dir, "sibling_project")
    os.makedirs(sibling_dir, exist_ok=True)
    sibling_file = os.path.join(sibling_dir, "important_data.txt")
    with open(sibling_file, "w", encoding="utf-8") as f:
        f.write("DO NOT DELETE")

    parent_control_file = str(tmp_path / "parent_control.txt")
    with open(parent_control_file, "w", encoding="utf-8") as f:
        f.write("PARENT UNTOUCHED")

    task_name = "حماية الملفات الخارجية من الحذف العرضي"
    app_dir, _ = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)

    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {"strategy": "test", "product": "test"}
        workflow_state["blockers"] = []

    def fake_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            return mock_agent_responses["development"]
        return "مخرج تجريبي"

    real_atomic_write = atomic_write_file

    def fail_at_end(filepath, content, encoding="utf-8"):
        if filepath.endswith("plan.md"):
            raise IOError("Simulated failure at plan.md")
        return real_atomic_write(filepath, content, encoding)

    with patch("app.call_agent", side_effect=fake_call_agent), \
         patch("app.get_project_folder_name", return_value=(app_dir, "clean")), \
         patch("app.atomic_write_file", side_effect=fail_at_end):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]

    assert status == "failed"

    # Assert external files were completely untouched
    assert os.path.exists(sibling_file)
    with open(sibling_file, "r", encoding="utf-8") as f:
        assert f.read() == "DO NOT DELETE"

    assert os.path.exists(parent_control_file)
    with open(parent_control_file, "r", encoding="utf-8") as f:
        assert f.read() == "PARENT UNTOUCHED"
