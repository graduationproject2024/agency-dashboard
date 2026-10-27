"""
Phase 2.8 P0 Release-Gate Hardening & Revision Binding Regression Test Suite
Validates the 20 mandatory security requirements from Phase 2.8 specification:
1. Manifest missing validation_status -> blocked.
2. Manifest with a non-success status -> blocked.
3. Manifest missing qa_run_id -> blocked.
4. Manifest containing an unknown or nonexistent QA run ID -> blocked.
5. Caller-forged success manifest with no authoritative persisted QA record -> blocked.
6. Missing candidate_files -> blocked.
7. Empty candidate content cannot bypass verification.
8. Candidate content digest mismatch -> blocked.
9. Candidate revision differs from QA revision -> blocked.
10. Manifest revision differs from persisted QA revision -> blocked.
11. QA run belongs to a different session -> blocked.
12. QA run is incomplete or failed in persistence -> blocked.
13. Required QA evidence is missing -> blocked.
14. Finding from revision A cannot be resolved by a run for revision B.
15. Finding from another task/session cannot be resolved.
16. Bulk resolution cannot close unrelated findings.
17. Malformed manifests and unexpected data types safely blocked.
18. Persistence lookup errors fail closed.
19. Repeated finding resolution remains idempotent.
20. A valid, trusted QA run for the exact candidate revision passes.
"""
import copy
import hashlib
import json
import os
import sqlite3
import pytest
from unittest.mock import patch

import db_persistence as db_store
from app import (
    app,
    evaluate_release_gate,
    run_qa_checks,
    compute_artifact_revision,
    build_artifact_manifest
)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Hermetic isolated SQLite database fixture."""
    db_file = str(tmp_path / "test_p0_hardening.db")
    monkeypatch.setenv("DATABASE_PATH", db_file)
    db_store.init_db(db_file)
    return db_file


VALID_HTML = "<!DOCTYPE html><html><head><title>App</title><link rel='stylesheet' href='style.css'></head><body><h1>App</h1><script src='script.js'></script></body></html>"
VALID_CSS = "body { margin: 0; padding: 0; }"
VALID_JS = "console.log('clean app');"


def _make_candidate_files():
    return {
        "index.html": VALID_HTML,
        "style.css": VALID_CSS,
        "script.js": VALID_JS
    }


# =========================================================================
# 1. Manifest missing validation_status -> blocked
# =========================================================================
def test_req01_manifest_missing_validation_status_blocks(temp_db):
    sess_id = db_store.create_session("Req01 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()
    rep = run_qa_checks(cand["index.html"], cand["style.css"], cand["script.js"], session_id=sess_id, db_path=temp_db)
    assert rep["passed"] is True

    manifest = copy.deepcopy(rep["manifest"])
    manifest.pop("validation_status", None)

    res = evaluate_release_gate(sess_id, candidate_files=cand, artifact_manifest=manifest, db_path=temp_db)
    assert res["allowed"] is False
    assert res["status"] == "blocked"
    assert any("validation_status" in r for r in res["reasons"])


# =========================================================================
# 2. Manifest with a non-success status -> blocked
# =========================================================================
def test_req02_manifest_non_success_status_blocks(temp_db):
    sess_id = db_store.create_session("Req02 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()
    rep = run_qa_checks(cand["index.html"], cand["style.css"], cand["script.js"], session_id=sess_id, db_path=temp_db)

    for bad_status in ["failed", "pending", "warning", "unknown", "error"]:
        manifest = copy.deepcopy(rep["manifest"])
        manifest["validation_status"] = bad_status

        res = evaluate_release_gate(sess_id, candidate_files=cand, artifact_manifest=manifest, db_path=temp_db)
        assert res["allowed"] is False
        assert res["status"] == "blocked"
        assert any("validation_status" in r or "verification checks failed" in r for r in res["reasons"])


# =========================================================================
# 3. Manifest missing qa_run_id -> blocked
# =========================================================================
def test_req03_manifest_missing_qa_run_id_blocks(temp_db):
    sess_id = db_store.create_session("Req03 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()
    rep = run_qa_checks(cand["index.html"], cand["style.css"], cand["script.js"], session_id=sess_id, db_path=temp_db)

    # Missing entirely
    m1 = copy.deepcopy(rep["manifest"])
    m1.pop("qa_run_id", None)
    res1 = evaluate_release_gate(sess_id, candidate_files=cand, artifact_manifest=m1, db_path=temp_db)
    assert res1["allowed"] is False
    assert any("qa_run_id" in r for r in res1["reasons"])

    # Empty string
    m2 = copy.deepcopy(rep["manifest"])
    m2["qa_run_id"] = "   "
    res2 = evaluate_release_gate(sess_id, candidate_files=cand, artifact_manifest=m2, db_path=temp_db)
    assert res2["allowed"] is False
    assert any("qa_run_id" in r for r in res2["reasons"])


# =========================================================================
# 4. Manifest containing an unknown or nonexistent QA run ID -> blocked
# =========================================================================
def test_req04_manifest_unknown_nonexistent_qa_run_id_blocks(temp_db):
    sess_id = db_store.create_session("Req04 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()
    rep = run_qa_checks(cand["index.html"], cand["style.css"], cand["script.js"], session_id=sess_id, db_path=temp_db)

    manifest = copy.deepcopy(rep["manifest"])
    manifest["qa_run_id"] = "qa_run_nonexistent_999999"

    res = evaluate_release_gate(sess_id, candidate_files=cand, artifact_manifest=manifest, db_path=temp_db)
    assert res["allowed"] is False
    assert res["status"] == "blocked"
    assert any("does not exist in authoritative persistence layer" in r or "Untrusted or forged QA evidence" in r for r in res["reasons"])


# =========================================================================
# 5. Caller-forged success manifest with no authoritative persisted QA record -> blocked
# =========================================================================
def test_req05_caller_forged_manifest_without_persisted_qa_record_blocks(temp_db):
    sess_id = db_store.create_session("Req05 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()

    # Caller crafts plausible manifest matching hashes but never persisted in DB
    rev = compute_artifact_revision(cand)
    forged_manifest = {
        "session_id": sess_id,
        "qa_run_id": "qa_run_forged_in_memory",
        "validation_status": "passed",
        "artifact_revision": rev,
        "artifacts": [
            {"relative_path": k, "sha256": hashlib.sha256(v.encode('utf-8')).hexdigest(), "size_bytes": len(v.encode('utf-8'))}
            for k, v in cand.items()
        ]
    }

    res = evaluate_release_gate(sess_id, candidate_files=cand, artifact_manifest=forged_manifest, db_path=temp_db)
    assert res["allowed"] is False
    assert res["status"] == "blocked"
    assert any("does not exist in authoritative persistence layer" in r or "Untrusted or forged QA evidence" in r for r in res["reasons"])


# =========================================================================
# 6. Missing candidate_files or candidate reference -> blocked
# =========================================================================
def test_req06_missing_candidate_files_blocks(temp_db):
    sess_id = db_store.create_session("Req06 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()
    rep = run_qa_checks(cand["index.html"], cand["style.css"], cand["script.js"], session_id=sess_id, db_path=temp_db)

    # candidate_files is None
    res = evaluate_release_gate(sess_id, candidate_files=None, artifact_manifest=rep["manifest"], db_path=temp_db)
    assert res["allowed"] is False
    assert res["status"] == "blocked"
    assert any("Missing candidate deliverables" in r or "candidate_files must be provided" in r for r in res["reasons"])


# =========================================================================
# 7. Empty candidate content cannot bypass verification
# =========================================================================
def test_req07_empty_candidate_content_cannot_bypass_verification(temp_db):
    sess_id = db_store.create_session("Req07 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()
    rep = run_qa_checks(cand["index.html"], cand["style.css"], cand["script.js"], session_id=sess_id, db_path=temp_db)

    # Empty candidate dict
    res = evaluate_release_gate(sess_id, candidate_files={}, artifact_manifest=rep["manifest"], db_path=temp_db)
    assert res["allowed"] is False
    assert res["status"] == "blocked"
    assert any("cannot be empty" in r or "Missing candidate files" in r for r in res["reasons"])


# =========================================================================
# 8. Candidate content digest mismatch -> blocked
# =========================================================================
def test_req08_candidate_content_digest_mismatch_blocks(temp_db):
    sess_id = db_store.create_session("Req08 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()
    rep = run_qa_checks(cand["index.html"], cand["style.css"], cand["script.js"], session_id=sess_id, db_path=temp_db)

    tampered_cand = copy.deepcopy(cand)
    tampered_cand["index.html"] = "<!DOCTYPE html><html><head><title>Tampered</title></head><body>Injected</body></html>"

    res = evaluate_release_gate(sess_id, candidate_files=tampered_cand, artifact_manifest=rep["manifest"], db_path=temp_db)
    assert res["allowed"] is False
    assert res["status"] == "blocked"
    assert any("digest mismatch" in r or "revision mismatch" in r for r in res["reasons"])


# =========================================================================
# 9. Candidate revision differs from QA revision -> blocked
# =========================================================================
def test_req09_candidate_revision_differs_from_qa_revision_blocks(temp_db):
    sess_id = db_store.create_session("Req09 Session", "Task", db_path=temp_db)
    cand_rev_a = _make_candidate_files()
    rep = run_qa_checks(cand_rev_a["index.html"], cand_rev_a["style.css"], cand_rev_a["script.js"], session_id=sess_id, db_path=temp_db)

    cand_rev_b = copy.deepcopy(cand_rev_a)
    cand_rev_b["script.js"] = "console.log('revision B changed logic');"

    res = evaluate_release_gate(sess_id, candidate_files=cand_rev_b, artifact_manifest=rep["manifest"], db_path=temp_db)
    assert res["allowed"] is False
    assert res["status"] == "blocked"
    assert any("revision mismatch" in r for r in res["reasons"])


# =========================================================================
# 10. Manifest revision differs from persisted QA revision -> blocked
# =========================================================================
def test_req10_manifest_revision_differs_from_persisted_qa_revision_blocks(temp_db):
    sess_id = db_store.create_session("Req10 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()
    rep = run_qa_checks(cand["index.html"], cand["style.css"], cand["script.js"], session_id=sess_id, db_path=temp_db)

    manifest = copy.deepcopy(rep["manifest"])
    manifest["artifact_revision"] = "f" * 64  # forged revision claim

    res = evaluate_release_gate(sess_id, candidate_files=cand, artifact_manifest=manifest, db_path=temp_db)
    assert res["allowed"] is False
    assert res["status"] == "blocked"
    assert any("revision mismatch" in r for r in res["reasons"])


# =========================================================================
# 11. QA run belongs to a different project/task/session -> blocked
# =========================================================================
def test_req11_qa_run_different_session_blocks(temp_db):
    sess1 = db_store.create_session("Session 1", "Task", db_path=temp_db)
    sess2 = db_store.create_session("Session 2", "Task", db_path=temp_db)
    cand = _make_candidate_files()

    rep = run_qa_checks(cand["index.html"], cand["style.css"], cand["script.js"], session_id=sess1, db_path=temp_db)

    # Attempt to use session 1's QA run in session 2's release gate
    res = evaluate_release_gate(sess2, candidate_files=cand, artifact_manifest=rep["manifest"], db_path=temp_db)
    assert res["allowed"] is False
    assert res["status"] == "blocked"
    assert any("belongs to session" in r or "QA evidence candidate mismatch" in r for r in res["reasons"])


# =========================================================================
# 12. QA run is incomplete or failed in persistence -> blocked
# =========================================================================
def test_req12_qa_run_incomplete_or_failed_blocks(temp_db):
    sess_id = db_store.create_session("Req12 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()
    rev = compute_artifact_revision(cand)

    # Persist a run with status 'failed'
    failed_run_id = "qa_run_failed_123"
    db_store.record_qa_run(
        qa_run_id=failed_run_id,
        session_id=sess_id,
        artifact_revision=rev,
        validation_status="failed",
        errors=["Syntax failure in test"],
        db_path=temp_db
    )

    # Caller crafts manifest claiming passed
    manifest = {
        "session_id": sess_id,
        "qa_run_id": failed_run_id,
        "validation_status": "passed",
        "artifact_revision": rev,
        "artifacts": [
            {"relative_path": k, "sha256": hashlib.sha256(v.encode('utf-8')).hexdigest(), "size_bytes": len(v.encode('utf-8'))}
            for k, v in cand.items()
        ]
    }

    res = evaluate_release_gate(sess_id, candidate_files=cand, artifact_manifest=manifest, db_path=temp_db)
    assert res["allowed"] is False
    assert res["status"] == "blocked"
    assert any("failed or incomplete" in r for r in res["reasons"])


# =========================================================================
# 13. Required QA evidence is missing -> blocked
# =========================================================================
def test_req13_required_qa_evidence_missing_blocks(temp_db):
    sess_id = db_store.create_session("Req13 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()

    # Pass candidate files but omit artifact_manifest
    res = evaluate_release_gate(sess_id, candidate_files=cand, artifact_manifest=None, db_path=temp_db)
    assert res["allowed"] is False
    assert res["status"] == "blocked"
    assert any("Missing artifact manifest" in r for r in res["reasons"])


# =========================================================================
# 14. Finding from revision A cannot be resolved by a run for revision B
# =========================================================================
def test_req14_finding_revision_a_not_resolved_by_revision_b(temp_db):
    sess_id = db_store.create_session("Req14 Session", "Task", db_path=temp_db)
    cand_rev_a = _make_candidate_files()
    rev_a = compute_artifact_revision(cand_rev_a)

    fid = db_store.record_qa_finding(
        session_id=sess_id,
        task_id=None,
        qa_run_id="run_a",
        severity="high",
        category="syntax",
        artifact_name="index.html",
        evidence="Missing doctype in Rev A",
        status="open",
        artifact_revision=rev_a,
        db_path=temp_db
    )

    # Run QA on Revision B with different JS/CSS
    cand_rev_b = copy.deepcopy(cand_rev_a)
    cand_rev_b["script.js"] = "console.log('Rev B content');"

    report = run_qa_checks(
        app_html=cand_rev_b["index.html"],
        app_css=cand_rev_b["style.css"],
        app_js=cand_rev_b["script.js"],
        session_id=sess_id,
        verified_finding_ids=[fid],
        db_path=temp_db
    )

    assert report["passed"] is True
    assert fid in report["unresolved_findings"]
    assert fid not in report["resolved_findings"]

    finding_record = db_store.get_qa_finding(fid, db_path=temp_db)
    assert finding_record["status"] == "open"


# =========================================================================
# 15. Finding from another task/session cannot be resolved
# =========================================================================
def test_req15_finding_from_another_session_cannot_be_resolved(temp_db):
    sess1 = db_store.create_session("Session 1", "Task", db_path=temp_db)
    sess2 = db_store.create_session("Session 2", "Task", db_path=temp_db)
    cand = _make_candidate_files()

    fid_sess1 = db_store.record_qa_finding(
        session_id=sess1,
        task_id=None,
        qa_run_id="run_sess1",
        severity="high",
        category="syntax",
        artifact_name="index.html",
        evidence="Defect in session 1",
        status="open",
        db_path=temp_db
    )

    # QA runs under session 2, attempts to resolve fid_sess1
    report = run_qa_checks(
        app_html=cand["index.html"],
        app_css=cand["style.css"],
        app_js=cand["script.js"],
        session_id=sess2,
        verified_finding_ids=[fid_sess1],
        db_path=temp_db
    )

    assert fid_sess1 in report["unresolved_findings"]
    f1 = db_store.get_qa_finding(fid_sess1, db_path=temp_db)
    assert f1["status"] == "open"


# =========================================================================
# 16. Bulk resolution cannot close unrelated findings
# =========================================================================
def test_req16_bulk_resolution_cannot_close_unrelated_findings(temp_db):
    sess_id = db_store.create_session("Req16 Session", "Task", db_path=temp_db)

    fid_html = db_store.record_qa_finding(
        session_id=sess_id, task_id=None, qa_run_id="r1",
        severity="medium", category="syntax", artifact_name="index.html",
        evidence="Issue in html", status="open", db_path=temp_db
    )
    fid_backend = db_store.record_qa_finding(
        session_id=sess_id, task_id=None, qa_run_id="r1",
        severity="high", category="security", artifact_name="backend_api.py",
        evidence="SQL injection in unverified backend file", status="open", db_path=temp_db
    )
    fid_db = db_store.record_qa_finding(
        session_id=sess_id, task_id=None, qa_run_id="r1",
        severity="critical", category="security", artifact_name="schema.sql",
        evidence="Plaintext password column", status="open", db_path=temp_db
    )

    # Run QA that verifies only frontend deliverables
    cand = _make_candidate_files()
    report = run_qa_checks(
        app_html=cand["index.html"],
        app_css=cand["style.css"],
        app_js=cand["script.js"],
        session_id=sess_id,
        db_path=temp_db
    )

    assert fid_backend in report["unresolved_findings"]
    assert fid_db in report["unresolved_findings"]
    assert db_store.get_qa_finding(fid_backend, db_path=temp_db)["status"] == "open"
    assert db_store.get_qa_finding(fid_db, db_path=temp_db)["status"] == "open"


# =========================================================================
# 17. Malformed manifests and unexpected data types safely blocked
# =========================================================================
def test_req17_malformed_manifests_and_unexpected_types_safely_blocked(temp_db):
    sess_id = db_store.create_session("Req17 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()

    malformed_inputs = [
        ("string_manifest", "not_a_dict"),
        ("int_manifest", 12345),
        ("list_manifest", [{"artifacts": []}]),
        ("none_manifest", None),
        ("manifest_artifacts_not_list", {"artifacts": "not_a_list", "qa_run_id": "r1", "validation_status": "passed"}),
        ("manifest_artifacts_entry_not_dict", {"artifacts": ["string_item"], "qa_run_id": "r1", "validation_status": "passed"}),
    ]

    for label, bad_m in malformed_inputs:
        res = evaluate_release_gate(sess_id, candidate_files=cand, artifact_manifest=bad_m, db_path=temp_db)
        assert res["allowed"] is False, f"Failed on {label}"
        assert res["status"] == "blocked"

    # Malformed candidate_files
    bad_cands = [
        ("string_cand", "not_a_dict"),
        ("list_cand", ["index.html"]),
        ("cand_has_none_value", {"index.html": None}),
        ("cand_missing_index_html", {"style.css": VALID_CSS}),
    ]
    rep = run_qa_checks(cand["index.html"], cand["style.css"], cand["script.js"], session_id=sess_id, db_path=temp_db)
    for label, bad_c in bad_cands:
        res = evaluate_release_gate(sess_id, candidate_files=bad_c, artifact_manifest=rep["manifest"], db_path=temp_db)
        assert res["allowed"] is False, f"Failed on {label}"
        assert res["status"] == "blocked"


# =========================================================================
# 18. Persistence lookup errors -> fail closed
# =========================================================================
def test_req18_persistence_lookup_errors_fail_closed(temp_db):
    sess_id = db_store.create_session("Req18 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()
    rep = run_qa_checks(cand["index.html"], cand["style.css"], cand["script.js"], session_id=sess_id, db_path=temp_db)

    with patch("db_persistence.get_qa_run", side_effect=sqlite3.OperationalError("Database locked")):
        res = evaluate_release_gate(sess_id, candidate_files=cand, artifact_manifest=rep["manifest"], db_path=temp_db)
        assert res["allowed"] is False
        assert res["status"] == "blocked"
        assert any("Persistence or evidence-retrieval failure" in r for r in res["reasons"])


# =========================================================================
# 19. Repeated finding resolution remains idempotent
# =========================================================================
def test_req19_repeated_finding_resolution_remains_idempotent(temp_db):
    sess_id = db_store.create_session("Req19 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()
    rev = compute_artifact_revision(cand)

    fid = db_store.record_qa_finding(
        session_id=sess_id,
        task_id=None,
        qa_run_id="run_1",
        severity="medium",
        category="syntax",
        artifact_name="index.html",
        evidence="Old finding to resolve",
        status="open",
        artifact_revision=rev,
        db_path=temp_db
    )

    # First QA run resolves it
    rep1 = run_qa_checks(
        cand["index.html"], cand["style.css"], cand["script.js"],
        session_id=sess_id, verified_finding_ids=[fid], db_path=temp_db
    )
    assert fid in rep1["resolved_findings"]
    f1 = db_store.get_qa_finding(fid, db_path=temp_db)
    assert f1["status"] == "verified_resolved"
    assert len(f1["verification_history"]) == 1

    # Second QA run does not create duplicate history
    rep2 = run_qa_checks(
        cand["index.html"], cand["style.css"], cand["script.js"],
        session_id=sess_id, verified_finding_ids=[fid], db_path=temp_db
    )
    f2 = db_store.get_qa_finding(fid, db_path=temp_db)
    assert f2["status"] == "verified_resolved"
    assert len(f2["verification_history"]) == 1


# =========================================================================
# 20. A valid, trusted QA run for exact candidate revision passes
# =========================================================================
def test_req20_valid_trusted_qa_run_for_exact_candidate_revision_passes(temp_db):
    sess_id = db_store.create_session("Req20 Session", "Task", db_path=temp_db)
    cand = _make_candidate_files()

    rep = run_qa_checks(
        cand["index.html"],
        cand["style.css"],
        cand["script.js"],
        session_id=sess_id,
        db_path=temp_db
    )
    assert rep["passed"] is True
    assert rep["manifest"] is not None

    res = evaluate_release_gate(
        sess_id,
        candidate_files=cand,
        artifact_manifest=rep["manifest"],
        db_path=temp_db
    )
    assert res["allowed"] is True
    assert res["status"] == "passed"
    assert res["reasons"] == []
    assert res["blocking_findings_count"] == 0
