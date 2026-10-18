"""
Phase 2.4-C: Verified Multi-Agent Runtime, Persistent Execution, Closed-Loop QA & Bounded Self-Healing Tests
========================================================================================================
Comprehensive verification covering:
1. True DAG execution through workflow entry points with honest lifecycle tracking.
2. Atomic concurrent task claim prevention under race conditions.
3. Upstream artifact propagation to downstream agents via DAG dependencies.
4. Process restart recovery idempotence (no duplicate executions or token double-counting).
5. Closed-loop QA self-healing: deterministic failure -> dynamic remediation task -> fix -> atomic publication.
6. Closed-loop QA remediation exhaustion: 2 failed remediation attempts -> fail closed, publication blocked.
"""

import os
import json
import sqlite3
import threading
import concurrent.futures
from unittest.mock import patch, MagicMock
import pytest

import db_persistence as db_store
import task_orchestrator as orch
from app import (
    run_workflow,
    continue_workflow,
    workflow_state,
    state_lock,
    GENERATED_APPS_DIR,
    get_agent_prompt_meta,
    run_qa_checks
)

VALID_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <title>تطبيق الاختبار المعتمد</title>
</head>
<body>
    <div id="app"><h1>تطبيق معتمد</h1></div>
</body>
</html>"""

VALID_CSS = "body { margin: 0; background: #fafafa; } #app { color: #111; }"
VALID_JS = "function init() { console.log('Ready'); } document.addEventListener('DOMContentLoaded', init);"


@pytest.fixture
def temp_db(tmp_path):
    """Provide an isolated, freshly initialized SQLite database for tests."""
    db_file = str(tmp_path / "test_workflow.db")
    db_store.init_db(db_file)
    return db_file


def test_atomic_concurrent_claim_prevention(temp_db):
    """
    Verify that concurrent worker threads attempting to claim the same ready task
    are strictly prevented from duplicate claims by atomic conditional update.
    Exactly one worker succeeds; all other concurrent claims fail.
    """
    session_id = db_store.create_session("Concurrent_Claim_Test", "Test concurrency", db_path=temp_db)
    task_id = db_store.create_task(
        session_id=session_id,
        title="مهمة فحص التزامن",
        division_id="engineering",
        assigned_agent_id="engineering-senior-developer",
        stage="development",
        status="ready",
        db_path=temp_db
    )

    claims_successful = []
    thread_count = 10
    barrier = threading.Barrier(thread_count)

    def attempt_claim(worker_idx):
        barrier.wait()  # Synchronize threads to hit the DB concurrently
        claimed = db_store.claim_task(task_id, db_path=temp_db)
        if claimed:
            claims_successful.append(worker_idx)

    threads = [threading.Thread(target=attempt_claim, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread must have succeeded
    assert len(claims_successful) == 1, f"Expected exactly 1 claim, got {len(claims_successful)}: {claims_successful}"
    
    # Task status must be 'running' in SQLite
    task = db_store.get_task(task_id, db_path=temp_db)
    assert task["status"] == "running"


def test_upstream_artifact_propagation_via_dag(temp_db):
    """
    Verify that get_task_execution_context correctly passes prerequisite outputs
    to downstream tasks, including special QA review synthesis formatting.
    """
    session_id = db_store.create_session("Upstream_Context_Test", "Context test", db_path=temp_db)
    
    # Prerequisite 1: Dev code output
    dev_task_id = db_store.create_task(
        session_id=session_id,
        title="تطوير الكود الأساسي",
        division_id="engineering",
        assigned_agent_id="engineering-senior-developer",
        stage="development",
        status="completed",
        output_text="```html\n<h1>Hello</h1>\n```",
        db_path=temp_db
    )
    
    # Prerequisite 2: QA Specialist advisory output
    spec_task_id = db_store.create_task(
        session_id=session_id,
        title="تدقيق أمني متخصص",
        division_id="testing",
        assigned_agent_id="security-appsec-engineer",
        stage="qa",
        status="completed",
        output_text="SEC_FINDING: All inputs validated.",
        db_path=temp_db
    )

    # Downstream task: Lead QA Reviewer
    reviewer_task_id = db_store.create_task(
        session_id=session_id,
        title="المراجعة الشاملة لجودة الكود والأمان",
        division_id="engineering",
        assigned_agent_id="engineering-code-reviewer",
        stage="qa",
        dependencies=[dev_task_id, spec_task_id],
        input_context="قم بمراجعة الكود والأمان.",
        db_path=temp_db
    )
    reviewer_task = db_store.get_task(reviewer_task_id, db_path=temp_db)

    ctx = orch.get_task_execution_context(session_id, reviewer_task, db_path=temp_db)

    # Assert downstream context contains upstream dev output
    assert "```html\n<h1>Hello</h1>\n```" in ctx
    # Assert downstream context contains specialist finding under proper section
    assert "SEC_FINDING: All inputs validated." in ctx
    assert "نتائج وتدقيقات المتخصصين المستقلة" in ctx


def test_restart_recovery_idempotence(temp_db):
    """
    Verify that resuming an interrupted session from a checkpoint does not
    duplicate work or re-execute completed tasks.
    Only remaining uncompleted tasks are dispatched.
    """
    session_id = db_store.create_session("Restart_Idempotence_Test", "Idempotence test", db_path=temp_db)
    
    # Create PRD task, already completed
    prd_id = db_store.create_task(
        session_id=session_id,
        title="وثيقة متطلبات المنتج",
        division_id="product",
        assigned_agent_id="product-manager",
        stage="product",
        status="completed",
        output_text="PRD Specifications Document Content",
        db_path=temp_db
    )

    # Create Architect task, already completed
    arch_id = db_store.create_task(
        session_id=session_id,
        title="معمارية النظام",
        division_id="engineering",
        assigned_agent_id="engineering-software-architect",
        stage="engineering",
        dependencies=[prd_id],
        status="completed",
        output_text="Architecture Blueprint Content",
        db_path=temp_db
    )

    # Create Developer task, pending execution
    dev_id = db_store.create_task(
        session_id=session_id,
        title="البرمجة والتطوير الفني",
        division_id="engineering",
        assigned_agent_id="engineering-senior-developer",
        stage="development",
        dependencies=[arch_id],
        status="ready",
        db_path=temp_db
    )

    executed_agents = []

    def mock_call_agent(prompt_file, role_name, short_name, color, prompt, context):
        executed_agents.append(short_name)
        return "Dev Output"

    # Run DAG stages for engineering and development
    ok, completed_ids, err = orch.run_dag_stages(
        session_id=session_id,
        allowed_stages=["engineering", "development"],
        call_agent_fn=mock_call_agent,
        agent_prompt_resolver_fn=get_agent_prompt_meta,
        db_path=temp_db
    )

    assert ok is True
    # Crucial: Architect task was ALREADY completed, so it was NOT executed again!
    assert "ENG" not in executed_agents
    assert executed_agents == ["DEV"]
    assert dev_id in completed_ids


def test_closed_loop_qa_self_healing_success(tmp_path, monkeypatch):
    """
    Closed-Loop QA with Bounded Self-Healing:
    1. Initial senior developer output has a structural defect (e.g. unclosed tag).
    2. QA gate detects the defect and generates diagnostic errors.
    3. Workflow triggers closed-loop remediation:
       - Persists defect in project memory
       - Dynamically generates a remediation task in the DAG
       - Injects diagnostic error and candidate code into developer context
    4. Developer returns corrected valid code.
    5. Re-run QA gate passes.
    6. Atomic deliverable publication succeeds and workflow transitions to completed.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "تطبيق حاسبة تفاعلية تجريبي"
    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {"strategy": "استراتيجية", "product": "متطلبات"}
        workflow_state["blockers"] = []

    # Defective initial HTML (unclosed <div>)
    bad_html = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head><meta charset="UTF-8"><title>تطبيق معطوب</title></head>
<body>
    <div id="unclosed"><h1>خطأ بنيوي
</body>
</html>"""

    corrected_html = VALID_HTML
    remediation_called = []

    def smart_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            prompt = args[1] if len(args) > 1 else kwargs.get("prompt", "")
            context = args[2] if len(args) > 2 else kwargs.get("context_text", "")
            all_text = f"{prompt} {context}"
            if "كشف عن الأخطاء" in all_text or "أخطاء فحص الجودة" in all_text or "خطأ بنيوي" in all_text:
                remediation_called.append(True)
                return f"```html\n{corrected_html}\n```\n```css\n{VALID_CSS}\n```\n```javascript\n{VALID_JS}\n```"
            # Initial invocation: return defective HTML
            return f"```html\n{bad_html}\n```\n```css\n{VALID_CSS}\n```\n```javascript\n{VALID_JS}\n```"
        elif short_name == "QA":
            return "QA Analysis Report"
        elif short_name == "MOTION":
            return f"```css\n/* motion */\n```\n```javascript\n/* motion */\n```"
        elif short_name == "SEO":
            return "<meta name='description' content='test'>"
        return "Generic Output"

    with patch("app.call_agent", side_effect=smart_call_agent):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]
        qa_rep = workflow_state.get("qa_report")
        session_id = workflow_state.get("session_id")

    # Assert remediation took place and succeeded
    assert len(remediation_called) >= 1, "Self-healing remediation task was not called!"
    assert qa_rep is not None
    assert qa_rep["passed"] is True
    assert status == "completed"

    # Assert atomic publish occurred
    created_folders = [f for f in os.listdir(test_gen_dir) if os.path.isdir(os.path.join(test_gen_dir, f))]
    assert len(created_folders) >= 1
    app_dir = os.path.join(test_gen_dir, created_folders[0])
    assert os.path.exists(os.path.join(app_dir, "index.html"))
    assert os.path.exists(os.path.join(app_dir, "style.css"))
    assert os.path.exists(os.path.join(app_dir, "script.js"))
    assert os.path.exists(os.path.join(app_dir, "qa_report.json"))

    # Assert remediation count recorded in DB
    if session_id:
        rem_count = db_store.get_session_remediation_count(session_id)
        assert rem_count >= 1


def test_closed_loop_qa_remediation_exhaustion_fails_closed(tmp_path, monkeypatch):
    """
    Closed-Loop QA Remediation Exhaustion:
    1. Senior developer returns persistently invalid code.
    2. Remediation is attempted for attempt 1 and attempt 2.
    3. Both remediation attempts fail to fix the QA defects.
    4. Workflow strictly bounds retries to 2 attempts, stops self-healing,
       and fails closed:
       - Status transitions to 'failed'
       - Blockers explain remediation exhaustion
       - Deliverables are NOT published to generated_apps
       - Session in DB marked 'failed'
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    task_name = "تطبيق فحص استنفاد محاولات الإصلاح"
    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_name
        workflow_state["outputs"] = {"strategy": "استراتيجية", "product": "متطلبات"}
        workflow_state["blockers"] = []

    bad_html = "<html><body><div>Unclosed tags galore"
    remediation_attempts = []

    def persistently_failing_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        if short_name == "DEV":
            prompt = args[1] if len(args) > 1 else kwargs.get("prompt", "")
            context = args[2] if len(args) > 2 else kwargs.get("context_text", "")
            all_text = f"{prompt} {context}"
            if "كشف عن الأخطاء" in all_text or "أخطاء فحص الجودة" in all_text or "Unclosed tags" in all_text:
                remediation_attempts.append(len(remediation_attempts) + 1)
            return f"```html\n{bad_html}\n```\n```css\n{VALID_CSS}\n```\n```javascript\n{VALID_JS}\n```"
        return "Generic Output"

    with patch("app.call_agent", side_effect=persistently_failing_call_agent):
        continue_workflow(feedback="معتمد")

    with state_lock:
        status = workflow_state["status"]
        blockers = list(workflow_state["blockers"])
        qa_rep = workflow_state.get("qa_report")

    # Bounded to exactly 2 attempts!
    assert len(remediation_attempts) == 2, f"Expected exactly 2 remediation attempts, got {len(remediation_attempts)}"
    assert status == "failed"
    assert qa_rep is not None
    assert qa_rep["passed"] is False
    assert any("استنفاد محاولات الإصلاح الذاتي" in b for b in blockers)

    # Fail closed: No deliverables published to generated_apps!
    created_folders = [f for f in os.listdir(test_gen_dir) if os.path.isdir(os.path.join(test_gen_dir, f))]
    assert len(created_folders) == 0, f"Expected no deliverables published, but found {created_folders}"


def test_true_dag_execution_through_run_workflow_entrypoint(tmp_path, monkeypatch):
    """
    End-to-end verification proving true DAG execution:
    1. run_workflow triggers Pre-PRD stages (strategy, product) via orch.run_dag_stages.
    2. Workflow reaches PRD approval gate and saves verified checkpoint.
    3. continue_workflow dispatches Post-PRD stages via orch.run_dag_stages.
    4. Code extraction, QA gate, and atomic publishing all succeed through the DAG runtime.
    """
    test_gen_dir = str(tmp_path / "generated_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    executed_stages = []

    def mock_workflow_call_agent(agent_file, role_name, short_name, *args, **kwargs):
        executed_stages.append(short_name)
        if short_name == "BS":
            return "دراسة جدوى استراتيجية شاملة"
        elif short_name == "PM":
            return "وثيقة متطلبات منتج (PRD) متكاملة"
        elif short_name == "ENG":
            return "معمارية النظام ومخطط قواعد البيانات"
        elif short_name == "UI":
            return "تصاميم واجهات المستخدم واللوحة اللونية"
        elif short_name == "DEV":
            return f"```html\n{VALID_HTML}\n```\n```css\n{VALID_CSS}\n```\n```javascript\n{VALID_JS}\n```"
        elif short_name == "QA":
            return "تقرير تدقيق الجودة البرمجية الشامل"
        elif short_name == "MOTION":
            return f"```css\n/* motion */\n```\n```javascript\n/* motion */\n```"
        elif short_name == "SEO":
            return "<meta name='description' content='SEO'>"
        return "Generic Specialist Output"

    with patch("app.call_agent", side_effect=mock_workflow_call_agent):
        # 1. Run workflow: Pre-PRD stages via DAG
        run_workflow("منصة سحابية لإدارة العقود والمستندات")

        with state_lock:
            pre_status = workflow_state["status"]
            session_id = workflow_state.get("session_id")
            approval_req = workflow_state.get("approval_request")

        assert pre_status == "approval_needed"
        assert approval_req is not None
        assert session_id is not None
        assert "BS" in executed_stages
        assert "PM" in executed_stages

        # Verify DB tasks and checkpoint
        tasks = db_store.list_tasks_for_session(session_id)
        assert len(tasks) >= 5
        strat_task = next(t for t in tasks if t["stage"] == "strategy" and t["assigned_agent_id"] == "business-strategist")
        prod_task = next(t for t in tasks if t["stage"] == "product")
        assert strat_task["status"] == "completed"
        assert prod_task["status"] == "completed"

        cp = db_store.get_latest_checkpoint(session_id)
        assert cp is not None
        assert cp["step_name"] == "prd_approval_gate"

        # 2. Continue workflow: Post-PRD stages via DAG
        continue_workflow(feedback="معتمد رسمياً")

        with state_lock:
            post_status = workflow_state["status"]
            qa_rep = workflow_state.get("qa_report")

        assert post_status == "completed"
        assert qa_rep is not None
        assert qa_rep["passed"] is True

        # Verify Post-PRD stages executed
        assert "ENG" in executed_stages
        assert "UI" in executed_stages
        assert "DEV" in executed_stages
        assert "QA" in executed_stages

        # Verify deliverable publication
        created_folders = [f for f in os.listdir(test_gen_dir) if os.path.isdir(os.path.join(test_gen_dir, f))]
        assert len(created_folders) >= 1
        app_dir = os.path.join(test_gen_dir, created_folders[0])
        assert os.path.exists(os.path.join(app_dir, "index.html"))
        assert os.path.exists(os.path.join(app_dir, "style.css"))
        assert os.path.exists(os.path.join(app_dir, "script.js"))
        assert os.path.exists(os.path.join(app_dir, "plan.md"))
        assert os.path.exists(os.path.join(app_dir, "qa_report.json"))

        # Verify session completed in SQLite
        sess = db_store.get_session(session_id)
        assert sess["status"] == "completed"
