import os
import tempfile
import pytest
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

def test_discover_agents_empty_directory():
    """Verify that an empty directory returns total 0 and status 'empty'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = discover_agents(tmpdir)
        assert result["total"] == 0
        assert result["status"] == "empty"
        assert result["agents"] == []

def test_discover_agents_filters_ignored_files_and_git():
    """Verify that non-markdown, README, and .git directory contents are excluded."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create department subfolder
        dept_dir = os.path.join(tmpdir, "engineering")
        os.makedirs(dept_dir, exist_ok=True)
        
        # Valid agent
        with open(os.path.join(dept_dir, "software-architect.md"), "w", encoding="utf-8") as f:
            f.write("# Architect Agent")
            
        # Ignored files
        with open(os.path.join(dept_dir, "README.md"), "w", encoding="utf-8") as f:
            f.write("# Readme")
        with open(os.path.join(dept_dir, "script.py"), "w", encoding="utf-8") as f:
            f.write("print(1)")
            
        # .git folder
        git_dir = os.path.join(tmpdir, ".git")
        os.makedirs(git_dir, exist_ok=True)
        with open(os.path.join(git_dir, "config.md"), "w", encoding="utf-8") as f:
            f.write("git config")

        result = discover_agents(tmpdir)
        assert result["total"] == 1
        assert result["agents"][0]["id"] == "software-architect"
        assert result["agents"][0]["department"] == "engineering"

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
