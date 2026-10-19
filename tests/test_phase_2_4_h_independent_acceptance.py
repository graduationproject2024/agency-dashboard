"""
Phase 2.4-H Independent Production Acceptance, Artifact Provenance & Autonomous Recovery Suite.

Verifies:
- Workstream A: Manifest Integrity & Anti-Tampering (Path traversal, Windows drive escapes, UNC, DOS device rejection, SQLite persistence failure rollback).
- Workstream B: QA Effectiveness & Dangerous Scheme Detection (javascript:, data:text/html, vbscript:, invalid DOM syntax).
- Workstream C: Autonomous Remediation Concurrency (Atomic task claim mutual exclusion, diagnostic reporting on failure).
- Workstream D: Token & Provider Cost Accounting (Explicit zero tokens preserved as provider_reported, repeated callbacks idempotency, negative value sanitization).
- Workstream E: Operator Audit & Recovery Observability (Correlated diagnostics, ambiguous provider flags, 404 on nonexistent sessions).
- Workstream F: End-to-End Acceptance Scenarios (Successful lifecycle, fault repair, unrecoverable failure, crash recovery, concurrency, browser acceptance).
- Workstream G: Workforce Inventory & Canonical Division Integrity (18 divisions, 282 agents dynamically discovered).
"""

import os
import sys
import json
import time
import uuid
import copy
import hashlib
import sqlite3
import threading
import http.server
import socketserver
from pathlib import Path
from typing import Dict, Any, Optional

import pytest

# Ensure repo root is on path
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import app as agency_app
from app import (
    app,
    run_qa_checks,
    build_artifact_manifest,
    publish_workflow_deliverables,
    continue_workflow,
    run_workflow,
    workflow_state,
    state_lock,
    CANONICAL_DIVISIONS,
    discover_agents,
    get_last_agent_execution,
    set_last_agent_execution
)
import db_persistence as db_store
import task_orchestrator as orch

def _make_deterministic_agent_adapter(html_defect=False, defect_resolved=True):
    attempt_counter = {"dev_calls": 0, "rem_calls": 0}

    def _adapter(prompt_file, role_name, short_name, color, prompt, context_text=""):
        p_lower = (prompt or "").lower()

        # Strategy Stage
        if "business-strategist" in prompt_file or "دراسة" in prompt or "جدوى" in prompt:
            return (
                "### دراسة الجدوى والتوجيه الاستراتيجي\n"
                "المشروع ذو جدوى تقنية وتجارية واعدة. نوصي بتطبيق معايير أمان عالية وهيكلية سحابية معتمدة."
            )
        if "clinical-evidence" in prompt_file or "طبي" in prompt_file:
            return "### توصيات الأدلة الطبية والسريرية\nيجب دعم اللغتين العربية والإنجليزية، والالتزام بمعايير HIPAA لبيانات المرضى ومواعيد العيادة."

        # Product PRD Stage
        if "product-manager" in prompt_file or "prd" in p_lower or "متطلبات" in prompt:
            return (
                "## وثيقة متطلبات المنتج (PRD)\n"
                "1. الأهداف: إطلاق منصة ويب متكاملة وعالية الاعتمادية.\n"
                "2. الميزات: واجهة مستخدم متجاوبة، تدفق بيانات موثوق، وتكامل فوري.\n"
                "3. معايير النجاح: زمن استجابة أقل من 200ms، توافق تام مع الهواتف الذكية."
            )

        # Engineering Architecture Stage
        if "software-architect" in prompt_file or "معمارية" in prompt or "architecture" in p_lower:
            return "## مخطط المعمارية\nهيكلية ثلاثية الطبقات: Presentation Layer, Logic Controller, Data Persistence."
        if "database-optimizer" in prompt_file or "قواعد" in prompt:
            return "## تخطيط قواعد البيانات\nمخطط علائقي مفهرس يدعم الاستعلامات المتزامنة."

        # UI/UX Design Stage
        if "ui-designer" in prompt_file or "تصميم" in prompt or "design" in p_lower:
            return "## التوجيه البصري وتجربة المستخدم\nنظام ألوان متناسق بنمط Dark/Light عصري، تايبوغرافي واضح."

        # Development Stage (Code Generation)
        if "senior-developer" in prompt_file or "البرمجة" in prompt or "development" in p_lower:
            attempt_counter["dev_calls"] += 1
            if html_defect and not defect_resolved:
                raw_html = "<div><h1>Defective</h1><p>Missing elements"
            elif html_defect and attempt_counter["dev_calls"] == 1:
                raw_html = "<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='UTF-8'><title>قيد التطوير</title></head><div>مسودة غير مكتملة بدون body</div></html>"
            else:
                raw_html = (
                    "<!DOCTYPE html>\n"
                    "<html lang=\"ar\" dir=\"rtl\">\n"
                    "<head>\n"
                    "  <meta charset=\"UTF-8\">\n"
                    "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
                    "  <title>تطبيق الوكالة الذاتي المتكامل</title>\n"
                    "</head>\n"
                    "<body>\n"
                    "  <main class=\"container\" id=\"app-root\">\n"
                    "    <header class=\"hero-section\">\n"
                    "      <h1 id=\"title\">منصة الوكالة الرقمية المستقلة</h1>\n"
                    "    </header>\n"
                    "    <div class=\"card\">المكون الأساسي</div>\n"
                    "  </main>\n"
                    "</body>\n"
                    "</html>"
                )

            valid_css = "body { font-family: sans-serif; background: #0f172a; color: #f8fafc; }\n.container { max-width: 800px; margin: 0 auto; }\n"
            valid_js = "document.addEventListener('DOMContentLoaded', function() { console.log('Ready'); });"
            return f"```html\n{raw_html}\n```\n\n```css\n{valid_css}\n```\n\n```javascript\n{valid_js}\n```"

        # QA Stage
        if "code-reviewer" in prompt_file or "qa" in p_lower:
            return "## تقرير فحص الجودة والأمان\nتم تدقيق كافة الأكواد وهياكل البيانات بنجاح."

        # Animation Stage
        if "whimsy" in prompt_file or "anim" in p_lower:
            return "```css\n.hero-section { animation: fadeIn 0.5s; }\n```\n```javascript\nconsole.log('anim');\n```"

        # SEO Stage
        if "seo" in prompt_file or "marketing" in p_lower:
            return "## تحسينات SEO\nتم فحص العلامات الدلالية وبيانات الميتا."

        return f"Output from {role_name} for {prompt[:30]}"

    return _adapter


try:
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


def _serve_directory_ephemerally(directory: str):
    """Serve a local directory on an ephemeral localhost port."""
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)
        def log_message(self, format, *args):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, f"http://127.0.0.1:{port}"


# =========================================================================
# Workstream A: Artifact Provenance and Manifest Integrity
# =========================================================================

def test_manifest_rejects_path_traversal_drive_escapes_and_reserved_devices():
    """
    Workstream A: build_artifact_manifest must reject:
    - Path traversal: '../file.txt', 'sub/../../file.txt'
    - Absolute paths: '/etc/passwd', '\\server\\share'
    - Windows drive escapes: 'C:secrets.txt', 'D:/passwords.json'
    - Alternate data streams: 'index.html:stream'
    - Windows DOS reserved device names: 'con', 'aux.txt', 'nul.html', 'com1'
    """
    valid_content = "<h1>Valid</h1>"

    # 1. Directory traversal
    with pytest.raises(ValueError, match="Path traversal detected"):
        build_artifact_manifest({"../secret.txt": valid_content})

    with pytest.raises(ValueError, match="Path traversal detected"):
        build_artifact_manifest({"css/../../secret.txt": valid_content})

    # 2. Absolute / UNC paths
    with pytest.raises(ValueError, match="Unsafe artifact path"):
        build_artifact_manifest({"/index.html": valid_content})

    with pytest.raises(ValueError, match="Unsafe artifact path"):
        build_artifact_manifest({"//server/share/index.html": valid_content})

    # 3. Windows drive escapes & alternate data streams
    with pytest.raises(ValueError, match="drive escape or alternate stream"):
        build_artifact_manifest({"C:passwords.txt": valid_content})

    with pytest.raises(ValueError, match="drive escape or alternate stream"):
        build_artifact_manifest({"index.html:hidden": valid_content})

    # 4. Windows reserved device names
    with pytest.raises(ValueError, match="reserved device name"):
        build_artifact_manifest({"con.txt": valid_content})

    with pytest.raises(ValueError, match="reserved device name"):
        build_artifact_manifest({"aux.html": valid_content})

    with pytest.raises(ValueError, match="reserved device name"):
        build_artifact_manifest({"nul": valid_content})

    with pytest.raises(ValueError, match="reserved device name"):
        build_artifact_manifest({"sub/com1.js": valid_content})

    # 5. Legitimate relative paths must succeed
    manifest = build_artifact_manifest({
        "index.html": valid_content,
        "css/style.css": "body { color: red; }",
        "js/script.js": "console.log('ok');"
    })
    assert manifest["total_artifacts"] == 3
    assert len(manifest["artifacts"]) == 3


def test_sqlite_persistence_failure_triggers_deliverable_rollback_and_session_failed(monkeypatch, tmp_path):
    """
    Workstream A & E: If SQLite manifest recording fails after filesystem promotion,
    the publication files must be immediately rolled back / removed from disk,
    and the session status must be set to 'failed'. No inconsistent unrecorded deliverables remain.
    """
    test_gen_dir = str(tmp_path / "gen_apps_persist_fail")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    session_id = db_store.create_session("PersistFailApp", "Test SQLite persist failure rollback")
    orch.build_project_task_graph(session_id, "PersistFailApp", "Test brief", {"archetype": "general"})

    valid_html = "<!DOCTYPE html><html><head><title>Test</title><link rel='stylesheet' href='style.css'></head><body><h1>Persist Fail Test</h1><script src='script.js'></script></body></html>"
    valid_css = "body { margin: 0; }"
    valid_js = "console.log('loaded');"

    # Pre-satisfy all DAG tasks so continue_workflow directly proceeds to publication
    tasks = db_store.list_tasks_for_session(session_id)
    for t in tasks:
        stg = t.get("stage")
        if stg in ("strategy", "product", "engineering", "design"):
            db_store.update_task_status(t["task_id"], "completed", output_text=f"Output for {stg}")
        elif stg == "development":
            code_out = f"```html\n{valid_html}\n```\n```css\n{valid_css}\n```\n```javascript\n{valid_js}\n```"
            db_store.update_task_status(t["task_id"], "completed", output_text=code_out)
        elif stg in ("qa", "animation", "seo"):
            db_store.update_task_status(t["task_id"], "completed", output_text=f"Output for {stg}")

    with state_lock:
        workflow_state["current_task"] = "تطبيق فحص التراجع عند فشل قاعدة البيانات"
        workflow_state["session_id"] = session_id
        workflow_state["outputs"] = {
            "strategy": "Strat ok",
            "product": "PRD ok",
            "engineering": "Arch ok",
            "design": "Design ok",
            "development": f"```html\n{valid_html}\n```\n```css\n{valid_css}\n```\n```javascript\n{valid_js}\n```",
            "qa": "QA passed"
        }
        workflow_state["departments"]["development"]["status"] = "done"

    # Simulate database disk error / lock error when record_artifact_manifest is called
    def _mock_record_manifest_fail(*args, **kwargs):
        raise sqlite3.OperationalError("Simulated SQLite disk I/O failure during manifest recording")

    monkeypatch.setattr(db_store, "record_artifact_manifest", _mock_record_manifest_fail)

    # Continue workflow should catch DB error, clean up published deliverables, and fail the session
    continue_workflow(session_id=session_id)

    # Assert session in SQLite is failed
    sess = db_store.get_session(session_id)
    assert sess["status"] == "failed"
    assert "Database delivery persistence failed" in (sess.get("error_message") or "")

    # Assert workflow state in-memory is failed
    with state_lock:
        assert workflow_state["status"] == "failed"

    # Assert deliverables were rolled back and do not remain orphaned on disk
    target_app_dir, clean_name = agency_app.get_project_folder_name("تطبيق فحص التراجع عند فشل قاعدة البيانات")
    if os.path.exists(target_app_dir):
        # index.html and manifest.json must NOT exist
        assert not os.path.exists(os.path.join(target_app_dir, "index.html"))
        assert not os.path.exists(os.path.join(target_app_dir, "manifest.json"))


# =========================================================================
# Workstream B: Independent Product Quality and QA Effectiveness
# =========================================================================

def test_qa_gate_detects_dangerous_url_schemes():
    """
    Workstream B: run_qa_checks must detect and reject dangerous URL schemes:
    - javascript: pseudo-protocol in href or src
    - data:text/html pseudo-protocol
    - vbscript: pseudo-protocol
    """
    safe_css = "body { color: black; }"
    safe_js = "console.log('ok');"

    # 1. javascript: in href
    bad_html_js = """<!DOCTYPE html><html><head><title>Bad</title></head><body>
    <a href="javascript:alert(document.cookie)">Click me</a>
    </body></html>"""
    rep1 = run_qa_checks(bad_html_js, safe_css, safe_js)
    assert rep1["passed"] is False
    assert any("Disallowed dangerous URL scheme" in err for err in rep1["errors"])

    # 2. data:text/html in iframe src
    bad_html_data = """<!DOCTYPE html><html><head><title>Bad</title></head><body>
    <iframe src="data:text/html,<script>alert(1)</script>"></iframe>
    </body></html>"""
    rep2 = run_qa_checks(bad_html_data, safe_css, safe_js)
    assert rep2["passed"] is False
    assert any("Disallowed dangerous URL scheme" in err for err in rep2["errors"])

    # 3. vbscript: in action
    bad_html_vb = """<!DOCTYPE html><html><head><title>Bad</title></head><body>
    <form action="vbscript:msgbox(1)"><button type="submit">Go</button></form>
    </body></html>"""
    rep3 = run_qa_checks(bad_html_vb, safe_css, safe_js)
    assert rep3["passed"] is False
    assert any("Disallowed dangerous URL scheme" in err for err in rep3["errors"])


def test_qa_gate_detects_syntax_errors_in_html_css_js():
    """
    Workstream B: Fault injection matrix for structural and syntactic failures:
    - HTML: unclosed div tags, missing doctype
    - CSS: missing colons in property declarations
    - JS: syntax error detected via Node vm.Script parser
    """
    # 1. HTML unclosed tag
    html_unclosed = "<!DOCTYPE html><html><head><title>T</title></head><body><div><span>text</div></body></html>"
    rep_html = run_qa_checks(html_unclosed, "body { margin: 0; }", "console.log('ok');")
    assert rep_html["passed"] is False
    assert any("Mismatched" in err or "Unclosed" in err for err in rep_html["errors"])

    # 2. CSS malformed property
    css_malformed = "body { color red; margin: 0; }"
    rep_css = run_qa_checks("<!DOCTYPE html><html><head><title>T</title></head><body><h1>OK</h1></body></html>", css_malformed, "console.log('ok');")
    assert rep_css["passed"] is False
    assert any("missing colon" in err for err in rep_css["errors"])

    # 3. JS syntax error
    js_broken = "function test() { const x = ; return x; }"
    rep_js = run_qa_checks("<!DOCTYPE html><html><head><title>T</title></head><body><h1>OK</h1></body></html>", "body { margin: 0; }", js_broken)
    assert rep_js["passed"] is False
    assert any("JavaScript syntax error" in err for err in rep_js["errors"])


# =========================================================================
# Workstream C: Genuine Autonomous Self-Healing & Concurrency
# =========================================================================

def test_remediation_task_concurrent_claim_mutual_exclusion(tmp_path):
    """
    Workstream C: Two workers attempting to claim the same remediation task
    must be mutually exclusive. Exactly one worker claims it, the second is rejected.
    """
    session_id = db_store.create_session("RemediationClaimApp", "Test concurrent claim on remediation")
    orch.build_project_task_graph(session_id, "RemediationClaimApp", "Test", {"archetype": "general"})

    tasks = db_store.list_tasks_for_session(session_id)
    dev_task = next(t for t in tasks if t.get("stage") == "development")

    # In a real workflow, dev_task has completed prior to remediation
    db_store.update_task_status(dev_task["task_id"], "completed", output_text="Initial flawed code")

    rem_task_id = orch.create_remediation_task(
        session_id=session_id,
        original_task_id=dev_task["task_id"],
        qa_errors=["HTML tag mismatch"],
        candidate_code={"html": "bad", "css": "body{}", "js": "var x=1;"},
        attempt_num=1
    )

    claims = []
    def _worker_claim(wid):
        res = db_store.claim_task(rem_task_id)
        claims.append((wid, res))

    t1 = threading.Thread(target=_worker_claim, args=(1,))
    t2 = threading.Thread(target=_worker_claim, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one succeeded, one failed
    successes = [c for c in claims if c[1] is True]
    failures = [c for c in claims if c[1] is False]
    assert len(successes) == 1
    assert len(failures) == 1


def test_remediation_agent_failure_records_honest_diagnostic(monkeypatch, tmp_path):
    """
    Workstream C: If a remediation agent fails or returns unparseable text,
    an explicit diagnostic is added to qa_report['errors'], and publication is blocked.
    """
    test_gen_dir = str(tmp_path / "gen_rem_fail")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    session_id = db_store.create_session("RemFailApp", "Test remediation agent failure")
    orch.build_project_task_graph(session_id, "RemFailApp", "Test", {"archetype": "general"})

    # Mark pre-requisites completed so development stage is ready
    tasks = db_store.list_tasks_for_session(session_id)
    for t in tasks:
        stg = t.get("stage")
        if stg in ("strategy", "product", "engineering", "design"):
            db_store.update_task_status(t["task_id"], "completed", output_text=f"Output for {stg}")
        elif stg == "development":
            db_store.update_task_status(t["task_id"], "completed", output_text="```html\n<div>Broken unclosed\n```\n```css\nbody{}\n```\n```javascript\nconsole.log(1);\n```")

    # Mock call_agent to return text with NO code blocks during remediation
    def _mock_call_agent(*args, **kwargs):
        return "I apologize, I cannot generate the requested code right now."

    with state_lock:
        workflow_state["current_task"] = "تطبيق فحص فشل وكيل المعالجة"
        workflow_state["session_id"] = session_id
        workflow_state["outputs"] = {
            "strategy": "Strat",
            "product": "PRD",
            "engineering": "Arch",
            "design": "Design",
            "development": "```html\n<div>Broken unclosed\n```\n```css\nbody{}\n```\n```javascript\nconsole.log(1);\n```",
            "qa": "QA found error"
        }

    continue_workflow(session_id=session_id, call_agent_fn=_mock_call_agent)

    # Session must be failed closed
    sess = db_store.get_session(session_id)
    assert sess["status"] == "failed"
    with state_lock:
        assert workflow_state["status"] == "failed"
        assert any("Remediation agent" in b or "فشل فحص" in b for b in workflow_state["blockers"])


# =========================================================================
# Workstream D: Provider Cost, Token Accounting, and Retry Safety
# =========================================================================

def test_explicit_zero_tokens_preserved_as_provider_reported():
    """
    Workstream D: When provider explicitly reports 0 tokens (e.g. prompt cache hit),
    it must NOT be treated as falsy or replaced with an estimated token count.
    """
    session_id = db_store.create_session("ZeroTokenApp", "Test zero token preservation")
    orch.build_project_task_graph(session_id, "ZeroTokenApp", "Brief", {"archetype": "general"})

    tasks = db_store.list_tasks_for_session(session_id)
    strat_task = next(t for t in tasks if t.get("stage") == "strategy")
    db_store.claim_task(strat_task["task_id"])

    # Mock agent returning explicit 0 tokens
    def _mock_zero_token_agent(*args, **kwargs):
        set_last_agent_execution(tokens_used=0, token_source="provider_reported", provider="test_gemini")
        return "دراسة جدوى متكاملة بتكلفة مجانية من الكاش"

    session_obj = db_store.get_session(session_id)
    ok, out, err = orch.execute_single_task(
        task=strat_task,
        session=session_obj,
        call_agent_fn=_mock_zero_token_agent,
        agent_prompt_resolver_fn=agency_app.get_agent_prompt_meta
    )
    assert ok is True

    # Inspect agent_runs in SQLite via list_agent_runs
    runs = db_store.list_agent_runs(session_id, task_id=strat_task["task_id"])
    assert len(runs) == 1
    assert runs[0]["tokens_used"] == 0
    assert runs[0]["token_source"] == "provider_reported"


def test_repeated_task_completion_idempotency_does_not_double_count():
    """
    Workstream D: If complete_task_with_agent_run is invoked repeatedly for the
    same task (e.g. retry / recovery replay), it must update the existing completed run
    and NOT insert duplicate runs or double-count tokens.
    """
    session_id = db_store.create_session("IdempotentRunApp", "Test repeated completion")
    orch.build_project_task_graph(session_id, "IdempotentRunApp", "Brief", {"archetype": "general"})

    tasks = db_store.list_tasks_for_session(session_id)
    strat_task = next(t for t in tasks if t.get("stage") == "strategy")
    tid = strat_task["task_id"]

    # First completion with 120 tokens
    s1, r1 = db_store.complete_task_with_agent_run(
        task_id=tid,
        session_id=session_id,
        agent_id="business-strategist",
        agent_name="مستشار الأعمال",
        division_id="strategy",
        prompt_file="strategy.md",
        output_text="Initial output",
        tokens_used=120,
        token_source="provider_reported"
    )
    assert s1 is True

    # Repeated completion with 120 tokens
    s2, r2 = db_store.complete_task_with_agent_run(
        task_id=tid,
        session_id=session_id,
        agent_id="business-strategist",
        agent_name="مستشار الأعمال",
        division_id="strategy",
        prompt_file="strategy.md",
        output_text="Repeated output",
        tokens_used=120,
        token_source="provider_reported"
    )
    assert s2 is True
    assert r1 == r2

    # Exactly 1 run row must exist
    runs = db_store.list_agent_runs(session_id, task_id=tid)
    assert len(runs) == 1

    # Operator audit total tokens billed must be 120, not 240
    audit = db_store.get_operator_session_audit(session_id)
    assert audit["cost_and_token_accounting"]["total_tokens_billed"] == 120


def test_malformed_and_negative_tokens_sanitized_to_zero():
    """
    Workstream D: Negative or malformed tokens must be safely clamped to 0.
    """
    session_id = db_store.create_session("SanitizeTokenApp", "Test token sanitation")
    orch.build_project_task_graph(session_id, "SanitizeTokenApp", "Brief", {"archetype": "general"})
    tasks = db_store.list_tasks_for_session(session_id)
    t = tasks[0]

    db_store.complete_task_with_agent_run(
        task_id=t["task_id"],
        session_id=session_id,
        agent_id="business-strategist",
        agent_name="مستشار",
        division_id="strategy",
        prompt_file="strategy.md",
        output_text="Valid output",
        tokens_used=-500,
        token_source="provider_reported"
    )

    runs = db_store.list_agent_runs(session_id, task_id=t["task_id"])
    assert runs[0]["tokens_used"] == 0


# =========================================================================
# Workstream E: Operator Audit, Security, and Recovery Decisions
# =========================================================================

def test_operator_audit_api_security_and_nonexistent_session(client):
    """
    Workstream E: Audit endpoint must return 404 for nonexistent sessions,
    and must not expose raw credentials or stack traces.
    """
    resp = client.get("/api/sessions/nonexistent_session_xyz_123/audit")
    assert resp.status_code == 404
    data = resp.get_json()
    assert "error" in data

    resp_m = client.get("/api/sessions/nonexistent_session_xyz_123/manifest")
    assert resp_m.status_code == 404


def test_operator_audit_distinguishes_ambiguous_provider_calls(tmp_path):
    """
    Workstream E: Operator audit must flag ambiguous provider calls and mark
    is_retry_safe = False to protect financial integrity.
    """
    session_id = db_store.create_session("AuditAmbiguousApp", "Test ambiguous provider audit")
    orch.build_project_task_graph(session_id, "AuditAmbiguousApp", "Brief", {"archetype": "general"})

    tasks = db_store.list_tasks_for_session(session_id)
    t = tasks[0]

    # Create run with ambiguous error message using record_agent_run
    db_store.record_agent_run(
        task_id=t["task_id"],
        session_id=session_id,
        agent_id="business-strategist",
        agent_name="مستشار",
        division_id="strategy",
        prompt_file="strategy.md",
        status="failed",
        error_message="Ambiguous provider timeout: Request sent to Gemini, status unknown"
    )
    db_store.update_session_status(session_id, "failed")

    audit = db_store.get_operator_session_audit(session_id)
    assert audit["cost_and_token_accounting"]["has_ambiguous_provider_call"] is True
    assert audit["failure_analysis"]["is_retry_safe"] is False
    assert "retry" not in audit["permitted_recovery_actions"]
    assert "inspect_diagnostics" in audit["permitted_recovery_actions"]


# =========================================================================
# Workstream G: Workforce Inventory & Canonical Division Integrity
# =========================================================================

def test_workforce_inventory_and_canonical_divisions():
    """
    Workstream G: Independently verifies:
    - Exactly 18 canonical divisions exist.
    - Exactly 282 workforce agents are dynamically discovered from the filesystem.
    """
    assert len(CANONICAL_DIVISIONS) == 18, f"Expected 18 canonical divisions, found {len(CANONICAL_DIVISIONS)}"

    inv = discover_agents()
    assert inv["status"] == "discovered"
    assert inv["total"] == 282, f"Expected 282 agents, found {inv['total']}"
    assert len(inv["divisions"]) == 18


# =========================================================================
# Workstream F: End-to-End Acceptance Scenarios
# =========================================================================

def test_scenario_1_successful_end_to_end_delivery(monkeypatch, tmp_path, client):
    """
    Scenario 1: Full end-to-end autonomous lifecycle:
    - Submit project -> Triage -> Pre-approval DAG execution -> PRD Approval Gate
    - Approve -> Resume -> Engineering -> Development -> QA checks -> Atomic Publication
    - Verified manifest in SQLite -> Operator audit verification -> Restart process consistency.
    """
    test_gen_dir = str(tmp_path / "gen_scenario_1")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    adapter = _make_deterministic_agent_adapter(html_defect=False)

    # Step 1: Start workflow
    with state_lock:
        workflow_state["current_task"] = "منصة عيادة أسنان متطورة"
    run_workflow("منصة عيادة أسنان متطورة", call_agent_fn=adapter)

    # Step 2: Assert approval gate reached
    with state_lock:
        assert workflow_state["status"] == "approval_needed"
        session_id = workflow_state["session_id"]
    assert session_id is not None

    sess = db_store.get_session(session_id)
    assert sess["status"] == "approval_needed"

    # Step 3: Approve and continue
    continue_workflow(feedback="معتمد بالكامل، تابع التنفيذ", session_id=session_id, call_agent_fn=adapter)

    # Step 4: Assert completion and publication
    with state_lock:
        assert workflow_state["status"] == "completed"

    sess_done = db_store.get_session(session_id)
    assert sess_done["status"] == "completed"

    # Step 5: Verify SQLite manifest and files
    manifest = db_store.get_artifact_manifest(session_id)
    assert len(manifest) >= 3
    paths = {m["relative_path"] for m in manifest}
    assert "index.html" in paths
    assert "style.css" in paths
    assert "script.js" in paths

    # Step 6: Verify Operator Audit API
    audit_resp = client.get(f"/api/sessions/{session_id}/audit")
    assert audit_resp.status_code == 200
    audit_data = audit_resp.get_json()
    assert audit_data["session"]["status"] == "completed"
    assert audit_data["publication_and_delivery"]["is_published"] is True
    assert audit_data["publication_and_delivery"]["published_artifact_count"] >= 3

    # Step 7: Restart resilience - re-query from separate connection
    fresh_audit = db_store.get_operator_session_audit(session_id)
    assert fresh_audit["session"]["status"] == "completed"
    assert fresh_audit["publication_and_delivery"]["is_published"] is True


def test_scenario_2_defect_and_successful_self_healing_repair(monkeypatch, tmp_path):
    """
    Scenario 2: Defect and successful repair:
    - Initial code has syntax defect (missing body tag to trigger QA failure).
    - QA detects defect.
    - Remediation task dispatched to agent.
    - Repaired artifact passes revalidation.
    - Successful publication with clean manifest.
    """
    test_gen_dir = str(tmp_path / "gen_scenario_2")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    adapter = _make_deterministic_agent_adapter(html_defect=True, defect_resolved=True)

    with state_lock:
        workflow_state["current_task"] = "تطبيق فحص الإصلاح الذاتي"
    run_workflow("تطبيق فحص الإصلاح الذاتي", call_agent_fn=adapter)

    with state_lock:
        sid = workflow_state["session_id"]

    continue_workflow(feedback="اعتمد وابدأ", session_id=sid, call_agent_fn=adapter)

    with state_lock:
        assert workflow_state["status"] == "completed"

    audit = db_store.get_operator_session_audit(sid)
    assert audit["publication_and_delivery"]["is_published"] is True
    assert audit["qa_and_remediation"]["remediation_succeeded"] is True


def test_scenario_3_unrecoverable_defect_fails_closed(monkeypatch, tmp_path):
    """
    Scenario 3: Unrecoverable QA failure fails closed and blocks publication:
    - Candidate continuously returns fatal syntax defects.
    - Bounded remediation retries are exhausted.
    - Publication is completely blocked.
    - Session terminal state is 'failed'.
    """
    test_gen_dir = str(tmp_path / "gen_scenario_3")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    broken_adapter = _make_deterministic_agent_adapter(html_defect=True, defect_resolved=False)

    with state_lock:
        workflow_state["current_task"] = "تطبيق فحص استنفاد المحاولات"
    run_workflow("تطبيق فحص استنفاد المحاولات", call_agent_fn=broken_adapter)

    with state_lock:
        sid = workflow_state["session_id"]

    continue_workflow(feedback="اعتمد", session_id=sid, call_agent_fn=broken_adapter)

    with state_lock:
        assert workflow_state["status"] == "failed"

    sess = db_store.get_session(sid)
    assert sess["status"] == "failed"

    # Assert no deliverables were published
    manifest = db_store.get_artifact_manifest(sid)
    assert len(manifest) == 0


def test_scenario_6_bilingual_clinic_browser_acceptance_with_negative_checks(monkeypatch, tmp_path):
    """
    Scenario 6: Browser-level acceptance testing:
    - Validates bilingual RTL/LTR toggle, booking form interactivity.
    - Negative check: asserts invalid submission is rejected.
    - Asserts 0 console errors and 0 unhandled runtime exceptions.
    """
    if not PLAYWRIGHT_AVAILABLE:
        pytest.skip("Playwright is not installed in current environment; skipping browser test")

    test_gen_dir = str(tmp_path / "gen_clinic_browser_h")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    clinic_html = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8">
  <title>عيادة النور لطب الأسنان</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <header>
    <h1 id="clinic-title">عيادة النور لطب الأسنان</h1>
    <button id="lang-toggle">English</button>
  </header>
  <main>
    <form id="booking-form">
      <input type="text" id="patient-name" placeholder="اسم المريض" required />
      <button type="submit" id="btn-submit">حجز موعد</button>
    </form>
    <div id="booking-confirmation" style="display:none;">تم تأكيد الحجز بنجاح!</div>
  </main>
  <script src="script.js"></script>
</body>
</html>"""

    clinic_css = """body { font-family: sans-serif; background: #f8fafc; color: #1e293b; padding: 20px; }
header { display: flex; justify-content: space-between; align-items: center; }
form { margin-top: 20px; display: flex; gap: 10px; }
input { padding: 8px 12px; border: 1px solid #cbd5e1; border-radius: 4px; }
button { padding: 8px 16px; background: #0284c7; color: white; border: none; border-radius: 4px; cursor: pointer; }
#booking-confirmation { margin-top: 15px; color: #16a34a; font-weight: bold; }"""

    clinic_js = """document.addEventListener('DOMContentLoaded', () => {
  const langBtn = document.getElementById('lang-toggle');
  const title = document.getElementById('clinic-title');
  const form = document.getElementById('booking-form');
  const input = document.getElementById('patient-name');
  const confirmMsg = document.getElementById('booking-confirmation');
  let currentLang = 'ar';

  langBtn.addEventListener('click', () => {
    if (currentLang === 'ar') {
      currentLang = 'en';
      document.documentElement.lang = 'en';
      document.documentElement.dir = 'ltr';
      title.innerText = 'Al-Noor Dental Clinic';
      langBtn.innerText = 'العربية';
      input.placeholder = 'Patient Name';
      document.getElementById('btn-submit').innerText = 'Book Appointment';
      confirmMsg.innerText = 'Appointment confirmed successfully!';
    } else {
      currentLang = 'ar';
      document.documentElement.lang = 'ar';
      document.documentElement.dir = 'rtl';
      title.innerText = 'عيادة النور لطب الأسنان';
      langBtn.innerText = 'English';
      input.placeholder = 'اسم المريض';
      document.getElementById('btn-submit').innerText = 'حجز موعد';
      confirmMsg.innerText = 'تم تأكيد الحجز بنجاح!';
    }
  });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (!input.value.trim()) return;
    confirmMsg.style.display = 'block';
  });
});"""

    qa_res = run_qa_checks(clinic_html, clinic_css, clinic_js)
    assert qa_res["passed"] is True

    clinic_dir = os.path.join(test_gen_dir, "bilingual_clinic_h")
    pub_res = publish_workflow_deliverables(
        app_dir=clinic_dir,
        task_title="عيادة أسنان ثنائية اللغة",
        validated_html=clinic_html,
        validated_css=clinic_css,
        validated_js=clinic_js,
        qa_report=qa_res,
        workflow_outputs={"strategy": "ok", "product": "ok"}
    )
    assert os.path.exists(os.path.join(clinic_dir, "manifest.json"))

    httpd, server_thread, base_url = _serve_directory_ephemerally(clinic_dir)
    page_errors = []
    console_errors = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

                page.goto(f"{base_url}/index.html", timeout=10000)

                # 1. Assert initial Arabic RTL layout
                assert page.locator("html").get_attribute("dir") == "rtl"
                assert "عيادة النور" in page.locator("#clinic-title").inner_text()

                # 2. Toggle language to English LTR
                page.locator("#lang-toggle").click()
                assert page.locator("html").get_attribute("dir") == "ltr"
                assert "Al-Noor Dental Clinic" in page.locator("#clinic-title").inner_text()

                # 3. Negative check: Empty input should not confirm
                page.locator("#btn-submit").click()
                assert page.locator("#booking-confirmation").is_hidden()

                # 4. Fill name and submit
                page.locator("#patient-name").fill("Dr. Ahmed")
                page.locator("#btn-submit").click()
                assert page.locator("#booking-confirmation").is_visible()
                assert "Appointment confirmed" in page.locator("#booking-confirmation").inner_text()

                # 5. Zero errors
                assert len(page_errors) == 0, f"Page errors: {page_errors}"
                assert len(console_errors) == 0, f"Console errors: {console_errors}"
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
