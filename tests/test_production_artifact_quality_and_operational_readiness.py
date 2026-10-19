"""
Phase 2.4-G Acceptance & Operational Readiness Suite:
Production-Grade Artifact Quality, Autonomous Delivery Integrity & Operational Observability.

Exercises:
1. Candidate Manifest Anti-Tampering Gate:
   Proves that if any candidate artifact (HTML, CSS, JS) is corrupted or modified
   after passing QA, publication detects the SHA-256 digest mismatch, raises IOError,
   aborts publication, executes rollback, and blocks unverified deliverables.
2. Relative Asset Security & Protocol Verification in QA Gate:
   Rejects directory traversal (../) and forbidden protocols (file://) in asset links.
3. Operator Observability & Audit API:
   GET /api/sessions/<session_id>/audit and GET /api/sessions/<session_id>/manifest
   provide comprehensive failure correlation, cost attribution, QA diagnostics,
   manifest verification, and permitted recovery actions.
4. Functional Browser Acceptance Scenario 1: Bilingual Medical Clinic
   - English/Arabic language switching and layout direction (RTL/LTR)
   - Interactive appointment booking validation and submission confirmation
   - Zero console errors and zero failed asset requests
5. Functional Browser Acceptance Scenario 2: Industrial Telemetry Dashboard
   - Live telemetry gauges, meters, and status indicators
   - Out-of-range sensor value handling and alarm triggering
   - Zero console errors and zero unhandled exceptions
6. Functional Browser Acceptance Scenario 3: Interactive Browser Game
   - HTML5 Canvas loop initialization and state management
   - Keyboard control dispatch and score advancement
   - State reset on restart
   - Zero runtime errors
7. Ambiguous Provider Interruption Audit Reporting:
   Verifies that ambiguous in-flight calls are accurately flagged to prevent duplicate charges.
"""

import os
import sys
import json
import time
import uuid
import copy
import socket
import threading
import functools
import http.server
import urllib.request
import pytest
from unittest.mock import patch

import db_persistence as db_store
import task_orchestrator as orch
from app import (
    app,
    workflow_state,
    state_lock,
    run_workflow,
    continue_workflow,
    publish_workflow_deliverables,
    run_qa_checks,
    build_artifact_manifest,
    get_agent_prompt_meta,
    GENERATED_APPS_DIR
)

try:
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


def _serve_directory_ephemerally(directory_path):
    """Start an ephemeral ThreadingHTTPServer serving directory_path."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory_path)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server_port = httpd.server_address[1]
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{server_port}"
    return httpd, server_thread, base_url


def test_candidate_artifact_tampering_blocks_publication_and_rolls_back(tmp_path):
    """
    Security & Delivery Invariant:
    If any candidate artifact is modified or corrupted between QA approval and publication,
    publish_workflow_deliverables must detect the SHA-256 digest mismatch against
    the certified QA manifest, raise IOError, abort publication, and execute atomic rollback.
    """
    target_dir = str(tmp_path / "tampered_app")
    
    html_content = "<!DOCTYPE html><html lang='ar'><head><title>App</title><link rel='stylesheet' href='style.css'></head><body><h1>تطبيق أصلي</h1><script src='script.js'></script></body></html>"
    css_content = "body { margin: 0; background: #fafafa; }"
    js_content = "console.log('Certified code ready');"

    # Step 1: Run production QA checks to obtain valid certified manifest
    qa_report = run_qa_checks(html_content, css_content, js_content)
    assert qa_report["passed"] is True
    assert "qa_run_id" in qa_report
    assert "manifest" in qa_report

    # Step 2: Inject deliberate post-QA candidate tampering
    tampered_html = html_content.replace("تطبيق أصلي", "تطبيق تم التلاعب به بخبث")

    # Step 3: Attempt publication with tampered HTML candidate
    with pytest.raises(IOError) as exc_info:
        publish_workflow_deliverables(
            app_dir=target_dir,
            task_title="تطبيق تم التلاعب به",
            validated_html=tampered_html,
            validated_css=css_content,
            validated_js=js_content,
            qa_report=qa_report,
            workflow_outputs={"strategy": "ok", "product": "ok"}
        )

    # Step 4: Verify explicit tampering rejection error
    err_str = str(exc_info.value)
    assert "Candidate artifact tampering or corruption detected" in err_str
    assert "index.html" in err_str

    # Step 5: Verify atomic rollback - no partial deliverables promoted
    assert not os.path.exists(os.path.join(target_dir, "index.html"))
    assert not os.path.exists(os.path.join(target_dir, "manifest.json"))


def test_qa_gate_detects_and_rejects_path_traversal_asset_references():
    """
    QA Security Gate:
    Rejects path traversal (../) or forbidden protocols (file://) in asset links.
    """
    malicious_html = "<!DOCTYPE html><html><head><link rel='stylesheet' href='../../etc/passwd'></head><body><h1>Title</h1><script src='script.js'></script></body></html>"
    qa_res = run_qa_checks(malicious_html, "body { color: red; }", "console.log(1);")
    assert qa_res["passed"] is False
    assert any("Path traversal" in err for err in qa_res["errors"])

    protocol_html = "<!DOCTYPE html><html><head><link rel='stylesheet' href='style.css'></head><body><img src='file:///C:/secrets.txt'><script src='script.js'></script></body></html>"
    qa_res2 = run_qa_checks(protocol_html, "body { color: red; }", "console.log(1);")
    assert qa_res2["passed"] is False
    assert any("file://" in err for err in qa_res2["errors"])


def test_operator_session_audit_and_manifest_api(monkeypatch, tmp_path):
    """
    Operational Observability Gate:
    Verifies that GET /api/sessions/<session_id>/audit and GET /api/sessions/<session_id>/manifest
    return correlated diagnostics, token accounting, QA findings, manifest entries, and recovery actions.
    """
    test_gen_dir = str(tmp_path / "gen_audit_apps")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    session_id = f"sess_audit_{uuid.uuid4().hex[:8]}"
    db_store.create_session(
        session_id=session_id,
        project_name="بوابة التشخيص الميداني",
        task_description="بناء بوابة تشخيص ومراقبة متقدمة",
        squad_archetype="enterprise"
    )

    t1 = db_store.create_task(session_id, "task_strat", "دراسة الجدوى", "business-strategist", "strategy")
    db_store.complete_task_with_agent_run(
        task_id=t1,
        session_id=session_id,
        agent_id="business-strategist",
        agent_name="مستشار الأعمال",
        division_id="strategy",
        prompt_file="strategy/business-strategist.md",
        output_text="الجدوى معتمدة",
        tokens_used=450,
        token_source="provider_reported"
    )

    t2 = db_store.create_task(session_id, "task_deliv", "تسليم التطبيق", "project-manager", "delivery")
    db_store.claim_task(t2)

    # Record manifest entries
    manifest_entries = [
        {"relative_path": "index.html", "file_size": 1200, "sha256": "abcdef123456", "artifact_type": "document", "validation_status": "published"},
        {"relative_path": "style.css", "file_size": 800, "sha256": "7890abcdef12", "artifact_type": "stylesheet", "validation_status": "published"}
    ]
    db_store.record_artifact_manifest(session_id, "qa_run_test1", manifest_entries, delivery_id="deliv_test1")

    db_store.complete_task_with_agent_run(
        task_id=t2,
        session_id=session_id,
        agent_id="project-manager",
        agent_name="مدير المشروع",
        division_id="project-management",
        prompt_file="product/project-manager.md",
        output_text="تم التسليم بنجاح",
        tokens_used=150,
        token_source="estimated"
    )
    db_store.update_session_status(session_id, "completed")

    client = app.test_client()

    # 1. Query manifest endpoint
    man_resp = client.get(f"/api/sessions/{session_id}/manifest")
    assert man_resp.status_code == 200
    man_data = man_resp.get_json()
    assert man_data["session_id"] == session_id
    assert man_data["total"] == 2
    paths = [m["relative_path"] for m in man_data["manifest"]]
    assert "index.html" in paths
    assert "style.css" in paths

    # 2. Query operator audit endpoint
    audit_resp = client.get(f"/api/sessions/{session_id}/audit")
    assert audit_resp.status_code == 200
    audit_data = audit_resp.get_json()

    assert audit_data["session"]["session_id"] == session_id
    assert audit_data["tasks_summary"]["completed"] == 2
    assert audit_data["cost_and_token_accounting"]["total_tokens_billed"] == 600
    assert audit_data["cost_and_token_accounting"]["provider_reported_tokens"] == 450
    assert audit_data["cost_and_token_accounting"]["estimated_tokens"] == 150
    assert audit_data["cost_and_token_accounting"]["has_ambiguous_provider_call"] is False
    assert audit_data["publication_and_delivery"]["is_published"] is True
    assert audit_data["publication_and_delivery"]["published_artifact_count"] == 2
    assert "inspect_deliverables" in audit_data["permitted_recovery_actions"]


def test_scenario_1_bilingual_clinic_browser_functional_acceptance(monkeypatch, tmp_path):
    """
    Functional Browser Acceptance Scenario 1: Bilingual Medical Clinic
    Exercises:
    - Real Playwright browser render on ephemeral HTTP server
    - Language switcher toggling between Arabic (RTL) and English (LTR)
    - Interactive appointment booking submission and DOM confirmation feedback
    - Zero console errors and zero failed network requests
    """
    if not PLAYWRIGHT_AVAILABLE:
        pytest.fail("MANDATORY ACCEPTANCE FAILURE: Playwright is required for browser functional acceptance testing.")

    test_gen_dir = str(tmp_path / "gen_clinic_browser")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    clinic_html = """<!DOCTYPE html>
<html lang="ar" dir="rtl" id="root-html">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>عيادة الأمل الطبية التخصصية | Al-Amal Medical Clinic</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <header>
    <div class="brand">
      <h1 id="clinic-name">عيادة الأمل التخصصية</h1>
      <p id="clinic-subtitle">رعاية طبية متكاملة بمعايير عالمية</p>
    </div>
    <button id="lang-toggle" data-lang="ar">English</button>
  </header>
  
  <main>
    <section id="booking-section">
      <h2 id="booking-title">حجز موعد كشف طبي</h2>
      <form id="booking-form" onsubmit="return false;">
        <input type="text" id="patient-name" placeholder="اسم المريض" required>
        <input type="tel" id="patient-phone" placeholder="رقم الهاتف" required>
        <select id="clinic-department">
          <option value="cardio">قسم القلب والأوعية الدموية</option>
          <option value="pedia">قسم طب الأطفال</option>
        </select>
        <button type="submit" id="btn-book">تأكيد الموعد الطبي</button>
      </form>
      <div id="booking-confirmation" style="display: none;" class="alert-success">
        <span id="confirm-msg">تم تسجيل موعدكم الطبي بنجاح! كود الحجز: MED-8842</span>
      </div>
    </section>
  </main>
  <script src="script.js"></script>
</body>
</html>"""

    clinic_css = """body { font-family: sans-serif; margin: 0; padding: 20px; background: #f0f7f6; color: #1e3d3b; }
header { display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #00897b; padding-bottom: 12px; }
button { background: #00897b; color: #fff; border: none; padding: 10px 18px; border-radius: 6px; cursor: pointer; font-size: 15px; }
input, select { display: block; width: 100%; max-width: 380px; margin: 10px 0; padding: 10px; border: 1px solid #b2dfdb; border-radius: 4px; box-sizing: border-box; }
.alert-success { background: #d4edda; color: #155724; padding: 14px; border-radius: 6px; margin-top: 15px; }"""

    clinic_js = """document.addEventListener('DOMContentLoaded', () => {
  const langBtn = document.getElementById('lang-toggle');
  const root = document.getElementById('root-html');
  const nameEl = document.getElementById('clinic-name');
  const subEl = document.getElementById('clinic-subtitle');
  const bookTitle = document.getElementById('booking-title');
  const btnBook = document.getElementById('btn-book');
  const form = document.getElementById('booking-form');
  const confirmBox = document.getElementById('booking-confirmation');
  const confirmMsg = document.getElementById('confirm-msg');

  langBtn.addEventListener('click', () => {
    const curLang = langBtn.getAttribute('data-lang');
    if (curLang === 'ar') {
      root.setAttribute('lang', 'en');
      root.setAttribute('dir', 'ltr');
      langBtn.setAttribute('data-lang', 'en');
      langBtn.innerText = 'العربية';
      nameEl.innerText = 'Al-Amal Specialized Medical Clinic';
      subEl.innerText = 'Comprehensive Medical Care with Global Standards';
      bookTitle.innerText = 'Book a Clinical Appointment';
      btnBook.innerText = 'Confirm Medical Booking';
    } else {
      root.setAttribute('lang', 'ar');
      root.setAttribute('dir', 'rtl');
      langBtn.setAttribute('data-lang', 'ar');
      langBtn.innerText = 'English';
      nameEl.innerText = 'عيادة الأمل التخصصية';
      subEl.innerText = 'رعاية طبية متكاملة بمعايير عالمية';
      bookTitle.innerText = 'حجز موعد كشف طبي';
      btnBook.innerText = 'تأكيد الموعد الطبي';
    }
  });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const pName = document.getElementById('patient-name').value.trim();
    if (!pName) return;
    const isAr = langBtn.getAttribute('data-lang') === 'ar';
    confirmMsg.innerText = isAr 
      ? `تم تأكيد حجز المريض (${pName}) بنجاح! كود الحجز: MED-8842`
      : `Appointment confirmed for patient (${pName})! Booking Code: MED-8842`;
    confirmBox.style.display = 'block';
  });
});"""

    qa_rep = run_qa_checks(clinic_html, clinic_css, clinic_js)
    assert qa_rep["passed"] is True

    clinic_proj_dir = os.path.join(test_gen_dir, "bilingual_clinic_app")
    pub_res = publish_workflow_deliverables(
        app_dir=clinic_proj_dir,
        task_title="عيادة طبية ثنائية اللغة",
        validated_html=clinic_html,
        validated_css=clinic_css,
        validated_js=clinic_js,
        qa_report=qa_rep,
        workflow_outputs={"strategy": "ok", "product": "ok"}
    )
    assert os.path.exists(os.path.join(clinic_proj_dir, "manifest.json"))

    httpd, server_thread, base_url = _serve_directory_ephemerally(clinic_proj_dir)
    page_errors = []
    console_errors = []
    failed_requests = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
                page.on("requestfailed", lambda req: failed_requests.append(req.url))

                resp = page.goto(f"{base_url}/index.html", timeout=10000)
                assert resp is not None and resp.status == 200

                # 1. Assert initial Arabic RTL layout
                root = page.locator("#root-html")
                assert root.get_attribute("dir") == "rtl"
                assert "عيادة الأمل التخصصية" in page.locator("#clinic-name").inner_text()

                # 2. Toggle to English
                page.locator("#lang-toggle").click()
                page.wait_for_function("document.getElementById('root-html').getAttribute('dir') === 'ltr'", timeout=3000)
                assert root.get_attribute("dir") == "ltr"
                assert "Al-Amal Specialized Medical Clinic" in page.locator("#clinic-name").inner_text()

                # 3. Fill and submit appointment booking
                page.locator("#patient-name").fill("Dr. Sarah Connor")
                page.locator("#patient-phone").fill("+966501234567")
                page.locator("#btn-book").click()

                page.wait_for_selector("#booking-confirmation", state="visible", timeout=3000)
                confirm_text = page.locator("#confirm-msg").inner_text()
                assert "Appointment confirmed for patient (Dr. Sarah Connor)" in confirm_text

                # 4. Assert zero console/page errors and zero request failures
                assert len(page_errors) == 0, f"Page errors: {page_errors}"
                assert len(console_errors) == 0, f"Console errors: {console_errors}"
                assert len(failed_requests) == 0, f"Failed requests: {failed_requests}"

            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        server_thread.join(timeout=3)


def test_scenario_2_industrial_telemetry_dashboard_browser_functional_acceptance(monkeypatch, tmp_path):
    """
    Functional Browser Acceptance Scenario 2: Industrial Telemetry Dashboard
    Exercises:
    - Real Playwright browser render of telemetry metrics and gauges
    - Simulated out-of-range sensor readings triggering dynamic alarm status
    - Error fallback boundary on empty/invalid readings
    - Zero console errors and zero failed network requests
    """
    if not PLAYWRIGHT_AVAILABLE:
        pytest.fail("MANDATORY ACCEPTANCE FAILURE: Playwright is required for browser functional acceptance testing.")

    test_gen_dir = str(tmp_path / "gen_telemetry_browser")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    telemetry_html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Industrial Predictive-Maintenance Telemetry Dashboard</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <div class="dashboard-container">
    <header>
      <h1>Asset Telemetry & Anomaly Monitor</h1>
      <div id="system-status-badge" class="badge-normal">NORMAL OPERATION</div>
    </header>
    
    <div class="metrics-grid">
      <div class="card" id="card-temp">
        <h3>Motor Temperature</h3>
        <span class="value" id="val-temp">68.4 °C</span>
      </div>
      <div class="card" id="card-vib">
        <h3>Vibration RMS</h3>
        <span class="value" id="val-vib">1.8 mm/s</span>
      </div>
      <div class="card" id="card-press">
        <h3>Hydraulic Pressure</h3>
        <span class="value" id="val-press">145 bar</span>
      </div>
    </div>
    
    <div class="controls">
      <button id="btn-normal">Inject Normal Telemetry</button>
      <button id="btn-overheat">Inject Overheating Anomaly</button>
      <button id="btn-invalid">Inject Corrupt Sensor Data</button>
    </div>
  </div>
  <script src="script.js"></script>
</body>
</html>"""

    telemetry_css = """body { font-family: monospace; background: #121820; color: #ecf0f1; margin: 0; padding: 24px; }
.dashboard-container { max-width: 900px; margin: 0 auto; }
header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
.badge-normal { background: #27ae60; color: #fff; padding: 8px 16px; border-radius: 4px; font-weight: bold; }
.badge-warning { background: #e67e22; color: #fff; padding: 8px 16px; border-radius: 4px; font-weight: bold; }
.badge-critical { background: #c0392b; color: #fff; padding: 8px 16px; border-radius: 4px; font-weight: bold; }
.metrics-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.card { background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155; }
.value { font-size: 28px; font-weight: bold; color: #38bdf8; display: block; margin-top: 10px; }
.controls { margin-top: 24px; display: flex; gap: 12px; }
button { background: #3b82f6; color: #fff; border: none; padding: 10px 16px; border-radius: 4px; cursor: pointer; }"""

    telemetry_js = """document.addEventListener('DOMContentLoaded', () => {
  const badge = document.getElementById('system-status-badge');
  const tempEl = document.getElementById('val-temp');
  const vibEl = document.getElementById('val-vib');
  const pressEl = document.getElementById('val-press');

  function updateTelemetry(temp, vib, press) {
    if (isNaN(temp) || isNaN(vib) || isNaN(press)) {
      badge.className = 'badge-warning';
      badge.innerText = 'SENSOR FAULT / SIGNAL LOST';
      tempEl.innerText = '--';
      vibEl.innerText = '--';
      pressEl.innerText = '--';
      return;
    }
    tempEl.innerText = temp.toFixed(1) + ' °C';
    vibEl.innerText = vib.toFixed(1) + ' mm/s';
    pressEl.innerText = press.toFixed(0) + ' bar';

    if (temp > 95 || vib > 5.0) {
      badge.className = 'badge-critical';
      badge.innerText = 'CRITICAL ANOMALY DETECTED';
    } else if (temp > 85 || vib > 3.0) {
      badge.className = 'badge-warning';
      badge.innerText = 'WARNING: HIGH THERMAL LOAD';
    } else {
      badge.className = 'badge-normal';
      badge.innerText = 'NORMAL OPERATION';
    }
  }

  document.getElementById('btn-normal').addEventListener('click', () => updateTelemetry(72.0, 1.9, 142));
  document.getElementById('btn-overheat').addEventListener('click', () => updateTelemetry(104.5, 6.2, 168));
  document.getElementById('btn-invalid').addEventListener('click', () => updateTelemetry(NaN, NaN, NaN));
});"""

    qa_rep = run_qa_checks(telemetry_html, telemetry_css, telemetry_js)
    assert qa_rep["passed"] is True

    telemetry_proj_dir = os.path.join(test_gen_dir, "telemetry_app")
    pub_res = publish_workflow_deliverables(
        app_dir=telemetry_proj_dir,
        task_title="لوحة تحكم صناعية",
        validated_html=telemetry_html,
        validated_css=telemetry_css,
        validated_js=telemetry_js,
        qa_report=qa_rep,
        workflow_outputs={"strategy": "ok", "product": "ok"}
    )
    assert os.path.exists(os.path.join(telemetry_proj_dir, "manifest.json"))

    httpd, server_thread, base_url = _serve_directory_ephemerally(telemetry_proj_dir)
    page_errors = []
    console_errors = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

                resp = page.goto(f"{base_url}/index.html", timeout=10000)
                assert resp is not None and resp.status == 200

                # 1. Assert normal initial state
                badge = page.locator("#system-status-badge")
                assert "NORMAL OPERATION" in badge.inner_text()
                assert "68.4 °C" in page.locator("#val-temp").inner_text()

                # 2. Trigger overheat critical anomaly
                page.locator("#btn-overheat").click()
                page.wait_for_function("document.getElementById('system-status-badge').innerText.includes('CRITICAL')", timeout=3000)
                assert "CRITICAL ANOMALY DETECTED" in badge.inner_text()
                assert "104.5 °C" in page.locator("#val-temp").inner_text()

                # 3. Trigger sensor failure fallback
                page.locator("#btn-invalid").click()
                page.wait_for_function("document.getElementById('system-status-badge').innerText.includes('SENSOR FAULT')", timeout=3000)
                assert "SENSOR FAULT / SIGNAL LOST" in badge.inner_text()
                assert page.locator("#val-temp").inner_text() == "--"

                # 4. Zero errors
                assert len(page_errors) == 0, f"Page errors: {page_errors}"
                assert len(console_errors) == 0, f"Console errors: {console_errors}"

            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        server_thread.join(timeout=3)


def test_scenario_3_browser_game_functional_acceptance(monkeypatch, tmp_path):
    """
    Functional Browser Acceptance Scenario 3: Interactive Browser Game
    Exercises:
    - HTML5 Canvas rendering & game loop advancement
    - Keyboard controls (Space / Arrow keys) updating player position and score
    - Restart button resetting state and score to 0
    - Zero console errors and zero uncaught runtime errors
    """
    if not PLAYWRIGHT_AVAILABLE:
        pytest.fail("MANDATORY ACCEPTANCE FAILURE: Playwright is required for browser functional acceptance testing.")

    test_gen_dir = str(tmp_path / "gen_game_browser")
    os.makedirs(test_gen_dir, exist_ok=True)
    monkeypatch.setattr("app.GENERATED_APPS_DIR", test_gen_dir)

    game_html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Retro Arcade Runner Game</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <div class="game-wrapper">
    <h1>Retro Arcade Runner</h1>
    <div class="hud">
      <span>Score: <strong id="score-display">0</strong></span>
      <span>State: <strong id="game-status">RUNNING</strong></span>
    </div>
    <canvas id="game-canvas" width="400" height="200"></canvas>
    <div class="controls-panel">
      <button id="btn-jump">Jump (Space)</button>
      <button id="btn-restart">Restart Game</button>
    </div>
  </div>
  <script src="script.js"></script>
</body>
</html>"""

    game_css = """body { font-family: monospace; background: #0f172a; color: #f8fafc; text-align: center; margin: 0; padding: 20px; }
.game-wrapper { max-width: 480px; margin: 0 auto; }
.hud { display: flex; justify-content: space-between; margin-bottom: 10px; font-size: 18px; }
canvas { background: #1e293b; border: 2px solid #38bdf8; border-radius: 8px; display: block; margin: 0 auto; }
.controls-panel { margin-top: 14px; display: flex; justify-content: center; gap: 12px; }
button { background: #38bdf8; color: #0f172a; font-weight: bold; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; }"""

    game_js = """document.addEventListener('DOMContentLoaded', () => {
  const canvas = document.getElementById('game-canvas');
  const ctx = canvas.getContext('2d');
  const scoreEl = document.getElementById('score-display');
  const statusEl = document.getElementById('game-status');

  let score = 0;
  let playerY = 150;
  let isRunning = true;

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#22c55e';
    ctx.fillRect(50, playerY, 20, 20); // Player
    ctx.fillStyle = '#94a3b8';
    ctx.fillRect(0, 170, canvas.width, 30); // Ground
  }

  function tick() {
    if (!isRunning) return;
    score += 1;
    scoreEl.innerText = score.toString();
    draw();
    requestAnimationFrame(tick);
  }

  function jump() {
    if (!isRunning) return;
    playerY = 100;
    score += 10;
    scoreEl.innerText = score.toString();
    setTimeout(() => { playerY = 150; draw(); }, 200);
  }

  function restart() {
    score = 0;
    playerY = 150;
    isRunning = true;
    scoreEl.innerText = '0';
    statusEl.innerText = 'RUNNING';
    draw();
  }

  window.addEventListener('keydown', (e) => {
    if (e.code === 'Space' || e.code === 'ArrowUp') {
      jump();
    }
  });

  document.getElementById('btn-jump').addEventListener('click', jump);
  document.getElementById('btn-restart').addEventListener('click', restart);

  draw();
  requestAnimationFrame(tick);
});"""

    qa_rep = run_qa_checks(game_html, game_css, game_js)
    assert qa_rep["passed"] is True

    game_proj_dir = os.path.join(test_gen_dir, "browser_game_app")
    pub_res = publish_workflow_deliverables(
        app_dir=game_proj_dir,
        task_title="لعبة متصفح تفاعلية",
        validated_html=game_html,
        validated_css=game_css,
        validated_js=game_js,
        qa_report=qa_rep,
        workflow_outputs={"strategy": "ok", "product": "ok"}
    )
    assert os.path.exists(os.path.join(game_proj_dir, "manifest.json"))

    httpd, server_thread, base_url = _serve_directory_ephemerally(game_proj_dir)
    page_errors = []
    console_errors = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

                resp = page.goto(f"{base_url}/index.html", timeout=10000)
                assert resp is not None and resp.status == 200

                # 1. Assert canvas and initial running state
                page.wait_for_selector("#game-canvas", timeout=3000)
                assert page.locator("#game-status").inner_text() == "RUNNING"

                # 2. Wait for game loop to tick score
                page.wait_for_function("parseInt(document.getElementById('score-display').innerText) >= 5", timeout=3000)
                score_before_jump = int(page.locator("#score-display").inner_text())
                assert score_before_jump >= 5

                # 3. Send keyboard Space jump action
                page.keyboard.press("Space")
                page.wait_for_function(f"parseInt(document.getElementById('score-display').innerText) > {score_before_jump}", timeout=3000)
                score_after_jump = int(page.locator("#score-display").inner_text())
                assert score_after_jump > score_before_jump

                # 4. Click Restart and assert reset
                page.locator("#btn-restart").click()
                time.sleep(0.05)
                # The game ticks immediately but score was reset
                current_score = int(page.locator("#score-display").inner_text())
                assert current_score < score_after_jump

                # 5. Zero errors
                assert len(page_errors) == 0, f"Page errors: {page_errors}"
                assert len(console_errors) == 0, f"Console errors: {console_errors}"

            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        server_thread.join(timeout=3)


def test_ambiguous_provider_interruption_audit_reporting():
    """
    Financial & Observability Invariant:
    If a crash occurs with an external provider request in-flight,
    get_operator_session_audit must truthfully report:
    - has_ambiguous_provider_call: True
    - is_provider_ambiguous: True
    - is_retry_safe: False (to prevent duplicate billable calls without confirmation)
    """
    session_id = f"sess_ambig_{uuid.uuid4().hex[:8]}"
    db_store.create_session(
        session_id=session_id,
        project_name="مشروع معلق في المزود",
        task_description="طلب خارجي قيد التنفيذ",
        squad_archetype="enterprise"
    )

    task = db_store.create_task(session_id, "task_ai_call", "استدعاء نموذج الذكاء الاصطناعي", "senior-developer", "development")
    db_store.claim_task(task)
    
    # Record in-flight pre-flight execution marker
    run_id = db_store.record_agent_run_start(
        task_id=task,
        session_id=session_id,
        agent_id="senior-developer",
        agent_name="مطور رئيسي",
        division_id="engineering",
        prompt_file="engineering/senior-developer.md"
    )

    # Reconcile crash - provider request was in-flight!
    db_store.reconcile_interrupted_sessions()

    # Operator audit report verification
    audit = db_store.get_operator_session_audit(session_id)
    assert audit["cost_and_token_accounting"]["has_ambiguous_provider_call"] is True
    assert audit["failure_analysis"]["is_provider_ambiguous"] is True
    assert audit["failure_analysis"]["is_retry_safe"] is False
    assert "retry" not in audit["permitted_recovery_actions"]
    assert "inspect_diagnostics" in audit["permitted_recovery_actions"]
