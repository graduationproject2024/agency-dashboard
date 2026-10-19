import sys
import os
import pytest
import gc
import shutil
import tempfile

# Ensure project root is in sys.path for test discovery
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import json
import db_persistence

@pytest.fixture(autouse=True)
def isolate_budget_for_tests(monkeypatch):
    """
    Hermetic test budget isolation fixture:
    Directs all budget.json persistence operations to an isolated temporary file.
    Guarantees that the repository's root budget.json and budget.json.bak are never touched by any test.
    """
    temp_dir = tempfile.mkdtemp(prefix="agy_test_budget_")
    test_budget = os.path.join(temp_dir, "budget_isolated.json")
    try:
        import app as agency_app
        initial_data = {
            "tokens_used": 0,
            "estimated_cost": 0.0,
            "budget_limit": 50.0,
            "transactions": []
        }
        with open(test_budget, "w", encoding="utf-8") as f:
            json.dump(initial_data, f)
        monkeypatch.setattr(agency_app, "BUDGET_FILE", test_budget)
    except Exception:
        pass
    try:
        yield test_budget
    finally:
        gc.collect()
        shutil.rmtree(temp_dir, ignore_errors=True)

@pytest.fixture(autouse=True)
def isolate_database_for_tests(monkeypatch):
    """
    Hermetic test database isolation fixture:
    Directs all SQLite persistence operations to an isolated temporary directory.
    Guarantees that no .db files are created in the tracked git workspace or pollute test tmp_path.
    """
    temp_dir = tempfile.mkdtemp(prefix="agy_test_db_")
    test_db = os.path.join(temp_dir, "test_persistence.db")
    monkeypatch.setenv("DATABASE_PATH", test_db)
    db_persistence.init_db(test_db)
    try:
        import app as agency_app
        with agency_app.state_lock:
            agency_app.workflow_state["session_id"] = None
    except Exception:
        pass
    try:
        yield test_db
    finally:
        gc.collect()
        shutil.rmtree(temp_dir, ignore_errors=True)

@pytest.fixture
def client():
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c



