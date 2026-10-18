# Phase 2.4.0 — Owner-Directed Objective Discovery & Pre-Implementation Planning Report

**Repository:** `graduationproject2024/agency-dashboard`  
**Workspace:** `C:\Users\sam20\agency-dashboard`  
**Active Branch:** `master`  
**Verified Baseline HEAD:** `841131c132308131c1ba972061cf4b7c469f3bc9`  
**Operating Mode:** Strict Read-Only Discovery and Planning  
**Authorization Status:** Planning authorized; implementation **NOT** authorized.

---

## 1. Baseline Verification

The repository baseline was inspected using read-only Git commands and cryptographic hash verification:

| Check | Expected Baseline | Observed Reality | Status |
|---|---|---|:---:|
| **Repository Root** | `C:\Users\sam20\agency-dashboard` | `C:/Users/sam20/agency-dashboard` | **PASS** |
| **Active Branch** | `master` | `master` | **PASS** |
| **Local HEAD SHA** | `841131c132308131c1ba972061cf4b7c469f3bc9` | `841131c132308131c1ba972061cf4b7c469f3bc9` | **PASS** |
| **Local Remote Ref** | `841131c132308131c1ba972061cf4b7c469f3bc9` | `origin/master` matches local HEAD exactly | **PASS** |
| **Tracked Cleanliness** | 0 tracked / staged modifications | 0 tracked modifications (`git status --porcelain` has 0 tracked diffs) | **PASS** |
| **Linear Ancestry** | Descends cleanly from `f4231b4` | `f4231b4` -> `a9b2f26` -> `4d9b7e4` -> `841131c` verified | **PASS** |

### Option B Protected Data Fingerprint Verification
Measured via PowerShell `Get-FileHash -Algorithm SHA256` and `Get-Item`:
- `budget.json`: 14,313 B | SHA-256 `f81988bc0de8083b46532baeb199834a4bfac4b991104b6b77b1733f0756ee16` (**100% Invariant**)
- `budget.json.bak`: 14,919 B | SHA-256 `5db2a3c506deb4c0e6d55478e1efad1c2adce14c7b193f290f90f814e2d56e59` (**100% Invariant**)
- `settings.json`: 44 B | SHA-256 `2f4e6d39b3e8ac781c04ca9d14983f1a1848b6ef1a2be5db0b258bf56f106c94` (**100% Invariant**)

### Untracked Item Preservation
All existing untracked workspace entries remain untouched:
- `generated_apps/make_tetris_game/.opencode/`
- `reports/PHASE_2_3_8_INDEPENDENT_PLAN_REVIEW_AND_SAFETY_GATE.md`
- `reports/PHASE_2_3_9_INDEPENDENT_CI_GUARD_AND_TEST_ISOLATION_REVIEW.md`
- `reports/PHASE_2_3_9_1_FINAL_READ_ONLY_PLAN_VALIDATION.md`
- `reports/PHASE_2_3_12_CI_WORKFLOW_TRIGGER_AND_INTEGRITY_GATE_HARDENING.md`
- `reports/PHASE_2_3_15_GITHUB_SYNCHRONIZATION_AND_CI_VERIFICATION.md`
- `reports/PHASE_2_3_16_INDEPENDENT_POST_RELEASE_AUDIT_AND_SECURITY_CLOSURE.md`
- `reports/PHASE_2_3_17_FORMAL_SECURITY_CLOSURE_AND_PHASE_2_4_READINESS_ASSESSMENT.md`

---

## 2. Current Product and Architecture State

The repository was systematically audited across source files (`app.py`, `index.html`, `script.js`, `style.css`), prompt repositories (`c:\Users\sam20\agency-agents`), and architecture manuals (`AGENTS_AGENCY_FULL_DOCUMENTATION.md`).

```
+-------------------------------------------------------------------------------------------------------+
|                                        Client Browser Dashboard                                       |
|  - Glassmorphic dark UI (HTML5 + Vanilla CSS + ES6 JS) with polling every 1.5s (/api/state)          |
|  - Real-time Activity Feed, Department Statuses, Approvals Modal, Projects & Token Ledger View        |
+---------------------------------------------------+---------------------------------------------------+
                                                    | HTTP REST
                                                    v
+-------------------------------------------------------------------------------------------------------+
|                                    Flask Application (`app.py`)                                       |
|  - Web Server & REST Endpoints (lines 1498–1735)                                                      |
|  - Security Layer: Origin validation, sensitive file blocks, MAX_PATH folder safety, path containment  |
|  - Threading: Background daemon worker per task (`run_workflow`, `continue_workflow`)                 |
|  - State: Single in-memory dictionary `workflow_state` protected by RLock (`state_lock`)              |
|  - Financial Ledger: `budget.json` updated dynamically via `record_token_usage()`                     |
|  - QA Gate: Strict HTML stack parser, JS Node syntax validator, CSS block validator                   |
|  - Persistence & Rollback: Atomic writes, `.bak` snapshotting, byte-for-byte post-write verification  |
+---------------------------------------------------+---------------------------------------------------+
                                                    |
         +------------------------------------------+------------------------------------------+
         |                                          |                                          |
         v                                          v                                          v
+------------------------+             +------------------------+             +------------------------+
| Google Gemini Direct   |             | Local OpenCode Engine  |             | OpenRouter Gateway     |
| (Multi-model failover) |             | (HTTP 127.0.0.1:4096)  |             | (OpenAI / Anthropic)   |
+------------------------+             +------------------------+             +------------------------+
```

### Key Architectural Strengths
1. **Hardened Security Perimeter:** Comprehensive defense-in-depth implemented across Phases 1 and 2—including origin validation (`is_safe_origin`), directory traversal and symlink containment (`safe_resolve_project_dir`), secret masking (`get_safe_settings_view`), and Windows device name blocks.
2. **Atomic Artifact Delivery & Rollback:** Staging with pre-existing backup preservation (`_stage_write_file`), strict QA validation before write, byte-for-byte candidate identity assertions, and rollback recovery if any deliverable fails write verification (`app.py:1360-1484`).
3. **CI Containment Gate:** Hosted GitHub Actions workflow (`.github/workflows/symlink_security_verify.yml`) captures pre-test manifest and enforces post-test zero persistent-file mutation under `if: always()`.

---

## 3. Completed vs. Incomplete Objectives

To eliminate ambiguity between documented aspirations and technical reality, every major subsystem was classified into five explicit evidence tiers:

| Subsystem / Capability | Classification | Direct Concrete Evidence | Notes & Gaps |
|---|---|---|---|
| **Symlink & Path Traversal Containment** | **Verified completed** | `safe_resolve_project_dir()` using `realpath` + `commonpath`; verified by 21 tests in `test_adversarial_paths.py` and live Ubuntu CI run `38609009740`. | Fully contained for all generated app asset requests and folder opening. |
| **Atomic Deliverable Rollback** | **Verified completed** | `app.py:1445-1484`; 10 dedicated tests in `test_adversarial_rollback.py`. | Restores pre-existing files, deletes partially written deliverables, surfaces errors honestly. |
| **QA Structural Validation Gate** | **Verified completed** | `StrictHTMLStructureValidator`, `verify_js_syntax`, `validate_css_structure`. | Fails closed on syntax errors, tag mismatch, unbalanced braces, or placeholder code. |
| **Token Consumption & Budget Ledger** | **Verified completed** | `budget.json`, `load_budget()`, `save_budget()`, `record_token_usage()`. | Dynamic atomic accounting ledger capped at 60 transactions with backup recovery. |
| **Windows MAX_PATH Safe Packaging** | **Verified completed** | `get_project_folder_name()` enforces 35-character ceiling and strips invalid path characters. | Prevents `[WinError 206]` path overflows on massive prompt inputs. |
| **Multi-Provider LLM Integration** | **Verified completed** | `call_agent()` supports Gemini (direct REST), OpenCode (local 4096), and OpenRouter with auto-rescue fallback. | Honest failure when API key is missing (never fakes success). |
| **282-Agent Dynamic Squad Assembly** | **Partially implemented** (Documented vs. Actual Gap) | `discover_agents()` finds 299 markdown agent roles in `c:\Users\sam20\agency-agents`. However, `run_workflow()` and `continue_workflow()` execute a **single, rigid 8-agent linear pipeline**. | The other ~290 specialized roles (e.g., `fastapi-expert`, `postgres-pro`, `security-reviewer`, `game-developer`) are **never dynamically dispatched**. The UI roster simulates "working" status by simple keyword substring matching (`app.py:1644-1690`). |
| **Multi-Project Concurrency & Durability** | **Partially implemented** | `workflow_state` is a single in-memory dictionary. Any concurrent task request while running is rejected with HTTP 409 (`app.py:1535`). | Server restart clears running state. No persistent database for task histories or past PRDs. |
| **Multi-Stage Human-in-the-Loop Approvals** | **Partially implemented** | Exactly one approval gate exists (`app.py:797` between Product PRD and Engineering Architecture). | No approval gate for Engineering Architecture before code generation, and no gate for QA audit review before client delivery. |
| **Closed-Loop QA Remediation Loop** | **Not implemented** | When QA validation fails (`app.py:1344-1353`), the workflow immediately sets `status = "failed"` and aborts. | No iterative repair agent attempts to fix syntax or tag errors before failing the run. |
| **Full-Stack / Multi-File Project Outputs** | **Not implemented** | Code extraction strictly searches for ````html````, ````css````, and ````javascript```` blocks (`app.py:1320-1334`), outputting only `index.html`, `style.css`, and `script.js`. | Cannot generate Python/Node backend servers, database migration scripts, Dockerfiles, or multi-page apps. |
| **Real External LLM Browser End-to-End Test** | **Documented but unverified** | Browser tests in `test_browser_smoke.py` use mocked agent outputs. | Live generation via real API keys was tested manually during development but not as an automated CI regression fixture. |

---

## 4. Roadmap and Dependency Findings

1. **Documented Roadmap Reality:**  
   In `reports/PHASE_2_3_CROSS_PLATFORM_SYMLINK_VERIFICATION_AND_SECURITY_GATE.md:149`, the original roadmap stated: *"No Phase 2.4 needed unless the symlink replay is required for release authorization."*  
   Because the symlink replay and zero-skip enforcement were completed and verified in Phase 2.3.15–2.3.16, **no pre-existing specification for Phase 2.4 exists in repository documentation**.
2. **Foundational Readiness:**  
   The platform's underlying file system, path handling, atomic rollback, and CI guard controls are complete and solid. The platform is ready to transition from **Security & Infrastructure Hardening** to **Agency Product Capabilities & Autonomous Execution**.
3. **Core Architectural Bottlenecks:**  
   - **Bottleneck A (Domain Specialization):** The agency claims 282 specialized agents, but executes only a fixed single-page web template pipeline.
   - **Bottleneck B (Single-Run Volatility):** `workflow_state` lives in volatile memory; restart kills in-flight runs; concurrency is locked to 1.
   - **Bottleneck C (One-Way QA Gate):** Minor syntax flaws cause immediate terminal failure rather than triggering a developer repair cycle.

---

## 5. Candidate Next Milestones

Three candidate milestones were formulated and evaluated against platform vision, technical dependencies, risk, and value:

```
+---------------------------------------------------------------------------------------------------------+
|                                    CANDIDATE MILESTONES EVALUATION                                      |
+---------------------------------------------------------------------------------------------------------+
| Milestone 2.4-A: Autonomous Squad Triage & Dynamic Multi-Agent Orchestration                            |
| Value: High (Fulfills the 282-agent promise; domain-tailored software planning and architecture)        |
| Complexity: Moderate | Scope: app.py prompt routing + agent registry integration | Regression Risk: Low|
+---------------------------------------------------------------------------------------------------------+
| Milestone 2.4-B: Persistent Workflow Session Engine & Multi-Project State Durability                   |
| Value: Moderate-High (Server restarts don't lose tasks; concurrent project handling)                   |
| Complexity: High | Scope: Database/JSON state refactor + state machine overhaul | Regression Risk: Med  |
+---------------------------------------------------------------------------------------------------------+
| Milestone 2.4-C: Closed-Loop QA Self-Healing & Iterative Code Remediation                               |
| Value: High (Drastically reduces build aborts; autonomous bug fixing before human delivery)             |
| Complexity: Moderate | Scope: continue_workflow retry loop + targeted QA feedback | Regression Risk: Low|
+---------------------------------------------------------------------------------------------------------+
```

### Detailed Evaluation of Candidates

#### Milestone 2.4-A — Autonomous Squad Triage & Dynamic Multi-Agent Orchestration
- **Concept:** Introduce an automated **Project Triage & Lead Architect** stage at inception. When a brief arrives, this stage classifies the project domain (e.g., E-Commerce, Healthcare/Clinic, Industrial Telemetry, Arcade/Gaming, Dashboard/Fintech), dynamically selects the 3–5 most relevant specialized agents from `agency-agents/` (e.g., `specialized/pricing-analyst.md`, `engineering/database-optimizer.md`, `design/apple-design.md`, `engineering/security-reviewer.md`), and injects their specialized domain prompts into the Strategy, Architecture, and QA stages.
- **Trade-offs:** Directly elevates output quality and delivers the promised 282-agent capability; relies on prompt structure and requires maintaining schema guarantees for downstream packaging.

#### Milestone 2.4-B — Persistent Workflow Session Engine & Multi-Project Durability
- **Concept:** Transition `workflow_state` from a single volatile Python dictionary to a persistent session model (JSON files or SQLite in `data/sessions/`). Assign each run a `session_id`, allow concurrent workflows, and enable workflow resumption if the host server reboots.
- **Trade-offs:** Solves server restart durability and concurrency locks; however, it requires significant structural changes across both backend state management and frontend polling contracts.

#### Milestone 2.4-C — Closed-Loop QA Self-Healing & Iterative Code Remediation
- **Concept:** When `run_qa_checks()` detects HTML/CSS/JS errors, instead of aborting the workflow, invoke an automated `Code Remediation Specialist` agent with the exact error list (e.g., `Unclosed <div> tag`, `Unbalanced CSS braces`), allowing up to 2 automated repair attempts before halting.
- **Trade-offs:** Solves the most common source of workflow abandonment and ensures delivered artifacts are truly functional; smaller in architectural scope than 2.4-A or 2.4-B.

---

## 6. Recommended Next Phase

### **Recommendation: Milestone 2.4-A — Autonomous Squad Triage & Dynamic Multi-Agent Orchestration**

**Rationale:**
1. **Core Value Alignment:** The platform's defining value proposition is an "Autonomous AI Software Agency" with 282 specialized agents. Currently, 97% of those agents are inactive prompt files. Activating dynamic squad selection transforms the dashboard from a static template generator into a genuinely intelligent agency.
2. **Low Architectural Regression Risk:** Dynamic agent dispatching can be implemented cleanly within `run_workflow()` and `continue_workflow()` without altering the underlying storage schema, breaking the frontend polling contract, or invalidating Option B data.
3. **Synergy with Established Controls:** Phase 2.3's hardened QA validation and atomic packaging remain fully in place to verify the outputs of whatever squad is dynamically assembled.

---

## 7. Detailed Pre-Implementation Plan (Phase 2.4)

### 7.1 Proposed Phase Identifier and Title
**Phase 2.4 — Autonomous Squad Triage & Dynamic Multi-Agent Orchestration**

### 7.2 Problem Statement & Business Value
- **Problem:** Every project prompt currently executes the identical 8 agents in a hardcoded linear chain (`app.py:772-802`, `app.py:1239-1317`). A dental clinic app, a predictive maintenance dashboard, and a Tetris game receive the exact same generic developer and architect prompts.
- **Value:** Dynamically tailoring the agent squad to the specific technical, regulatory, and design requirements of the client brief produces substantially higher-quality software, realistic domain architectures, and delivers on the core agency vision.

### 7.3 Scope and Explicit Non-Goals
- **In-Scope:**
  1. Create a `triage_project_squad(task_desc)` helper that classifies project archetype and selects 3–5 specialized agent prompt paths from `agency-agents/`.
  2. Dynamically inject the selected specialist personas into the Strategic Feasibility, Software Architecture, and QA Review stages.
  3. Update `workflow_state["active_squad"]` so the frontend Activity Feed and Agent Roster display the actual active squad members.
  4. Unit and integration tests in `tests/test_squad_triage.py` verifying correct archetype classification and valid agent prompt resolution across diverse briefs.
- **Explicit Non-Goals:**
  - No changes to persistent Option B files (`budget.json`, `budget.json.bak`, `settings.json`).
  - No alterations to the delivery packaging mechanism (`index.html`, `style.css`, `script.js`).
  - No database migration or server framework replacement.
  - No external network calls in automated CI tests (all triage tests use local classification heuristics or mocked LLM fixtures).

### 7.4 Dependencies & Prerequisite Checks
1. Baseline HEAD verified at `841131c132308131c1ba972061cf4b7c469f3bc9`.
2. Directory `c:\Users\sam20\agency-agents` verified present and accessible.
3. Existing CI workflow `.github/workflows/symlink_security_verify.yml` remains green.

### 7.5 Expected Files Requiring Changes
- `app.py`: Implement squad classification and dynamic agent path resolution in `run_workflow` and `continue_workflow`.
- `tests/test_squad_triage.py`: New comprehensive test suite for squad discovery, classification, and execution.
- `tests/test_workflow_integration.py`: Add test asserting dynamic squad metadata in `workflow_state`.

### 7.6 Protected Files (Strictly Untouched)
- `budget.json` (SHA-256: `f81988bc0de8083b46532baeb199834a4bfac4b991104b6b77b1733f0756ee16`)
- `budget.json.bak` (SHA-256: `5db2a3c506deb4c0e6d55478e1efad1c2adce14c7b193f290f90f814e2d56e59`)
- `settings.json` (SHA-256: `2f4e6d39b3e8ac781c04ca9d14983f1a1848b6ef1a2be5db0b258bf56f106c94`)

### 7.7 Proposed Architecture & Implementation Sequence
```text
Task Inception (POST /api/start_task)
      │
      ▼
Stage 0: Squad Triage Engine (New)
  - Evaluates brief keywords & intent
  - Selects domain specialist (e.g. Healthcare, Industrial, Gaming, E-Commerce)
  - Selects engineering specialist (e.g. Database pro, API designer, Fullstack guardian)
  - Selects QA/Security specialist (e.g. Security reviewer, Accessibility tester)
  - Registers active squad in workflow_state["active_squad"]
      │
      ▼
Stage 1: Strategy Stage (Dynamic: Business Strategist + Domain Specialist)
      │
      ▼
Stage 2: Product Stage (Product Manager with Domain Focus)
      │
      ▼
[Human-in-the-Loop Approval Gate]
      │
      ▼
Stage 3: Engineering Architecture (Dynamic: Software Architect + Specialized Architect)
      │
      ▼
Stage 4: Creative UI/UX Design (Dynamic: Mivida Standard + Industry Style)
      │
      ▼
Stage 5: Development (Senior Developer with Domain Stack Instructions)
      │
      ▼
Stage 6: QA & Code Audit (Dynamic: QA Engineer + Domain Security Reviewer)
      │
      ▼
Stages 7–8: Motion, SEO & Atomic Packaging (Existing Hardened Pipeline)
```

### 7.8 Security, Reliability & Data-Integrity Invariants
1. **Prompt Sanitization:** Client briefs passed to triage are treated as untrusted text; no template injection or prompt escaping exploits.
2. **Fallback Safety:** If a requested specialist markdown file is missing or corrupt, the system falls back gracefully to the established core generalist prompt (`business-strategist.md`, `engineering-software-architect.md`, etc.) without raising an unhandled exception.
3. **Fail-Closed QA:** The strict HTML/CSS/JS structural QA gate must remain mandatory and unaltered.

### 7.9 Verification & Testing Strategy
- **Unit Tests:** `test_squad_triage.py` will test at least 10 diverse client briefs across industries (Clinic, Fintech, E-Commerce, IoT/Industrial, Arcade Game, Portfolio) and assert that valid, specialized agent prompt paths are selected.
- **Fallback Verification:** Assert that invalid or missing agent prompt requests cleanly default to baseline roles without workflow abortion.
- **Integration Tests:** Full workflow execution using mocked LLM responses asserting that `workflow_state["active_squad"]` correctly populates and deliverables write atomically.
- **CI Integrity:** GitHub Actions workflow must trigger automatically via `tests/**` and confirm post-test repository cleanliness.

### 7.10 Objectively Testable Acceptance Criteria
| ID | Criterion | Evidence Required |
|:---:|---|---|
| **AC-1** | Domain Brief Classification | `triage_project_squad("عيادة أسنان")` selects healthcare/specialized roles; `"متجر إلكتروني"` selects e-commerce/pricing roles. |
| **AC-2** | Graceful Fallback | Missing agent file triggers safe fallback to default role with logged telemetry; workflow does not crash. |
| **AC-3** | Active Squad Telemetry | `GET /api/state` returns `active_squad` array with valid role metadata. |
| **AC-4** | Zero Protected Mutation | `budget.json`, `budget.json.bak`, `settings.json` retain identical SHA-256 hashes before and after all test runs. |
| **AC-5** | Full Regression Suite Green | All existing test suites (`test_adversarial_paths`, `test_adversarial_rollback`, `test_csrf_origin_policy`, etc.) pass 100%. |

### 7.11 Rollback & Recovery Strategy
All changes will be isolated to a dedicated feature branch or linear commit that can be reverted cleanly via standard Git commands without modifying `budget.json` or existing generated applications.

### 7.12 Definition of Done
1. Code implementation in `app.py` complete and formatted.
2. New test suite `tests/test_squad_triage.py` passing with 100% success.
3. Full local regression test suite (167+ tests) passing with 0 failures and 0 unintended skips.
4. Option B protected files verified byte-for-byte invariant.
5. GitHub Actions CI workflow triggered and passing on Ubuntu runner.
6. Independent audit report produced and approved by repository owner.

---

## 8. Risks and Required Owner Decisions

### Risks to Note
1. **Prompt Token Budget Impact:** Calling additional specialized agents or adding triage analysis will consume additional LLM tokens per workflow run. The dynamic accounting ledger in `budget.json` will accurately record these, but overall cost per project will slightly increase.
2. **Context Window Sizing:** Combining specialist prompts with the user brief requires careful prompt framing to avoid exceeding model context boundaries on smaller models (e.g. `gemini-3.5-flash-lite`).

### Required Owner Decisions
Before implementation can be authorized, the repository owner must decide:
1. **Milestone Selection:** Does the owner approve proceeding with **Milestone 2.4-A (Autonomous Squad Triage)**, or does the owner prefer **Milestone 2.4-B (Persistent Sessions)** or **Milestone 2.4-C (QA Self-Healing)**?
2. **Triage Strategy:** Should squad triage be handled by a lightweight heuristic rule-engine (zero token cost, deterministic, instant), by an LLM classification call (dynamic, semantic, requires ~200 tokens), or a hybrid approach? (Recommended: Hybrid — heuristic fast-path with LLM semantic fallback).
3. **Execution Authorization:** Formal sign-off to begin Phase 2.4 implementation based on the approved pre-implementation plan.

---

## 9. Final Authorization Gate

```
========================================================================================
FINAL AUTHORIZATION STATUS: READY FOR OWNER REVIEW
========================================================================================
- Baseline State:       841131c132308131c1ba972061cf4b7c469f3bc9 (Verified & Clean)
- Option B Data:        100% Byte-for-Byte Invariant
- Pre-Implementation:   Detailed Phase 2.4 Plan Complete & Grounded in Evidence
- Implementation Mode:  STRICTLY BLOCKED pending explicit owner review and approval.
========================================================================================
```
