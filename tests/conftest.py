import sys
import os
import pytest

# Ensure project root is in sys.path for test discovery
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import db_persistence

import tempfile

@pytest.fixture(autouse=True)
def isolate_database_for_tests(monkeypatch):
    """
    Hermetic test database isolation fixture:
    Directs all SQLite persistence operations to an isolated temporary directory.
    Guarantees that no .db files are created in the tracked git workspace or pollute test tmp_path.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        test_db = os.path.join(temp_dir, "test_persistence.db")
        monkeypatch.setenv("DATABASE_PATH", test_db)
        db_persistence.init_db(test_db)
        try:
            import app as agency_app
            with agency_app.state_lock:
                agency_app.workflow_state["session_id"] = None
        except Exception:
            pass
        yield test_db

