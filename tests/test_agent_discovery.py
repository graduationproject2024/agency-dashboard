import os
import tempfile
import pytest
from unittest.mock import patch
from app import app, discover_agents

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_discover_agents_real_directory():
    """Verify that agent discovery finds valid markdown definitions in default AGENTS_DIR."""
    result = discover_agents()
    assert result["status"] in ["discovered", "not_found"]
    if result["status"] == "discovered":
        assert result["total"] > 0
        assert len(result["agents"]) == result["total"]
        sample = result["agents"][0]
        assert "id" in sample
        assert "name" in sample
        assert "department" in sample
        assert "status" in sample

def test_discover_agents_nonexistent_directory_never_fabricates_count():
    """Verify that a missing directory returns total 0 and status 'not_found', never fabricated 282."""
    missing_dir = r"C:\non_existent_agents_folder_xyz_98765"
    result = discover_agents(missing_dir)
    assert result["total"] == 0
    assert result["status"] == "not_found"
    assert result["error"] is not None
    assert result["agents"] == []

def test_discover_agents_empty_directory(tmp_path):
    """Verify that an empty directory returns total 0 and status 'empty'."""
    result = discover_agents(str(tmp_path))
    assert result["total"] == 0
    assert result["status"] == "empty"
    assert result["agents"] == []

def test_discover_agents_environment_override(monkeypatch, tmp_path):
    """Verify that AGENTS_DIR environment variable takes precedence."""
    env_dir = tmp_path / "custom_env_agents"
    eng_dir = env_dir / "engineering"
    eng_dir.mkdir(parents=True)
    
    agent_file = eng_dir / "senior-dev.md"
    agent_file.write_text("# Senior Developer Agent\nRole: Lead Engineer", encoding="utf-8")
    
    monkeypatch.setenv("AGENTS_DIR", str(env_dir))
    
    result = discover_agents()
    assert result["status"] == "discovered"
    assert result["total"] == 1
    assert result["agents"][0]["id"] == "senior-dev"
    assert result["directory"] == str(env_dir)

def test_discover_agents_prunes_hidden_directories_and_build_folders(tmp_path):
    """Verify that hidden directories (.git, .github, .agents) and node_modules are pruned."""
    # Hidden dirs
    for hidden in [".git", ".github", ".agents", "node_modules", "__pycache__"]:
        h_dir = tmp_path / hidden
        h_dir.mkdir()
        (h_dir / "fake-agent.md").write_text("# Fake Agent Definition in hidden dir", encoding="utf-8")
        
    # Legitimate dir
    valid_dir = tmp_path / "design"
    valid_dir.mkdir()
    (valid_dir / "ui-designer.md").write_text("# UI Designer Agent\nPortfolio and spec", encoding="utf-8")

    result = discover_agents(str(tmp_path))
    assert result["total"] == 1
    assert result["agents"][0]["id"] == "ui-designer"

def test_discover_agents_filters_unrelated_markdown_and_empty_files(tmp_path):
    """Verify that doc files (README, TODO, CHANGELOG, translations) and empty files are ignored."""
    dept_dir = tmp_path / "marketing"
    dept_dir.mkdir()

    # Valid agent
    (dept_dir / "growth-hacker.md").write_text("# Growth Hacker\nAcquisition experiments", encoding="utf-8")

    # Documentation files that should be ignored
    for doc in ["README.md", "CONTRIBUTING.md", "LICENSE.md", "CHANGELOG.md", "TODO.md", "agent_zh-CN.md"]:
        (dept_dir / doc).write_text("# Documentation content", encoding="utf-8")

    # File under 20 bytes (stubs/empty files)
    (dept_dir / "empty-stub.md").write_text("# Stub", encoding="utf-8")

    result = discover_agents(str(tmp_path))
    assert result["total"] == 1
    assert result["agents"][0]["id"] == "growth-hacker"

def test_discover_agents_unreadable_path_returns_error_status(monkeypatch, tmp_path):
    """Verify that filesystem permission / OS walk errors return status 'error' without crashing."""
    with patch("os.walk", side_effect=PermissionError("Simulated Access Denied")):
        result = discover_agents(str(tmp_path))
        assert result["total"] == 0
        assert result["status"] == "error"
        assert "Access Denied" in result["error"]

def test_get_agents_endpoint(client):
    """Verify GET /api/agents returns JSON agent list with mapped statuses."""
    res = client.get('/api/agents')
    assert res.status_code == 200
    data = res.get_json()
    assert isinstance(data, list)
    if len(data) > 0:
        agent = data[0]
        assert "name" in agent
        assert "status" in agent
        assert agent["status"] in ["idle", "working", "completed"]
