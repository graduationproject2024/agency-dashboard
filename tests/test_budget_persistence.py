import os
import json
import time
import pytest
import threading
from unittest.mock import patch

from app import (
    load_budget,
    save_budget,
    record_token_usage,
    atomic_write_json,
    load_settings,
    manage_settings,
    BUDGET_FILE,
    SETTINGS_FILE
)

@pytest.fixture
def clean_budget_env(tmp_path, monkeypatch):
    test_budget = str(tmp_path / "budget_test.json")
    monkeypatch.setattr("app.BUDGET_FILE", test_budget)
    return test_budget

@pytest.fixture
def clean_settings_env(tmp_path, monkeypatch):
    test_settings = str(tmp_path / "settings_test.json")
    monkeypatch.setattr("app.SETTINGS_FILE", test_settings)
    return test_settings

def test_atomic_write_json_creates_valid_file(tmp_path):
    target_file = str(tmp_path / "atomic_test.json")
    test_data = {"key": "value", "numbers": [1, 2, 3]}
    
    atomic_write_json(target_file, test_data)
    
    assert os.path.exists(target_file)
    with open(target_file, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded == test_data
    
    # Ensure no leftover tmp files
    files = os.listdir(str(tmp_path))
    assert files == ["atomic_test.json"]

def test_load_budget_default_when_missing(clean_budget_env):
    budget = load_budget()
    assert budget["tokens_used"] == 0
    assert budget["estimated_cost"] == 0.0
    assert budget["budget_limit"] == 50.0
    assert budget["transactions"] == []

def test_save_and_load_budget(clean_budget_env):
    data = {
        "tokens_used": 5000,
        "estimated_cost": 0.0008,
        "budget_limit": 50.0,
        "transactions": [{"id": 1, "agent": "Engineer", "tokens": 5000, "cost": 0.0008}]
    }
    save_budget(data)
    loaded = load_budget()
    assert loaded["tokens_used"] == 5000
    assert loaded["estimated_cost"] == 0.0008
    assert len(loaded["transactions"]) == 1

def test_record_token_usage_increments_and_tags_source(clean_budget_env):
    budget_after = record_token_usage(
        agent_name="Software Architect",
        dept_name="engineering",
        task_name="Scaffold backend API",
        tokens_count=2000,
        source="provider_reported"
    )
    
    assert budget_after["tokens_used"] == 2000
    assert budget_after["estimated_cost"] > 0
    assert len(budget_after["transactions"]) == 1
    tx = budget_after["transactions"][0]
    assert tx["agent"] == "Software Architect"
    assert tx["department"] == "engineering"
    assert tx["tokens"] == 2000
    assert tx["source"] == "provider_reported"
    
    # Reload from disk to verify persistence
    disk_budget = load_budget()
    assert disk_budget["tokens_used"] == 2000

def test_budget_corrupted_json_restores_from_backup(clean_budget_env):
    initial_data = {
        "tokens_used": 15000,
        "estimated_cost": 0.0023,
        "budget_limit": 50.0,
        "transactions": [{"id": 101, "tokens": 15000}]
    }
    # Save legitimate budget (this creates clean_budget_env.bak on subsequent write)
    save_budget(initial_data)
    # Save updated budget to trigger creation of .bak
    initial_data["tokens_used"] = 20000
    save_budget(initial_data)
    
    bak_file = clean_budget_env + ".bak"
    assert os.path.exists(bak_file)
    
    # Intentionally corrupt the primary budget file
    with open(clean_budget_env, "w", encoding="utf-8") as f:
        f.write("{corrupt_json_half_written: true,")
        
    recovered = load_budget()
    # It must restore from backup rather than silently resetting to 0!
    assert recovered["tokens_used"] in (15000, 20000)
    assert recovered["tokens_used"] > 0

def test_budget_corrupted_json_without_backup_preserves_corrupt_file(clean_budget_env):
    # Write corrupted file directly with NO .bak file
    with open(clean_budget_env, "w", encoding="utf-8") as f:
        f.write("NOT_A_JSON_STRING_CRASH")
        
    budget = load_budget()
    # Returns default fallback
    assert budget["tokens_used"] == 0
    
    # But crucially, the corrupted file was preserved with .corrupt suffix for forensic review
    parent_dir = os.path.dirname(clean_budget_env)
    files = os.listdir(parent_dir)
    corrupted_preserved = [f for f in files if ".corrupt." in f]
    assert len(corrupted_preserved) >= 1, "Corrupted budget was silently destroyed without forensic preservation!"

def test_concurrent_token_updates(clean_budget_env):
    threads = []
    num_threads = 8
    tokens_per_thread = 500
    
    def worker(idx):
        record_token_usage(
            agent_name=f"Worker-{idx}",
            dept_name="engineering",
            task_name=f"Concurrent task {idx}",
            tokens_count=tokens_per_thread,
            source="provider_reported"
        )
        
    for i in range(num_threads):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join(timeout=5)
        
    final_budget = load_budget()
    expected_total = num_threads * tokens_per_thread
    assert final_budget["tokens_used"] == expected_total, (
        f"Expected {expected_total} tokens, found {final_budget['tokens_used']}. Race condition detected!"
    )
    assert len(final_budget["transactions"]) == num_threads
