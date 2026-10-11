import os
import json
import threading
import pytest
from app import load_budget, record_token_usage, budget_lock

def test_token_accounting_single_count():
    """Verify that token recording accurately increments tokens without duplication."""
    initial = load_budget()
    init_tokens = initial.get("tokens_used", 0)
    
    # Record 10,000 tokens
    updated = record_token_usage("اختبار الوكيل", "البرمجة", "مهمة اختبار", 10000, source="provider_reported")
    
    assert updated["tokens_used"] == init_tokens + 10000
    # Cost for 10,000 tokens at $0.15/1M = 0.0015
    assert updated["transactions"][0]["tokens"] == 10000
    assert updated["transactions"][0]["source"] == "provider_reported"
    assert updated["transactions"][0]["cost"] == 0.0015

def test_concurrent_budget_writes_thread_safety():
    """Verify that multiple concurrent threads writing to budget.json do not corrupt data or race."""
    initial = load_budget()
    init_tokens = initial.get("tokens_used", 0)
    
    threads = []
    increments = 10
    tokens_per_thread = 500
    
    def worker():
        record_token_usage("Worker", "DEV", "Concurrent Test", tokens_per_thread, source="estimated")
        
    for _ in range(increments):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
        
    final_budget = load_budget()
    expected_tokens = init_tokens + (increments * tokens_per_thread)
    assert final_budget["tokens_used"] == expected_tokens
