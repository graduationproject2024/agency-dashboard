import os
import sys
import json
import pytest
from app import (
    app,
    safe_resolve_agent_path,
    safe_load_agent_prompt,
    discover_agents,
    CANONICAL_DIVISIONS,
    CANONICAL_DIVISION_MAP,
    continue_workflow,
    workflow_state,
    state_lock,
    run_qa_checks
)

@pytest.fixture(autouse=True)
def isolate_settings_file(monkeypatch, tmp_path):
    temp_settings = tmp_path / "settings_adv_test.json"
    temp_settings.write_text(json.dumps({"provider": "gemini"}), encoding="utf-8")
    monkeypatch.setattr("app.SETTINGS_FILE", str(temp_settings))

@pytest.fixture(autouse=True)
def isolated_agents_environment(tmp_path, monkeypatch):
    """
    Hermetic test isolation fixture:
    Provisions a lightweight isolated directory containing representative agent definitions
    for each canonical agency division.
    Sets AGENTS_DIR to this isolated path for the duration of every test.
    Guarantees test portability across local workstations and headless CI runners.
    """
    agents_dir = tmp_path / "mock_agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    
    canonical_samples = [
        ("academic", "academic-research-scientist.md", "Academic Research Scientist"),
        ("design", "design-ui-designer.md", "UI Designer"),
        ("engineering", "engineering-code-reviewer.md", "Code Reviewer"),
        ("finance", "finance-financial-analyst.md", "Financial Analyst"),
        ("game-development", "game-designer.md", "Game Designer"),
        ("gis", "gis-analyst.md", "GIS Analyst"),
        ("healthcare", "healthcare-clinical-evidence-agent.md", "Clinical Evidence Specialist"),
        ("marketing", "marketing-seo-specialist.md", "SEO Specialist"),
        ("paid-media", "paid-media-manager.md", "Paid Media Manager"),
        ("product", "product-manager.md", "Product Manager"),
        ("project-management", "project-manager.md", "Project Manager"),
        ("research", "trend-researcher.md", "Trend Researcher"),
        ("sales", "outbound-strategist.md", "Outbound Strategist"),
        ("security", "security-appsec-engineer.md", "AppSec Engineer"),
        ("spatial-computing", "vision-pro-developer.md", "Vision Pro Developer"),
        ("specialized", "business-strategist.md", "Business Strategist"),
        ("support", "support-customer-care.md", "Customer Care Specialist"),
        ("testing", "testing-accessibility-auditor.md", "Accessibility Auditor")
    ]
    for div_id, filename, agent_name in canonical_samples:
        fpath = agents_dir / div_id / filename
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(
            f"---\nname: {agent_name}\ndivision: {div_id}\ndescription: Test specialist for {div_id}\n---\n# Specialist Agent: {agent_name}\nRole: Autonomous specialist test definition with sufficient length.\n",
            encoding="utf-8"
        )
        
    monkeypatch.setenv("AGENTS_DIR", str(agents_dir))
    monkeypatch.setattr("app.AGENTS_DIR", str(agents_dir))

@pytest.fixture(autouse=True)
def reset_state():
    """Reset workflow_state to idle baseline before and after each test."""
    with state_lock:
        workflow_state["status"] = "idle"
        workflow_state["current_task"] = None
        workflow_state["squad_plan"] = None
        workflow_state["active_squad"] = []
        workflow_state["feed"] = []
        workflow_state["blockers"] = []
        workflow_state["qa_report"] = None
        workflow_state["approval_request"] = None
        for d in workflow_state["departments"]:
            workflow_state["departments"][d] = {"status": "idle", "text": "في انتظار بدء المشروع"}
    yield
    with state_lock:
        workflow_state["status"] = "idle"
        workflow_state["current_task"] = None
        workflow_state["squad_plan"] = None
        workflow_state["active_squad"] = []
        workflow_state["feed"] = []
        workflow_state["blockers"] = []
        workflow_state["qa_report"] = None
        workflow_state["approval_request"] = None
        for d in workflow_state["departments"]:
            workflow_state["departments"][d] = {"status": "idle", "text": "في انتظار بدء المشروع"}

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

# Helper for cross-platform symlink creation in tests
def _create_symlink_or_skip(target, link_path, is_dir=False):
    try:
        if sys.platform == "win32":
            os.symlink(target, link_path, target_is_directory=is_dir)
        else:
            os.symlink(target, link_path)
    except OSError as e:
        if sys.platform == "win32":
            pytest.skip(f"Windows unprivileged environment denied symlink creation: {e}")
        else:
            pytest.fail(f"Linux/Ubuntu symlink creation failed: {e}")


# =========================================================================
# Part 1 — Security & Agent Path Resolution Tests
# =========================================================================

def test_safe_resolve_agent_path_traversal_rejection(tmp_path):
    """Rejects ../ traversal attempts in various formats."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    
    # Path traversal attempts
    for bad in ["../outside.md", "..\\outside.md", "sub/../../outside.md", "..", ".", ""]:
        res, err = safe_resolve_agent_path(bad, agents_dir=str(agents_dir))
        assert res is None
        assert err is not None

def test_safe_resolve_agent_path_absolute_path_rejection(tmp_path):
    """Rejects absolute paths and Windows drive letters."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    
    for bad in ["/etc/passwd", "C:\\Windows\\System32\\calc.exe", "\\Windows\\System32", "D:/test.md"]:
        res, err = safe_resolve_agent_path(bad, agents_dir=str(agents_dir))
        assert res is None
        assert "Absolute" in err or "escapes" in err or "Invalid path" in err

def test_safe_resolve_agent_path_final_symlink_escape_rejection(tmp_path):
    """Rejects final component symlink pointing outside agents directory."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    outside_file = tmp_path / "secret.md"
    outside_file.write_text("classified data", encoding="utf-8")
    
    link_file = agents_dir / "evil_link.md"
    _create_symlink_or_skip(str(outside_file), str(link_file), is_dir=False)
    
    res, err = safe_resolve_agent_path("evil_link.md", agents_dir=str(agents_dir))
    assert res is None
    assert "Symlink escape" in err

def test_safe_resolve_agent_path_intermediate_symlink_dir_escape_rejection(tmp_path):
    """
    CRITICAL: Intermediate directory symlink pointing outside agent directory.
    agents/external_dir -> outside_dir (which has target.md).
    Resolving external_dir/target.md must be strictly rejected as a symlink escape!
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    target_file = outside_dir / "target.md"
    target_file.write_text("outside agent secret content", encoding="utf-8")
    
    inter_symlink = agents_dir / "external_sub"
    _create_symlink_or_skip(str(outside_dir), str(inter_symlink), is_dir=True)
    
    res, err = safe_resolve_agent_path("external_sub/target.md", agents_dir=str(agents_dir))
    assert res is None
    assert "Symlink escape" in err

def test_safe_resolve_agent_path_broken_symlink_rejection(tmp_path):
    """Broken or dangling symlinks are rejected with explicit error."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    
    nonexistent_target = str(tmp_path / "does_not_exist_file.md")
    broken_link = agents_dir / "broken.md"
    _create_symlink_or_skip(nonexistent_target, str(broken_link), is_dir=False)
    
    res, err = safe_resolve_agent_path("broken.md", agents_dir=str(agents_dir))
    assert res is None
    assert "Broken symlink" in err or "does not exist" in err

def test_safe_resolve_agent_path_directory_rejection(tmp_path):
    """Directories are rejected because agent prompts must be regular files."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    sub_dir = agents_dir / "subfolder"
    sub_dir.mkdir()
    
    res, err = safe_resolve_agent_path("subfolder", agents_dir=str(agents_dir))
    assert res is None
    assert "directory" in err

def test_safe_resolve_agent_path_internal_symlink_accepted(tmp_path):
    """
    Internal symlink policy: symlinks resolving within the canonical trusted root
    to an existing regular file are permitted.
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    real_file = agents_dir / "real_agent.md"
    real_file.write_text("I am a real agent with sufficient content length to be valid.", encoding="utf-8")
    
    link_file = agents_dir / "alias_agent.md"
    _create_symlink_or_skip(str(real_file), str(link_file), is_dir=False)
    
    res, err = safe_resolve_agent_path("alias_agent.md", agents_dir=str(agents_dir))
    assert err is None
    assert res is not None
    assert os.path.realpath(res) == os.path.realpath(str(real_file))

def test_safe_resolve_agent_path_windows_and_unix_separators(tmp_path):
    """Both forward and backward slashes are normalized correctly."""
    agents_dir = tmp_path / "agents"
    sub = agents_dir / "engineering"
    sub.mkdir(parents=True)
    real_file = sub / "architect.md"
    real_file.write_text("Architecture specialist content that is long enough.", encoding="utf-8")
    
    # Unix separator
    res1, err1 = safe_resolve_agent_path("engineering/architect.md", agents_dir=str(agents_dir))
    assert err1 is None
    assert res1 is not None
    
    # Windows separator
    res2, err2 = safe_resolve_agent_path("engineering\\architect.md", agents_dir=str(agents_dir))
    assert err2 is None
    assert res2 is not None
    assert os.path.realpath(res1) == os.path.realpath(res2)


# =========================================================================
# Part 2 — Canonical Division Catalog & Assignment Tests
# =========================================================================

def test_all_18_canonical_divisions_exist():
    """Verify that all 18 canonical divisions exist in the canonical registry in stable order."""
    expected_divisions = [
        "Academic", "Design", "Engineering", "Finance", "Game Development",
        "GIS", "Healthcare", "Marketing", "Paid Media", "Product",
        "Project Management", "Research", "Sales", "Security", "Spatial Computing",
        "Specialized", "Support", "Testing"
    ]
    assert len(CANONICAL_DIVISIONS) == 18
    actual_names = [d["name"] for d in CANONICAL_DIVISIONS]
    assert actual_names == expected_divisions

def test_canonical_division_map_coverage():
    """Verify that every division slug maps to its canonical record."""
    for d in CANONICAL_DIVISIONS:
        assert d["id"] in CANONICAL_DIVISION_MAP
        assert CANONICAL_DIVISION_MAP[d["id"]]["name"] == d["name"]

def test_api_divisions_endpoint(client):
    """GET /api/divisions returns all 18 divisions with counts and stable order."""
    res = client.get('/api/divisions')
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] in ["discovered", "empty", "not_found"]
    assert data["total_divisions"] == 18
    divisions = data["divisions"]
    assert len(divisions) == 18
    
    # Verify each division has registered_count, discovered_count, icon, description
    for d in divisions:
        assert "id" in d
        assert "name" in d
        assert "icon" in d
        assert "description" in d
        assert "registered_count" in d
        assert "discovered_count" in d
        assert isinstance(d["agents"], list)

def test_empty_division_remains_visible(tmp_path):
    """If a division has zero agents, it must remain visible in catalog with count 0."""
    empty_agents_dir = tmp_path / "empty_agents"
    empty_agents_dir.mkdir()
    
    inv = discover_agents(str(empty_agents_dir))
    assert inv["total"] == 0
    assert len(inv["divisions"]) == 18
    for d in inv["divisions"]:
        assert d["registered_count"] == 0
        assert d["discovered_count"] == 0

def test_agent_division_metadata_in_api_agents(client):
    """GET /api/agents includes division_id, division_name, and status."""
    res = client.get('/api/agents')
    assert res.status_code == 200
    agents = res.get_json()
    assert isinstance(agents, list)
    assert len(agents) > 0
    for a in agents:
        assert "id" in a
        assert "name" in a
        assert "department" in a
        assert "division_id" in a
        assert "division_name" in a
        assert "status" in a

def test_agent_assignment_deterministic(tmp_path):
    """Test explicit frontmatter assignment takes precedence over directory name."""
    agents_dir = tmp_path / "agents"
    sub = agents_dir / "engineering"
    sub.mkdir(parents=True)
    
    # Agent in engineering folder, but frontmatter explicitly specifies division: Healthcare
    f1 = sub / "medical_coder.md"
    f1.write_text(
        "---\nname: Medical Coder\ndivision: Healthcare\ndescription: Healthcare coder\n---\nPrompt content here that is long enough.",
        encoding="utf-8"
    )
    
    inv = discover_agents(str(agents_dir))
    matched = next((a for a in inv["agents"] if a["id"] == "medical_coder"), None)
    assert matched is not None
    assert matched["division_id"] == "healthcare"
    assert matched["division_name"] == "Healthcare"


# =========================================================================
# Part 3 — Orchestration & QA Specialist Findings Preservation Tests
# =========================================================================

def test_qa_specialist_findings_reach_primary_reviewer(monkeypatch):
    """
    Test that when QA specialists execute, their findings are passed to the primary
    QA reviewer and stored in workflow_state['outputs']['qa_specialists'].
    """
    specialist_invocations = []
    
    def mock_call_agent(prompt_file, name, short_name, color, prompt, context):
        specialist_invocations.append({
            "prompt_file": prompt_file,
            "name": name,
            "prompt": prompt,
            "context": context
        })
        if "security-appsec-engineer" in prompt_file:
            return "VULNERABILITY DETECTED: Hardcoded credentials found in auth module."
        if "engineering-code-reviewer" in prompt_file:
            return f"Code review completed. Findings incorporated. Context length: {len(context)}"
        if "whimsy" in prompt_file or "animation" in prompt_file:
            return "```css\n.btn { transition: all 0.3s; }\n```\n```javascript\nconsole.log('anim');\n```"
        if "seo" in prompt_file:
            return "<meta name='description' content='test'>"
        return "Generic agent output"

    monkeypatch.setattr("app.call_agent", mock_call_agent)

    # Set up active squad with QA specialist
    with state_lock:
        workflow_state["active_squad"] = [
            {
                "id": "security-appsec-engineer",
                "name": "مهندس أمن التطبيقات",
                "display_name": "AppSec Engineer",
                "stage": "qa",
                "status": "selected",
                "reason": "Security audit"
            }
        ]
        workflow_state["squad_plan"] = {
            "archetype": "fintech",
            "specialists": [
                {
                    "id": "security-appsec-engineer",
                    "name": "مهندس أمن التطبيقات",
                    "display_name": "AppSec Engineer",
                    "short_name": "SEC",
                    "stage": "qa",
                    "reason": "Security audit",
                    "prompt_file": r"security\security-appsec-engineer.md",
                    "color": "red"
                }
            ]
        }
        workflow_state["departments"]["development"]["status"] = "done"

    continue_workflow()

    # Verify specialist findings are saved
    with state_lock:
        qa_spec_out = workflow_state["outputs"].get("qa_specialists", "")
        appsec_spec = next((s for s in workflow_state["active_squad"] if s["id"] == "security-appsec-engineer"), None)

    assert "VULNERABILITY DETECTED" in qa_spec_out
    assert appsec_spec is not None
    assert appsec_spec["status"] == "completed"

    # Verify primary code reviewer received specialist findings in its context
    reviewer_call = next((c for c in specialist_invocations if "engineering-code-reviewer" in c["prompt_file"]), None)
    assert reviewer_call is not None
    assert "VULNERABILITY DETECTED" in reviewer_call["context"]
    assert "نتائج وتدقيقات المتخصصين المستقلة" in reviewer_call["context"]

def test_specialist_failure_reported_honestly(monkeypatch):
    """
    If a specialist execution throws an exception or returns empty, its status
    must transition to 'failed' and NOT falsely report completed.
    """
    def mock_failing_call_agent(prompt_file, name, short_name, color, prompt, context):
        if "security-appsec-engineer" in prompt_file:
            raise RuntimeError("API quota exceeded")
        return "ok"

    monkeypatch.setattr("app.call_agent", mock_failing_call_agent)

    with state_lock:
        workflow_state["active_squad"] = [
            {
                "id": "security-appsec-engineer",
                "name": "مهندس أمن التطبيقات",
                "display_name": "AppSec Engineer",
                "stage": "qa",
                "status": "selected",
                "reason": "Security audit"
            }
        ]
        workflow_state["squad_plan"] = {
            "archetype": "fintech",
            "specialists": [
                {
                    "id": "security-appsec-engineer",
                    "name": "مهندس أمن التطبيقات",
                    "display_name": "AppSec Engineer",
                    "stage": "qa",
                    "prompt_file": r"security\security-appsec-engineer.md"
                }
            ]
        }

    continue_workflow()

    with state_lock:
        appsec_spec = next((s for s in workflow_state["active_squad"] if s["id"] == "security-appsec-engineer"), None)
    
    assert appsec_spec is not None
    assert appsec_spec["status"] == "failed"

def test_production_qa_gate_fails_closed():
    """Verify that production QA gate fails closed when errors exist."""
    bad_html = "<html><body>Missing closing tags"
    bad_css = "body { unclosed-bracket:"
    bad_js = "function bad() {"
    
    report = run_qa_checks(bad_html, bad_css, bad_js)
    assert report["passed"] is False
    assert len(report["errors"]) > 0
