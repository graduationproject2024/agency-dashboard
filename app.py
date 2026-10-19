from flask import Flask, send_from_directory, jsonify, request, abort, Response
import time
import threading
import os
import glob
import json
import requests
import re
import copy
import uuid
import shutil
import subprocess
import logging
import hashlib
import hmac
from urllib.parse import urlparse
from html.parser import HTMLParser
from typing import Optional, Dict, Any, Tuple, List, Union
import db_persistence as db_store
import task_orchestrator as orch

# Initialize persistence schema on application startup
db_store.init_db()
db_store.reconcile_interrupted_sessions()

# Secure Flask initialization: Never expose current working directory as static root
app = Flask(__name__, static_folder=None)

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_AGENTS_DIR = os.path.abspath(os.path.join(REPO_DIR, "..", "agency-agents"))

def get_agents_dir():
    """
    Resolve agents directory path dynamically at runtime.
    Precedence:
    1. AGENTS_DIR environment variable
    2. Sibling directory '../agency-agents'
    3. Nested directory './agency-agents'
    """
    env_path = os.environ.get('AGENTS_DIR')
    if env_path:
        return os.path.abspath(env_path)
    if os.path.exists(DEFAULT_AGENTS_DIR):
        return DEFAULT_AGENTS_DIR
    return os.path.abspath(os.path.join(REPO_DIR, "agency-agents"))

AGENTS_DIR = get_agents_dir()
SETTINGS_FILE = 'settings.json'
BUDGET_FILE = 'budget.json'
GENERATED_APPS_DIR = os.path.abspath("generated_apps")
os.makedirs(GENERATED_APPS_DIR, exist_ok=True)

# Windows reserved device names that cannot be created or accessed as regular files
WINDOWS_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {f'COM{i}' for i in range(1, 10)} | {f'LPT{i}' for i in range(1, 10)}

ALLOWED_LOCAL_HOSTS = {'127.0.0.1', 'localhost'}
ALLOWED_SCHEMES = {'http', 'https'}

def get_allowed_ports():
    return {int(os.environ.get('PORT', 8080))}

def is_safe_origin(req):
    """
    Strict request-validation policy for state-changing requests.
    - Defines explicit trusted-origin policy matching configured host and exact port.
    - Rejects foreign origins, null origins, unsupported schemes, and unexpected ports.
    - Never trusts an attacker-controlled Host header.
    - Rejects cross-site Fetch Metadata indications.
    - Validates Referer against the exact trusted origin if Origin is absent.
    - If both Origin and Referer are absent, rejects browser form requests (urlencoded/multipart)
      and cross-site navigation, allowing only non-browser API clients with Content-Type: application/json.
    """
    # 1. Fetch Metadata defense-in-depth
    sec_fetch_site = req.headers.get('Sec-Fetch-Site')
    if sec_fetch_site == 'cross-site':
        return False
        
    allowed_ports = get_allowed_ports()

    origin_header = req.headers.get('Origin')
    if origin_header is not None:
        origin = origin_header.strip()
        if not origin or origin.lower() == 'null':
            return False
        try:
            parsed = urlparse(origin)
            if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.hostname:
                return False
            # Never trust req.host - validate strictly against ALLOWED_LOCAL_HOSTS
            if parsed.hostname.lower() not in ALLOWED_LOCAL_HOSTS:
                return False
            effective_port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            if effective_port not in allowed_ports:
                return False
        except Exception:
            return False
        return True

    # 2. If Origin is absent, check Referer
    referer = req.headers.get('Referer')
    if referer:
        referer = referer.strip()
        try:
            parsed_ref = urlparse(referer)
            if parsed_ref.scheme.lower() not in ALLOWED_SCHEMES or not parsed_ref.hostname:
                return False
            if parsed_ref.hostname.lower() not in ALLOWED_LOCAL_HOSTS:
                return False
            effective_ref_port = parsed_ref.port or (443 if parsed_ref.scheme == 'https' else 80)
            if effective_ref_port not in allowed_ports:
                return False
        except Exception:
            return False
        return True

    # 3. If both Origin and Referer are absent:
    # Defend against browser form submissions that strip referrer (e.g. rel="noreferrer")
    sec_fetch_mode = req.headers.get('Sec-Fetch-Mode')
    if sec_fetch_mode in ('navigate', 'nested-navigate', 'no-cors'):
        return False

    content_type = (req.content_type or '').lower()
    # Reject cross-origin browser form submissions (urlencoded/multipart) attempting to strip referrer
    if 'application/x-www-form-urlencoded' in content_type or 'multipart/form-data' in content_type:
        return False
        
    return True

def atomic_write_json(filepath, data):
    """
    Safely write JSON file atomically via temporary file + atomic rename.
    Ensures that unexpected termination never leaves a corrupt or empty file.
    Cleans up temporary file upon error.
    """
    dir_name = os.path.dirname(os.path.abspath(filepath)) or '.'
    os.makedirs(dir_name, exist_ok=True)
    tmp_path = os.path.join(dir_name, f".{os.path.basename(filepath)}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp_path, 'w', encoding='utf-8', newline='') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, filepath)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise

def atomic_write_file(filepath, content, encoding="utf-8"):
    """
    Safely write arbitrary text content atomically via temporary file + atomic rename.
    Ensures that unexpected termination or write failures never leave a corrupted file.
    Uses newline='' to preserve explicit byte encoding without platform newline translation.
    Cleans up temporary file upon error.
    """
    dir_name = os.path.dirname(os.path.abspath(filepath)) or '.'
    os.makedirs(dir_name, exist_ok=True)
    tmp_path = os.path.join(dir_name, f".{os.path.basename(filepath)}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp_path, 'w', encoding=encoding, newline='') as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, filepath)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise

def cleanup_backups(backups_dict, failed_restorations=None):
    """
    Safely clean up obsolete backup files (.bak) created during workflow staging.
    CRITICAL SAFETY INVARIANT:
    Never delete a backup file whose restoration failed, as it is the only surviving
    copy of pre-existing user data.
    Returns a list of cleanup error strings (if any).
    """
    failed = failed_restorations or {}
    cleanup_errors = []
    if not backups_dict:
        return cleanup_errors

    for fpath, bak_path in backups_dict.items():
        if fpath in failed:
            # Retain recovery backup - MUST NOT delete!
            continue
        try:
            if os.path.exists(bak_path):
                os.remove(bak_path)
        except OSError as e:
            cleanup_errors.append(f"Failed to clean up obsolete backup {os.path.basename(bak_path)}: {e}")

    return cleanup_errors

# =========================================================================
# Canonical Agency Divisions Registry (18 Required Divisions)
# =========================================================================

CANONICAL_DIVISIONS = [
    {
        "id": "academic",
        "name": "Academic",
        "name_ar": "الشؤون الأكاديمية والعلوم",
        "icon": "fa-graduation-cap",
        "description": "Academic research, humanities, statistical modeling, and specialized research mentoring.",
        "order": 1
    },
    {
        "id": "design",
        "name": "Design",
        "name_ar": "التصميم وتجربة المستخدم",
        "icon": "fa-palette",
        "description": "UI/UX design, visual systems, whimsy animation, brand identity, and design systems.",
        "order": 2
    },
    {
        "id": "engineering",
        "name": "Engineering",
        "name_ar": "الهندسة والتطوير البرمجي",
        "icon": "fa-code",
        "description": "Software architecture, fullstack development, backend systems, databases, and DevOps.",
        "order": 3
    },
    {
        "id": "finance",
        "name": "Finance",
        "name_ar": "المالية والمحاسبة",
        "icon": "fa-calculator",
        "description": "Corporate finance, financial modeling, accounting, budget auditing, and investment metrics.",
        "order": 4
    },
    {
        "id": "game-development",
        "name": "Game Development",
        "name_ar": "تطوير الألعاب والمحاكاة",
        "icon": "fa-gamepad",
        "description": "Game design, economy balancing, Unity, Unreal Engine, Godot, and Roblox systems.",
        "order": 5
    },
    {
        "id": "gis",
        "name": "GIS",
        "name_ar": "نظم المعلومات الجغرافية",
        "icon": "fa-map-location-dot",
        "description": "Geospatial intelligence, mapping, spatial data infrastructure, and cartography.",
        "order": 6
    },
    {
        "id": "healthcare",
        "name": "Healthcare",
        "name_ar": "الرعاية الصحية والطبية",
        "icon": "fa-notes-medical",
        "description": "Clinical evidence, healthcare compliance, patient data privacy, and medical workflows.",
        "order": 7
    },
    {
        "id": "marketing",
        "name": "Marketing",
        "name_ar": "التسويق والمحتوى",
        "icon": "fa-bullhorn",
        "description": "Content strategy, SEO, brand positioning, growth hacking, and social media.",
        "order": 8
    },
    {
        "id": "paid-media",
        "name": "Paid Media",
        "name_ar": "الإعلانات الممولة والحملات",
        "icon": "fa-rectangle-ad",
        "description": "Paid advertising campaigns, ad copy optimization, acquisition bidding, and performance ROAS.",
        "order": 9
    },
    {
        "id": "product",
        "name": "Product",
        "name_ar": "إدارة وتخطيط المنتجات",
        "icon": "fa-boxes-stacked",
        "description": "Product management, roadmap definition, PRD authoring, and user stories.",
        "order": 10
    },
    {
        "id": "project-management",
        "name": "Project Management",
        "name_ar": "إدارة المشاريع والتنسيق",
        "icon": "fa-list-check",
        "description": "Scrum agile management, sprint planning, delivery coordination, and risk tracking.",
        "order": 11
    },
    {
        "id": "research",
        "name": "Research",
        "name_ar": "البحث واستشراف السوق",
        "icon": "fa-microscope",
        "description": "Market research, deep tech investigation, trend analysis, and competitor benchmarking.",
        "order": 12
    },
    {
        "id": "sales",
        "name": "Sales",
        "name_ar": "المبيعات وتطوير الأعمال",
        "icon": "fa-handshake",
        "description": "B2B sales strategy, enterprise deal closing, lead qualification, and cold outreach.",
        "order": 13
    },
    {
        "id": "security",
        "name": "Security",
        "name_ar": "الأمن السيبراني والامتثال",
        "icon": "fa-shield-halved",
        "description": "Application security, penetration testing, compliance, threat modeling, and cryptography.",
        "order": 14
    },
    {
        "id": "spatial-computing",
        "name": "Spatial Computing",
        "name_ar": "الحوسبة المكانية والواقع الممتد",
        "icon": "fa-vr-cardboard",
        "description": "AR/VR/XR spatial applications, 3D world interaction, visionOS, and WebXR.",
        "order": 15
    },
    {
        "id": "specialized",
        "name": "Specialized",
        "name_ar": "الاستشارات والحلول المتخصصة",
        "icon": "fa-star",
        "description": "Specialized consultants, business strategists, pricing analysts, and boutique domains.",
        "order": 16
    },
    {
        "id": "support",
        "name": "Support",
        "name_ar": "الدعم الفني وخدمة العملاء",
        "icon": "fa-headset",
        "description": "Technical support, customer success, incident triaging, and documentation guidance.",
        "order": 17
    },
    {
        "id": "testing",
        "name": "Testing",
        "name_ar": "فحص البرمجيات وضمان الجودة",
        "icon": "fa-vial-circle-check",
        "description": "Automated testing, accessibility compliance, performance benchmarking, and QA audits.",
        "order": 18
    }
]

CANONICAL_DIVISION_MAP = {d["id"]: d for d in CANONICAL_DIVISIONS}
CANONICAL_NAME_MAP = {d["name"].lower(): d for d in CANONICAL_DIVISIONS}

def parse_agent_frontmatter(content):
    """
    Extract structured metadata from agent definition markdown frontmatter.
    Extracts name, description, division, and department if present.
    """
    meta = {}
    if not content or not content.startswith("---"):
        return meta
    parts = content.split("---", 2)
    if len(parts) >= 3:
        for line in parts[1].splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip("'\"")
    return meta

def discover_agents(agents_dir=None):
    """
    Discover available agent markdown definitions in target directory safely.
    Maps every discovered agent to one of the 18 canonical divisions.
    Always provides all 18 canonical divisions in 'divisions', even if empty.
    Distinguishes actual discovered count from missing/empty/error states.
    Never fabricates an agent count.
    """
    target_dir = agents_dir or get_agents_dir()
    
    empty_divisions = [
        {
            **d,
            "registered_count": 0,
            "discovered_count": 0,
            "agents": []
        }
        for d in CANONICAL_DIVISIONS
    ]

    if not target_dir or not os.path.exists(target_dir):
        return {
            "total": 0,
            "agents": [],
            "divisions": empty_divisions,
            "unclassified": [],
            "status": "not_found",
            "directory": target_dir,
            "error": "Directory does not exist"
        }
        
    agents_list = []
    unclassified_list = []
    ignored_files = {
        'README.md', 'CONTRIBUTING.md', 'SECURITY.md', 'LICENSE.md',
        'CHANGELOG.md', 'INDEX.md', 'TODO.md', 'SUMMARY.md',
        'PLAN.md', 'REPORT.md', 'CONTEXT.md', 'TEMPLATE.md'
    }
    
    try:
        for root, dirs, files in os.walk(target_dir, followlinks=False):
            # Prune hidden directories, build tools, and non-agent folders
            dirs[:] = [
                d for d in dirs 
                if not d.startswith('.') and d not in {'node_modules', '__pycache__', 'venv', '.venv', 'examples', 'scripts', 'tests', 'strategy', 'integrations'}
            ]
            rel_root = os.path.relpath(root, target_dir)
            top_folder = rel_root.replace('\\', '/').split('/')[0].lower() if rel_root != '.' else ''

            for f in files:
                if f.startswith('.'):
                    continue
                if f.endswith('.md') and f not in ignored_files and not f.endswith('_zh-CN.md'):
                    f_path = os.path.join(root, f)
                    try:
                        # Genuine agent definition format check: non-empty file
                        if os.path.getsize(f_path) < 20:
                            continue
                        with open(f_path, 'r', encoding='utf-8', errors='replace') as fp:
                            content = fp.read(4096)
                    except OSError:
                        continue

                    raw_id = f.replace('.md', '')
                    meta = parse_agent_frontmatter(content)
                    clean_name = meta.get("name") or raw_id.replace('-', ' ').title()
                    
                    # Deterministic Division Assignment Strategy:
                    # 1. Explicit metadata in frontmatter (division / department)
                    # 2. Top-level relative directory under target_dir
                    # 3. Fallback: match by relative path components
                    div_match = None
                    explicit_div = (meta.get("division") or meta.get("department") or "").lower()
                    if explicit_div in CANONICAL_DIVISION_MAP:
                        div_match = CANONICAL_DIVISION_MAP[explicit_div]
                    elif explicit_div in CANONICAL_NAME_MAP:
                        div_match = CANONICAL_NAME_MAP[explicit_div]
                    elif top_folder in CANONICAL_DIVISION_MAP:
                        div_match = CANONICAL_DIVISION_MAP[top_folder]
                    elif top_folder in CANONICAL_NAME_MAP:
                        div_match = CANONICAL_NAME_MAP[top_folder]
                    else:
                        # Check any path component in case of custom nested structure
                        for part in rel_root.replace('\\', '/').split('/'):
                            p_clean = part.lower()
                            if p_clean in CANONICAL_DIVISION_MAP:
                                div_match = CANONICAL_DIVISION_MAP[p_clean]
                                break
                            elif p_clean in CANONICAL_NAME_MAP:
                                div_match = CANONICAL_NAME_MAP[p_clean]
                                break

                    rel_file = os.path.relpath(f_path, target_dir).replace('\\', '/')
                    
                    if div_match:
                        agents_list.append({
                            "id": raw_id,
                            "name": clean_name,
                            "department": div_match["name"],
                            "division_id": div_match["id"],
                            "division_name": div_match["name"],
                            "division_name_ar": div_match.get("name_ar", div_match["name"]),
                            "description": meta.get("description", ""),
                            "relative_path": rel_file,
                            "status": "idle"
                        })
                    else:
                        unclassified_list.append({
                            "id": raw_id,
                            "name": clean_name,
                            "path": rel_file,
                            "folder": top_folder
                        })
    except Exception as e:
        return {
            "total": 0,
            "agents": [],
            "divisions": empty_divisions,
            "unclassified": [],
            "status": "error",
            "directory": target_dir,
            "error": str(e)
        }
        
    # Populate per-division counts and member agent IDs in stable order
    division_records = []
    for d in CANONICAL_DIVISIONS:
        d_agents = [a for a in agents_list if a["division_id"] == d["id"]]
        division_records.append({
            **d,
            "registered_count": len(d_agents),
            "discovered_count": len(d_agents),
            "agents": [a["id"] for a in d_agents]
        })

    return {
        "total": len(agents_list),
        "agents": agents_list,
        "divisions": division_records,
        "unclassified": unclassified_list,
        "status": "discovered" if agents_list else "empty",
        "directory": target_dir,
        "error": None
    }

# --- Safe Agent Discovery & Secure Prompt Loading ---

MAX_AGENT_PROMPT_BYTES = 65536  # 64 KB prompt size limit
MIN_AGENT_PROMPT_BYTES = 20     # Minimum viable agent definition size

def safe_resolve_agent_path(agent_rel_path, agents_dir=None):
    """
    Resolve agent prompt file strictly relative to trusted agent root.
    Security policy:
    1. Rejects non-string, empty, or whitespace-only paths.
    2. Rejects absolute paths (Unix '/', Windows '\' or drive letters 'C:').
    3. Rejects path traversal attempts ('..').
    4. Canonical containment: Resolves full real canonical paths for both root and target.
       Guarantees that intermediate directory symlinks AND final symlinks cannot escape
       the canonical agent directory.
    5. Internal symlink policy: Symlinks whose canonical destination lies strictly within
       canonical_base and resolves to a regular .md file are permitted.
    6. Broken symlinks and non-existent files are rejected with explicit error.
    7. Directories, device files, and non-regular files are rejected with explicit error.
    """
    raw_base = agents_dir or get_agents_dir()
    if not raw_base:
        return None, "Agents directory not configured"
    base_dir = os.path.abspath(raw_base)
    
    if not agent_rel_path or not isinstance(agent_rel_path, str):
        return None, "Invalid path: empty or not a string"
    
    clean_path = agent_rel_path.strip()
    if not clean_path:
        return None, "Invalid path: empty string"

    # Normalize Windows backslashes to standard forward slashes for cross-platform containment
    unified_path = clean_path.replace('\\', '/')

    # Reject absolute paths (Unix '/', Windows '\' or drive letters 'C:')
    if (os.path.isabs(clean_path) or os.path.isabs(unified_path) or 
        clean_path.startswith('\\') or clean_path.startswith('/') or 
        (len(clean_path) > 1 and clean_path[1] == ':')):
        return None, "Absolute paths not permitted"
        
    normalized_rel = os.path.normpath(unified_path)
    # Check for path traversal components across all platforms
    parts = normalized_rel.replace('\\', '/').split('/')
    if '..' in parts or normalized_rel.startswith('..'):
        return None, "Path traversal rejected"
        
    target_path = os.path.abspath(os.path.join(base_dir, normalized_rel))
    
    # 1. Lexical containment check
    try:
        if os.path.commonpath([base_dir, target_path]) != base_dir:
            return None, "Path escapes agents directory"
    except (ValueError, Exception):
        return None, "Path containment check failed"

    # 2. Check for broken symlinks or non-existent paths before canonical inspection
    if os.path.lexists(target_path) and not os.path.exists(target_path):
        return None, f"Broken symlink or target does not exist: {normalized_rel}"

    if not os.path.exists(target_path):
        return None, f"Agent prompt file does not exist: {normalized_rel}"

    # 3. Canonical path containment (enforces both intermediate-directory and final symlinks)
    try:
        canonical_base = os.path.realpath(base_dir)
        canonical_target = os.path.realpath(target_path)
        if canonical_target == canonical_base or os.path.commonpath([canonical_base, canonical_target]) != canonical_base:
            return None, "Symlink escape rejected"
    except (ValueError, Exception):
        return None, "Symlink containment check failed"

    # 4. Enforce regular file check (reject directories and special devices)
    if os.path.isdir(canonical_target) or os.path.isdir(target_path):
        return None, f"Agent path is a directory, not a regular file: {normalized_rel}"

    if not os.path.isfile(canonical_target):
        return None, f"Agent path is not a regular file: {normalized_rel}"
        
    return canonical_target, None


def safe_load_agent_prompt(agent_rel_path, fallback_prompt=None, max_bytes=MAX_AGENT_PROMPT_BYTES, agents_dir=None):
    """
    Safely load an agent prompt file with strict size, encoding, and containment validation.
    Returns: (content: str, success: bool, error: str or None)
    """
    safe_path, err = safe_resolve_agent_path(agent_rel_path, agents_dir)
    if err or not safe_path:
        return fallback_prompt or "", False, err
        
    try:
        file_size = os.path.getsize(safe_path)
        if file_size > max_bytes:
            return fallback_prompt or "", False, f"File size ({file_size} bytes) exceeds maximum limit ({max_bytes} bytes)"
        if file_size < MIN_AGENT_PROMPT_BYTES:
            return fallback_prompt or "", False, f"File size ({file_size} bytes) below minimum viable threshold"
            
        with open(safe_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
            
        return content, True, None
    except Exception as e:
        return fallback_prompt or "", False, f"Failed to read agent prompt: {str(e)}"

# --- Hybrid Squad Triage Engine ---

def normalize_text(text):
    """Normalize Arabic and Latin text for robust keyword matching."""
    text = (text or '').lower()
    text = re.sub(r'[إأآا]', 'ا', text)
    text = re.sub(r'ة', 'ه', text)
    text = re.sub(r'ى', 'ي', text)
    text = re.sub(r'[\u064B-\u0652]', '', text)
    text = re.sub(r'[\r\n\t]', ' ', text)
    return text

def contains_keyword(text, kw):
    """Boundary-aware keyword matching with support for Arabic prefixes."""
    if ' ' in kw:
        return kw in text
    pattern = r'(?:\b|(?<=[\s\W]))(?:ال|و|ف|ب|ك|ل|لل|وال|فال|بال|كال)?' + re.escape(kw) + r'(?=\b|[\s\W]|$)'
    return bool(re.search(pattern, text))

SQUAD_ARCHETYPES = {
    'healthcare': {
        'label': 'Healthcare & Clinic Systems',
        'keywords': [
            'عياده', 'عيادات', 'طبيب', 'اطباء', 'دكتور', 'دكاتره', 'اسنان', 'سن', 'مستشفي', 'مستشفيات',
            'مرضي', 'مريض', 'صيدليه', 'صيدليات', 'علاج', 'صحه', 'طبي', 'طبيه', 'روشسته', 'مواعيد طبيه',
            'clinic', 'dental', 'dentist', 'doctor', 'hospital', 'patient', 'patients', 'healthcare',
            'medical', 'medicine', 'pharmacy', 'health', 'appointment'
        ],
        'specialists': [
            {
                'id': 'healthcare-clinical-evidence-agent',
                'name': 'خبير الأدلة الطبية والسريرية',
                'display_name': 'Clinical Evidence Specialist',
                'short_name': 'MED',
                'category': 'domain',
                'stage': 'strategy',
                'reason': 'يتحقق من دقة تدفق المواعيد ومطابقة المعايير الطبية وخصوصية بيانات المرضى',
                'prompt_file': r'healthcare\healthcare-clinical-evidence-agent.md',
                'color': 'linear-gradient(135deg, #10b981, #059669)'
            },
            {
                'id': 'engineering-database-optimizer',
                'name': 'مهندس قواعد البيانات الطبية',
                'display_name': 'Database Optimizer',
                'short_name': 'DB',
                'category': 'architecture',
                'stage': 'engineering',
                'reason': 'يصمم مخططات بيانات المرضى وسجلات المواعيد الطبية بكفاءة وأمان',
                'prompt_file': r'engineering\engineering-database-optimizer.md',
                'color': 'linear-gradient(135deg, #3b82f6, #1d4ed8)'
            },
            {
                'id': 'testing-accessibility-auditor',
                'name': 'مدقق إمكانية الوصول والتوافق',
                'display_name': 'Accessibility Auditor',
                'short_name': 'A11Y',
                'category': 'testing',
                'stage': 'qa',
                'reason': 'يضمن سهولة استخدام النظام للمرضى وكبار السن وفق معايير WCAG',
                'prompt_file': r'testing\testing-accessibility-auditor.md',
                'color': 'linear-gradient(135deg, #f59e0b, #d97706)'
            }
        ]
    },
    'ecommerce': {
        'label': 'E-Commerce & Pricing Systems',
        'keywords': [
            'متجر', 'متاجر', 'تسوق', 'سله', 'سلات', 'شراء', 'منتجات', 'منتج', 'دفع', 'اسعار', 'سعر',
            'تسعير', 'طلبات', 'طلب', 'تخفيضات', 'خصم', 'بضائع', 'بضاعه', 'شحن', 'توصيل', 'عملاء',
            'ecommerce', 'e-commerce', 'store', 'shop', 'shopping', 'cart', 'checkout', 'pricing',
            'price', 'inventory', 'marketplace', 'retail', 'discount', 'payment', 'order'
        ],
        'specialists': [
            {
                'id': 'specialized-pricing-analyst',
                'name': 'محلل التسعير والتجارة الإلكترونية',
                'display_name': 'Pricing Analyst',
                'short_name': 'PRICE',
                'category': 'domain',
                'stage': 'strategy',
                'reason': 'يحلل ديناميكيات التسعير، الخصومات وسلة الشراء لتحقيق أعلى معدل تحويل',
                'prompt_file': r'specialized\specialized-pricing-analyst.md',
                'color': 'linear-gradient(135deg, #f97316, #ea580c)'
            },
            {
                'id': 'security-appsec-engineer',
                'name': 'مهندس أمن تطبيقات التجارة والدفع',
                'display_name': 'AppSec Engineer',
                'short_name': 'SEC',
                'category': 'security',
                'stage': 'engineering',
                'reason': 'يؤمن بوابات الدفع الإلكتروني، حماية بطاقات العملاء ومنع التلاعب بالأسعار',
                'prompt_file': r'security\security-appsec-engineer.md',
                'color': 'linear-gradient(135deg, #ef4444, #b91c1c)'
            },
            {
                'id': 'testing-performance-benchmarker',
                'name': 'مدقق أداء المتاجر والتحميل',
                'display_name': 'Performance Benchmarker',
                'short_name': 'PERF',
                'category': 'testing',
                'stage': 'qa',
                'reason': 'يختبر سرعة تصفح المنتجات وأداء إتمام الطلبات تحت الضغط العالي',
                'prompt_file': r'testing\testing-performance-benchmarker.md',
                'color': 'linear-gradient(135deg, #8b5cf6, #6d28d9)'
            }
        ]
    },
    'industrial': {
        'label': 'Industrial Automation & Telemetry',
        'keywords': [
            'مصنع', 'مصانع', 'صناعي', 'صناعيه', 'مراقبه', 'حساسات', 'حساس', 'تتبع', 'معدات', 'صيانه',
            'صيانه تنبويه', 'انترنت الاشياء', 'تشغيل', 'انتاج', 'خط انتاج', 'تليميتري', 'قراءات',
            'industrial', 'factory', 'iot', 'telemetry', 'sensor', 'sensors', 'predictive maintenance',
            'monitoring', 'scada', 'automation', 'equipment', 'manufacturing', 'plc'
        ],
        'specialists': [
            {
                'id': 'engineering-ai-data-remediation-engineer',
                'name': 'مهندس معالجة البيانات والحساسات',
                'display_name': 'IoT Data Remediation Engineer',
                'short_name': 'IOT',
                'category': 'domain',
                'stage': 'strategy',
                'reason': 'ينظم تدفق بيانات الحساسات، قراءات الأجهزة ومعايير اكتشاف الأعطال',
                'prompt_file': r'engineering\engineering-ai-data-remediation-engineer.md',
                'color': 'linear-gradient(135deg, #06b6d4, #0891b2)'
            },
            {
                'id': 'engineering-database-optimizer',
                'name': 'مهندس قواعد بيانات القياس والوقت',
                'display_name': 'Database Optimizer',
                'short_name': 'DB',
                'category': 'architecture',
                'stage': 'engineering',
                'reason': 'يهيئ قواعد بيانات السلاسل الزمنية لتخزين ملايين الإشارات الصناعية',
                'prompt_file': r'engineering\engineering-database-optimizer.md',
                'color': 'linear-gradient(135deg, #3b82f6, #1d4ed8)'
            },
            {
                'id': 'security-appsec-engineer',
                'name': 'مهندس أمن الأنظمة الصناعية والشبكات',
                'display_name': 'AppSec Engineer',
                'short_name': 'SEC',
                'category': 'security',
                'stage': 'qa',
                'reason': 'يدقق بروتوكولات عزل الحساسات ومنع الهجمات على خطوط الإنتاج والمعدات',
                'prompt_file': r'security\security-appsec-engineer.md',
                'color': 'linear-gradient(135deg, #ef4444, #b91c1c)'
            }
        ]
    },
    'gaming': {
        'label': 'Games & Interactive Experiences',
        'keywords': [
            'لعبه', 'العاب', 'جيم', 'مرحله', 'مراحل', 'نقاط', 'لاعب', 'لاعبين', 'تحدي', 'تتريس', 'اركيد',
            'سكور', 'مستويات', 'مستوي', 'تحكم', 'game', 'games', 'gaming', 'arcade', 'player', 'score',
            'scores', 'tetris', 'gameplay', 'level', 'levels', 'puzzle'
        ],
        'specialists': [
            {
                'id': 'game-designer',
                'name': 'مصمم ألعاب وتجربة اللعب',
                'display_name': 'Game Designer',
                'short_name': 'GAME',
                'category': 'domain',
                'stage': 'strategy',
                'reason': 'يضع ميكانيكا وحلقة اللعب وقواعد التفاعل وتدرج الصعوبة',
                'prompt_file': r'game-development\game-designer.md',
                'color': 'linear-gradient(135deg, #ec4899, #be185d)'
            },
            {
                'id': 'economy-designer',
                'name': 'مصمم ميكانيكا ونقاط اللعبة',
                'display_name': 'Economy Designer',
                'short_name': 'ECON',
                'category': 'domain',
                'stage': 'engineering',
                'reason': 'يوازن جداول النقاط والمكافآت وشروط الفوز والخسارة',
                'prompt_file': r'game-development\economy-designer.md',
                'color': 'linear-gradient(135deg, #8b5cf6, #6d28d9)'
            },
            {
                'id': 'design-whimsy-injector',
                'name': 'خبير المؤثرات الحركية والألعاب',
                'display_name': 'Whimsy Injector',
                'short_name': 'WHIMSY',
                'category': 'design',
                'stage': 'animation',
                'reason': 'يضيف مؤثرات بصرية مبهجة وتفاعل فيزيائي واستجابة سريعة للتحكم',
                'prompt_file': r'design\design-whimsy-injector.md',
                'color': 'linear-gradient(135deg, #ff758c, #ff7eb3)'
            }
        ]
    },
    'fintech': {
        'label': 'Business Dashboards & Fintech',
        'keywords': [
            'لوحه تحكم', 'مؤشرات', 'ماليه', 'محاسبه', 'ميزانيه', 'استثمار', 'تقارير', 'احصائيات',
            'ارباح', 'فواتير', 'فاتوره', 'تحليلات', 'بنوك', 'ارصده', 'رصيد', 'dashboard', 'fintech',
            'analytics', 'finance', 'financial', 'accounting', 'budget', 'kpi', 'investment',
            'invoicing', 'invoice', 'metrics', 'ledger'
        ],
        'specialists': [
            {
                'id': 'finance-financial-analyst',
                'name': 'المحلل المالي للشركات',
                'display_name': 'Financial Analyst',
                'short_name': 'FIN',
                'category': 'domain',
                'stage': 'strategy',
                'reason': 'يحدد معادلات المؤشرات المالية، تدفق الإيرادات ونماذج التقارير الدقيقة',
                'prompt_file': r'finance\finance-financial-analyst.md',
                'color': 'linear-gradient(135deg, #10b981, #047857)'
            },
            {
                'id': 'engineering-database-optimizer',
                'name': 'مهندس تدفق وقواعد البيانات المالية',
                'display_name': 'Database Optimizer',
                'short_name': 'DB',
                'category': 'architecture',
                'stage': 'engineering',
                'reason': 'يضمن دقة العمليات الحسابية وتطابق القيود المحاسبية وسرعة التجميع',
                'prompt_file': r'engineering\engineering-database-optimizer.md',
                'color': 'linear-gradient(135deg, #3b82f6, #1d4ed8)'
            },
            {
                'id': 'security-appsec-engineer',
                'name': 'مهندس أمان المعاملات المالية',
                'display_name': 'AppSec Engineer',
                'short_name': 'SEC',
                'category': 'security',
                'stage': 'qa',
                'reason': 'يتحقق من سرية البيانات المالية وسجلات التدقيق ومنع التلاعب بالأرقام',
                'prompt_file': r'security\security-appsec-engineer.md',
                'color': 'linear-gradient(135deg, #ef4444, #b91c1c)'
            }
        ]
    },
    'general': {
        'label': 'General Websites & Portfolios',
        'keywords': [
            'بورتفوليو', 'موقع شخصي', 'معرض اعمال', 'مدونه', 'موقع تعريفي', 'صفحه هبوط', 'موقع ويب',
            'portfolio', 'personal website', 'landing page', 'blog', 'showcase', 'website', 'web application'
        ],
        'specialists': [
            {
                'id': 'engineering-code-reviewer',
                'name': 'مراجع جودة الأكواد ومعايير الويب',
                'display_name': 'Code Reviewer',
                'short_name': 'REV',
                'category': 'testing',
                'stage': 'qa',
                'reason': 'يراقب المعايير البرمجية ونظافة هيكلة المشروع وخلوه من العيوب',
                'prompt_file': r'engineering\engineering-code-reviewer.md',
                'color': 'linear-gradient(135deg, #6366f1, #4f46e5)'
            },
            {
                'id': 'design-whimsy-injector',
                'name': 'خبير تفاعل ومؤثرات الواجهة',
                'display_name': 'Whimsy Injector',
                'short_name': 'WHIMSY',
                'category': 'design',
                'stage': 'animation',
                'reason': 'يضفي طابعاً تفاعلياً مبهجاً على الواجهة والحركات الدقيقة',
                'prompt_file': r'design\design-whimsy-injector.md',
                'color': 'linear-gradient(135deg, #ff758c, #ff7eb3)'
            },
            {
                'id': 'marketing-seo-specialist',
                'name': 'أخصائي تحسين محركات البحث والظهور',
                'display_name': 'SEO Specialist',
                'short_name': 'SEO',
                'category': 'marketing',
                'stage': 'seo',
                'reason': 'يحسن العناوين والبيانات الوصفية لتحقيق أرشفة مثالية في محركات البحث',
                'prompt_file': r'marketing\marketing-seo-specialist.md',
                'color': 'linear-gradient(135deg, #f093fb, #f5576c)'
            }
        ]
    }
}

def _validate_specialists(specialist_list, agents_dir=None):
    """
    Validate each specialist prompt file against trusted agent directory.
    Replaces missing or unreadable specialists with safe fallback generalists.
    """
    validated = []
    for spec in specialist_list:
        spec_copy = copy.deepcopy(spec)
        prompt_file = spec_copy.get("prompt_file", "")
        safe_path, err = safe_resolve_agent_path(prompt_file, agents_dir)
        if err or not safe_path:
            logging.warning("Specialist '%s' prompt file invalid (%s): %s. Substituting fallback.", spec_copy.get("id"), prompt_file, err)
            spec_copy["prompt_file"] = r"engineering\engineering-code-reviewer.md"
            spec_copy["reason"] += " (Safe fallback: code reviewer)"
        validated.append(spec_copy)
    return validated

def classify_project_semantic(task_desc):
    """
    Semantic fallback triage using configured provider LLM.
    Strictly bounded prompt, strict schema, returns valid archetype string or None on failure.
    Never executes in test environments or without credentials.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TESTING") or os.environ.get("DISABLE_SEMANTIC_TRIAGE"):
        return None

    settings = load_settings()
    provider = settings.get('provider', 'gemini')
    api_key = get_api_key(provider)
    if not api_key and provider != 'opencode':
        return None

    system_prompt = (
        "You are an autonomous software agency project classifier.\n"
        "Classify the given project task into exactly one of these archetypes:\n"
        "[\"healthcare\", \"ecommerce\", \"industrial\", \"gaming\", \"fintech\", \"general\"]\n"
        "Return ONLY a valid JSON object matching this schema:\n"
        "{\"archetype\": \"<string>\", \"confidence\": <float between 0.0 and 1.0>, \"reasoning\": \"<short string>\"}\n"
        "Do not wrap in markdown or include extra text."
    )
    
    clean_task = (task_desc or "").strip()[:500]  # Bounded prompt
    try:
        if provider == 'gemini':
            model = settings.get('openrouter_model', 'gemini-3.5-flash-lite')
            if not model or any(k in model.lower() for k in ['gpt', 'claude', 'openai', '1.5', '2.5']):
                model = 'gemini-3.5-flash-lite'
            resp = call_agent_gemini(system_prompt, clean_task, api_key, "Triage Classifier", preferred_model=model)
        elif provider == 'openrouter':
            model = settings.get('openrouter_model', 'google/gemini-2.0-flash-001')
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": clean_task}
                ],
                "temperature": 0.1,
                "max_tokens": 150
            }
            r = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=10)
            if r.status_code != 200:
                return None
            resp = r.json()["choices"][0]["message"]["content"]
        else:
            return None

        # Parse JSON and validate archetype
        resp_clean = resp.strip()
        if resp_clean.startswith("```"):
            resp_clean = re.sub(r"^```(?:json)?\s*", "", resp_clean)
            resp_clean = re.sub(r"\s*```$", "", resp_clean)
            
        data = json.loads(resp_clean)
        arch = data.get("archetype", "").strip().lower()
        if arch in SQUAD_ARCHETYPES:
            conf = float(data.get("confidence", 0.8))
            return arch, min(1.0, max(0.0, conf)), data.get("reasoning", "")
    except Exception as e:
        logging.warning("Semantic triage fallback encountered error: %s", e)
        
    return None

def triage_project_squad(task_desc, allow_semantic=True, agents_dir=None):
    """
    Hybrid squad triage engine:
    1. Deterministic heuristic fast-path for supported archetypes (Arabic & English).
    2. Semantic fallback via LLM only when heuristic is ambiguous.
    3. Safe generalist squad fallback on empty/unclassified briefs or errors.
    4. Safe filesystem resolution and validation for every specialist prompt file.
    """
    if not task_desc or not isinstance(task_desc, str) or not task_desc.strip():
        arch = "general"
        validated_specs = _validate_specialists(SQUAD_ARCHETYPES[arch]["specialists"], agents_dir)
        return {
            "archetype": arch,
            "label": SQUAD_ARCHETYPES[arch]["label"],
            "method": "fallback",
            "confidence": 1.0,
            "specialists": validated_specs
        }
        
    norm = normalize_text(task_desc)
    scores = {}
    matched = {}
    for arch, cfg in SQUAD_ARCHETYPES.items():
        if arch == "general":
            continue
        hits = [kw for kw in cfg["keywords"] if contains_keyword(norm, kw)]
        scores[arch] = len(hits)
        matched[arch] = hits
        
    gen_hits = [kw for kw in SQUAD_ARCHETYPES["general"]["keywords"] if contains_keyword(norm, kw)]
    scores["general"] = len(gen_hits)
    matched["general"] = gen_hits
    
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_arch, best_score = sorted_scores[0]
    second_arch, second_score = sorted_scores[1] if len(sorted_scores) > 1 else ("", 0)
    
    # Deterministic heuristic fast path
    if best_score > 0 and (best_score > second_score or best_score >= 2):
        validated_specs = _validate_specialists(SQUAD_ARCHETYPES[best_arch]["specialists"], agents_dir)
        return {
            "archetype": best_arch,
            "label": SQUAD_ARCHETYPES[best_arch]["label"],
            "method": "heuristic",
            "confidence": round(min(1.0, 0.6 + (best_score * 0.1)), 2),
            "specialists": validated_specs,
            "matched_keywords": matched[best_arch]
        }
        
    # Heuristic is ambiguous: try semantic fallback if permitted
    if allow_semantic:
        sem_res = classify_project_semantic(task_desc)
        if sem_res:
            sem_arch, sem_conf, sem_reason = sem_res
            validated_specs = _validate_specialists(SQUAD_ARCHETYPES[sem_arch]["specialists"], agents_dir)
            return {
                "archetype": sem_arch,
                "label": SQUAD_ARCHETYPES[sem_arch]["label"],
                "method": "semantic",
                "confidence": sem_conf,
                "reasoning": sem_reason,
                "specialists": validated_specs
            }
            
    # Safe generalist fallback
    arch = "general"
    validated_specs = _validate_specialists(SQUAD_ARCHETYPES[arch]["specialists"], agents_dir)
    return {
        "archetype": arch,
        "label": SQUAD_ARCHETYPES[arch]["label"],
        "method": "fallback",
        "confidence": 0.5,
        "specialists": validated_specs,
        "ambiguous": True
    }

# Thread synchronization locks (RLock for re-entrant thread safety)
state_lock = threading.RLock()
budget_lock = threading.RLock()
settings_lock = threading.RLock()

CONFIG_ALLOWLIST = {
    'provider',
    'openrouter_model',
    'opencode_url',
    'opencode_ai_provider',
    'opencode_model',
    'openrouter_key'
}

# Scan available agents count without fabrication
initial_agent_inventory = discover_agents()
total_agents = initial_agent_inventory["total"]

# Global workflow state
workflow_state = {
    "session_id": None,
    "execution_id": None,
    "status": "idle",  # idle | running | approval_needed | completed | failed
    "current_task": None,
    "squad_plan": None,
    "active_squad": [],
    "feed": [],
    "departments": {
        "strategy": {"status": "idle", "text": "في انتظار بدء المشروع"},
        "product": {"status": "idle", "text": "في انتظار بدء المشروع"},
        "engineering": {"status": "idle", "text": "في انتظار بدء المشروع"},
        "design": {"status": "idle", "text": "في انتظار بدء المشروع"},
        "development": {"status": "idle", "text": "في انتظار بدء المشروع"},
        "qa": {"status": "idle", "text": "في انتظار بدء المشروع"},
        "seo": {"status": "idle", "text": "في انتظار بدء المشروع"},
        "animation": {"status": "idle", "text": "في انتظار بدء المشروع"}
    },
    "outputs": {
        "strategy": "",
        "product": "",
        "engineering": "",
        "design": "",
        "development": "",
        "qa": "",
        "qa_specialists": "",
        "seo": "",
        "animation": ""
    },
    "approval_request": None,
    "stats": {
        "total_agents": total_agents,
        "active_agents": 0,
        "pending_approvals": 0
    },
    "blockers": [],
    "qa_report": None
}

def sync_task_to_db(session_id, stage, agent_id, status, output=None, error=None):
    """Synchronize workflow execution state to the SQLite task DAG."""
    if not session_id:
        return
    try:
        tasks = db_store.list_tasks_for_session(session_id)
        matching = [t for t in tasks if t["stage"] == stage and t["assigned_agent_id"] == agent_id]
        if matching:
            db_store.update_task_status(matching[0]["task_id"], status, output_text=output, error_message=error)
    except Exception as e:
        logging.warning("Failed to sync task %s (%s) to DB: %s", agent_id, stage, e)

def get_agent_prompt_meta(agent_id):
    """Resolve prompt_file, role_name, color for an agent ID."""
    with state_lock:
        sp_list = list(workflow_state.get("squad_plan", {}).get("specialists", [])) if workflow_state.get("squad_plan") else []
        for s in workflow_state.get("active_squad", []):
            if s.get("id") == agent_id and s not in sp_list:
                sp_list.append(s)
    for sp in sp_list:
        if sp.get("id") == agent_id and (sp.get("prompt_file") or sp.get("name")):
            pf = sp.get("prompt_file") or f"{agent_id}.md"
            nm = sp.get("name") or sp.get("display_name") or agent_id
            cl = sp.get("color") or "linear-gradient(135deg, #667eea, #764ba2)"
            return pf, nm, cl

    known_defaults = {
        "business-strategist": (r"specialized\business-strategist.md", "مستشار البزنس", "linear-gradient(135deg, #FF6B6B, #FF8E53)"),
        "product-manager": (r"product\product-manager.md", "مدير المنتج", "linear-gradient(135deg, #4facfe, #00f2fe)"),
        "engineering-software-architect": (r"engineering\engineering-software-architect.md", "المهندس المعماري", "linear-gradient(135deg, #20E2D7, #F9FEA5)"),
        "design-ui-designer": (r"design\design-ui-designer.md", "مصمم الواجهات", "linear-gradient(135deg, #a18cd1, #fbc2eb)"),
        "engineering-senior-developer": (r"engineering\engineering-senior-developer.md", "كبير المبرمجين", "linear-gradient(135deg, #11998e, #38ef7d)"),
        "engineering-code-reviewer": (r"engineering\engineering-code-reviewer.md", "مهندس الجودة", "linear-gradient(135deg, #FF9A9E, #FECFEF)"),
        "design-whimsy-injector": (r"design\design-whimsy-injector.md", "خبير الأنيميشن", "linear-gradient(135deg, #ff758c, #ff7eb3)"),
        "marketing-seo-specialist": (r"marketing\marketing-seo-specialist.md", "خبير SEO", "linear-gradient(135deg, #f093fb, #f5576c)"),
        "security-appsec-engineer": (r"security\security-appsec-engineer.md", "مهندس أمن التطبيقات", "red")
    }
    if agent_id in known_defaults:
        return known_defaults[agent_id]
        
    inv = discover_agents()
    for a in inv.get("agents", []):
        if a["id"] == agent_id:
            return a.get("relative_path", f"{agent_id}.md"), a.get("name", agent_id), "linear-gradient(135deg, #667eea, #764ba2)"
            
    return f"{agent_id}.md", agent_id, "linear-gradient(135deg, #667eea, #764ba2)"


# --- Secrets and Configuration Security ---

def mask_secret(secret_str):
    if not secret_str:
        return ""
    if len(secret_str) <= 8:
        return "********"
    return f"{secret_str[:4]}...{secret_str[-4:]}"

def load_settings():
    with settings_lock:
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"[WARN] Error reading {SETTINGS_FILE}: {e}")
        return {
            "provider": "gemini",
            "openrouter_model": "gemini-3.5-flash-lite",
            "opencode_url": "http://127.0.0.1:4096",
            "opencode_ai_provider": "anthropic",
            "opencode_model": "claude-haiku-4-5",
            "openrouter_key": ""
        }

def get_api_key(provider):
    """
    Retrieve API key prioritizing Environment Variables, then settings.json.
    Never returns hardcoded fallbacks.
    """
    if provider == 'gemini':
        env_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
        if env_key:
            return env_key.strip()
    elif provider == 'openrouter':
        env_key = os.environ.get('OPENROUTER_API_KEY')
        if env_key:
            return env_key.strip()
    
    settings = load_settings()
    return settings.get('openrouter_key', '').strip()

def get_safe_settings_view():
    settings = load_settings()
    provider = settings.get("provider", "gemini")
    raw_key = get_api_key(provider)
    is_env = bool(
        (provider == 'gemini' and (os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'))) or
        (provider == 'openrouter' and os.environ.get('OPENROUTER_API_KEY'))
    )
    
    return {
        "provider": provider,
        "openrouter_model": settings.get("openrouter_model", "gemini-3.5-flash-lite"),
        "opencode_url": settings.get("opencode_url", "http://127.0.0.1:4096"),
        "opencode_ai_provider": settings.get("opencode_ai_provider", "anthropic"),
        "opencode_model": settings.get("opencode_model", "claude-haiku-4-5"),
        "has_key": bool(raw_key),
        "key_masked": mask_secret(raw_key),
        "key_source": "environment" if is_env else ("settings_file" if raw_key else "none")
    }

# --- Budget & Token Accounting ---

def load_budget():
    with budget_lock:
        if os.path.exists(BUDGET_FILE):
            try:
                with open(BUDGET_FILE, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        return json.loads(content)
            except Exception as e:
                print(f"[ERROR] Corrupt budget file detected: {e}")
                # Attempt recovery from backup
                bak_path = BUDGET_FILE + ".bak"
                if os.path.exists(bak_path):
                    try:
                        with open(bak_path, 'r', encoding='utf-8') as bf:
                            bak_data = json.load(bf)
                            print("[INFO] Restored budget from backup copy")
                            return bak_data
                    except Exception:
                        pass
                # Preserve corrupt file for audit rather than silently erasing
                corrupt_copy = f"{BUDGET_FILE}.corrupt.{int(time.time())}"
                try:
                    os.replace(BUDGET_FILE, corrupt_copy)
                    print(f"[WARN] Preserved corrupted budget as {corrupt_copy}")
                except Exception:
                    pass
        return {
            "tokens_used": 0,
            "estimated_cost": 0.0,
            "budget_limit": 50.0,
            "transactions": []
        }

def save_budget(data):
    with budget_lock:
        try:
            # Create backup before atomic write
            if os.path.exists(BUDGET_FILE):
                try:
                    with open(BUDGET_FILE, 'r', encoding='utf-8') as sf, open(BUDGET_FILE + ".bak", 'w', encoding='utf-8') as df:
                        df.write(sf.read())
                except Exception:
                    pass
            atomic_write_json(BUDGET_FILE, data)
        except Exception as e:
            print(f"[ERROR] Error saving budget: {e}")

def record_token_usage(agent_name, dept_name, task_name, tokens_count, source="provider_reported"):
    """
    Safely record token consumption without race conditions or double-counting.
    Uses atomic persistence and records unique transaction IDs.
    """
    with budget_lock:
        budget = load_budget()
        try:
            tokens_count = max(int(tokens_count or 0), 0)
        except (ValueError, TypeError):
            tokens_count = 0
        budget["tokens_used"] = budget.get("tokens_used", 0) + tokens_count
        
        # Gemini Flash token pricing estimate: ~$0.15 per 1M tokens
        cost = round((tokens_count / 1_000_000.0) * 0.15, 4)
        if cost == 0 and tokens_count > 0:
            cost = 0.0001
        budget["estimated_cost"] = round(budget.get("estimated_cost", 0.0) + cost, 4)
        
        tx = {
            "id": int(time.time() * 1000) + len(budget.get("transactions", [])),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "agent": agent_name,
            "department": dept_name,
            "task": (task_name[:35] + '...') if task_name and len(task_name) > 35 else (task_name or "مهمة برمجية"),
            "tokens": tokens_count,
            "cost": cost,
            "source": source
        }
        tx_list = budget.get("transactions", [])
        tx_list.insert(0, tx)
        budget["transactions"] = tx_list[:60]
        
        save_budget(budget)
        return budget

# --- Filesystem Safety Helpers ---

def safe_resolve_project_dir(project_name):
    """
    Strictly validate that project_name is alphanumeric/safe and resolves
    strictly within GENERATED_APPS_DIR (Prevents path traversal).
    Also guards against Windows reserved device names (CON, NUL, AUX, PRN, etc.).
    """
    if not project_name:
        return None
    # Block Windows reserved device names
    base_check = project_name.split('.')[0].strip().upper()
    if base_check in WINDOWS_RESERVED:
        return None
    # Allow alphanumeric, arabic, underscores, hyphens
    if not re.match(r'^[a-zA-Z0-9_\u0600-\u06FF -]{1,100}$', project_name):
        return None
    target_dir = os.path.abspath(os.path.join(GENERATED_APPS_DIR, project_name))
    try:
        # Canonical containment: resolve symlinks and junctions before comparing
        canonical_root = os.path.realpath(GENERATED_APPS_DIR)
        canonical_target = os.path.realpath(target_dir)
        common = os.path.commonpath([canonical_root, canonical_target])
        if common != canonical_root or canonical_target == canonical_root:
            return None
    except (ValueError, OSError):
        return None
    return target_dir

def get_project_folder_name(task_name):
    if not task_name:
        return os.path.join(GENERATED_APPS_DIR, f"Project_{int(time.time())}"), f"Project_{int(time.time())}"
    
    lines = [line.strip() for line in task_name.splitlines() if line.strip()]
    first_title = ""
    for line in lines:
        cleaned_line = re.sub(r'^[#*\-=\s]+', '', line).strip()
        if cleaned_line and len(cleaned_line) > 3 and not cleaned_line.lower().startswith('client project brief'):
            first_title = cleaned_line
            break
        elif cleaned_line and len(cleaned_line) > 3 and not first_title:
            first_title = cleaned_line
            
    if not first_title:
        first_title = lines[0] if lines else "Project"
        
    clean = re.sub(r'[\\/*?:"<>|#\n\r\t]', '', first_title).strip()
    clean = re.sub(r'[^a-zA-Z0-9_\u0600-\u06FF\s-]', '', clean)
    clean = re.sub(r'\s+', '_', clean).strip('._-')
    
    # Restrict folder name length to maximum 35 characters to guarantee Windows MAX_PATH safety
    if len(clean) > 35:
        clean = clean[:35].rstrip('._-')
    if not clean:
        clean = f"Project_{int(time.time())}"
        
    base_folder = os.path.join(GENERATED_APPS_DIR, clean)
    if not os.path.exists(base_folder):
        return base_folder, clean
    timestamped = f"{clean}_{int(time.time())}"
    return os.path.join(GENERATED_APPS_DIR, timestamped), timestamped

# --- Workflow State Management ---

def update_stats():
    with state_lock:
        active_squad_count = 0
        dept_squad_sizes = {
            "strategy": 4,
            "product": 3,
            "engineering": 5,
            "design": 4,
            "development": 6,
            "qa": 3,
            "animation": 3,
            "seo": 2
        }
        if "departments" in workflow_state and isinstance(workflow_state["departments"], dict):
            for dept_key, dept_data in workflow_state["departments"].items():
                if isinstance(dept_data, dict) and dept_data.get("status") == "active":
                    active_squad_count += dept_squad_sizes.get(dept_key, 3)
                
        if "stats" not in workflow_state or not isinstance(workflow_state["stats"], dict):
            workflow_state["stats"] = {
                "total_agents": total_agents,
                "active_agents": 0,
                "pending_approvals": 0
            }
        workflow_state["stats"]["active_agents"] = active_squad_count
        workflow_state["stats"]["pending_approvals"] = 1 if workflow_state.get("status") == "approval_needed" else 0

def add_feed(name, role, color, initial, text):
    with state_lock:
        workflow_state["feed"].insert(0, {
            "name": name,
            "role": role,
            "color": color,
            "initial": initial,
            "text": text,
            "time": "الآن",
            "id": int(time.time() * 1000)
        })

# --- LLM Providers & Agent Execution ---

_last_agent_execution_ctx = threading.local()

def get_last_agent_execution() -> Optional[Dict[str, Any]]:
    """Return the usage metadata from the most recent agent execution in the current thread."""
    return getattr(_last_agent_execution_ctx, "data", None)

def set_last_agent_execution(tokens_used: int, token_source: str, provider: str = "unknown", provider_request_id: Optional[str] = None):
    """Durable in-memory record of provider usage metadata for orchestration layers."""
    _last_agent_execution_ctx.data = {
        "tokens_used": max(int(tokens_used), 0),
        "token_source": token_source,
        "provider": provider,
        "provider_request_id": provider_request_id
    }

def call_agent_gemini(system_prompt, full_prompt, api_key, role_name, preferred_model='gemini-3.5-flash-lite'):
    """
    Call Google Gemini REST API directly. Returns (reply, token_count, True).
    """
    headers = {"Content-Type": "application/json"}
    data = {
        "contents": [{
            "parts": [{"text": f"System Guidelines:\n{system_prompt}\n\nTask:\n{full_prompt}\n\n(أجب باللغة العربية باحترافية كاملة. إذا طلب منك كتابة كود، أرجع الأكواد مفصولة داخل: ```html و ```css و ```javascript بالتفصيل الكامل بدون أي اختصار)."}]
        }]
    }
    models = [preferred_model, 'gemini-3.5-flash-lite', 'gemini-3.5-flash', 'gemini-flash-latest']
    seen = set()
    models_to_try = [m for m in models if not (m in seen or seen.add(m))]
    
    last_err = ""
    for cur_m in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{cur_m}:generateContent?key={api_key}"
        try:
            res = requests.post(url, headers=headers, json=data, timeout=75)
            res_json = res.json()
            if 'error' not in res_json and 'candidates' in res_json and len(res_json['candidates']) > 0:
                reply = res_json['candidates'][0]['content']['parts'][0]['text']
                usage_meta = res_json.get('usageMetadata', {})
                if 'totalTokenCount' in usage_meta:
                    try:
                        token_count = max(0, int(usage_meta['totalTokenCount'] or 0))
                    except (ValueError, TypeError):
                        token_count = 0
                    is_reported = True
                else:
                    token_count = max(1, (len(system_prompt or "") + len(full_prompt or "") + len(reply or "")) // 4)
                    is_reported = False
                
                with state_lock:
                    task_name = workflow_state.get('current_task', 'تطبيق ويب')
                
                # Phase 2.6-A1: Extract genuine provider request ID from HTTP headers or payload
                provider_req_id = res.headers.get('x-goog-request-id') or res.headers.get('x-request-id') or res_json.get('id')
                source_tag = "provider_reported" if is_reported else "estimated"
                set_last_agent_execution(token_count, source_tag, "gemini", provider_request_id=provider_req_id)

                # Record token usage ONCE with source tag
                record_token_usage(
                    role_name,
                    "البرمجة والتطوير",
                    task_name,
                    token_count,
                    source=source_tag
                )
                return reply, token_count, True
                
            err_msg = res_json.get('error', {}).get('message', str(res_json.get('error', '')))
            last_err = err_msg
            time.sleep(1)
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
            
    raise RuntimeError(f"فشل الاتصال بـ Gemini: {last_err}")

def call_agent(agent_file, role_name, short_name, color, prompt, context_text="", return_metadata=False):
    """
    Orchestrate specialized agent invocation with honest failure handling.
    Returns string response (or (reply, tokens_used, token_source) if return_metadata=True).
    Raises RuntimeError on genuine failure.
    """
    default_prompt = f"أنت {role_name} محترف."
    system_prompt, loaded_ok, load_err = safe_load_agent_prompt(agent_file, fallback_prompt=default_prompt)
    if not loaded_ok and load_err:
        logging.warning("Agent prompt load fallback for %s (%s): %s", role_name, agent_file, load_err)
            
    prompt_str = str(prompt or "")
    context_str = str(context_text or "")
    full_prompt = f"{prompt_str}\n\nالسياق:\n{context_str}" if context_str else prompt_str
    settings = load_settings()
    provider = settings.get('provider', 'gemini')
    api_key = get_api_key(provider)
    model = settings.get('openrouter_model', 'gemini-3.5-flash-lite' if provider == 'gemini' else 'openai/gpt-4o-mini')
    opencode_url = settings.get('opencode_url', 'http://127.0.0.1:4096')
    
    if provider == 'gemini':
        if not model or '3.8' in model or any(k in model.lower() for k in ['gpt', 'claude', 'openai', '1.5', '2.5']):
            model = 'gemini-3.5-flash-lite'
    elif provider == 'openrouter' and 'gemini' in model.lower() and '/' not in model:
        model = 'google/gemini-2.0-flash-001'
        
    # Honest validation: No API key -> Explicit failure, never simulated success!
    if not api_key and provider != 'opencode':
        err_msg = f"لم يتم العثور على مفتاح API صالح للمزود ({provider}). يرجى حفظ المفتاح في الإعدادات أو المتغيرات البيئية."
        add_feed("النظام", "Security Guard", "red", "SEC", err_msg)
        raise RuntimeError(err_msg)
        
    add_feed("النظام", "System", "linear-gradient(135deg, #2c3e50, #3498db)", "SY", f"جاري التفكير والتنفيذ بواسطة {role_name}...")
    
    tokens_recorded = False
    reply = ""
    
    try:
        if provider == 'opencode':
            # Local OpenCode connection with automatic failover
            try:
                oc = requests.Session()
                sess_r = oc.post(f"{opencode_url}/session", json={}, timeout=15)
                if sess_r.status_code != 200:
                    raise RuntimeError(f"فشل الاتصال بـ OpenCode server على {opencode_url}")
                oc_session_id = sess_r.json()['id']
                
                sys_trim = system_prompt[:1000] if len(system_prompt) > 1000 else system_prompt
                ctx_trim = context_text[:2500] if context_text and len(context_text) > 2500 else context_text
                
                if "مبرمج" in role_name or "أنيميشن" in role_name or short_name in ["DEV", "MOTION"]:
                    instruction = "(أنت تعمل في بيئة برمجية مؤتمتة وغير تفاعلية. أرجع الأكواد البرمجية مباشرة ومكتملة داخل: ```html و ```css و ```javascript فوراً في ردك)."
                else:
                    instruction = "(أجب باللغة العربية باحترافية واختصار مفيد. اعتمد فقط على المعطيات المرفقة)."
                    
                ctx_part = f"السياق:\n{ctx_trim}\n\n" if ctx_trim else ""
                full_msg = f"أنت {role_name}. {sys_trim[:300]}\n\nالمهمة: {prompt}\n\n{ctx_part}{instruction}"
                
                msg_r = oc.post(
                    f"{opencode_url}/session/{oc_session_id}/message",
                    json={'parts': [{'type': 'text', 'text': full_msg}], 'agent': 'general'},
                    timeout=200
                )
                if msg_r.status_code != 200:
                    raise RuntimeError(f"خطأ OpenCode HTTP {msg_r.status_code}")
                    
                oc_data = msg_r.json()
                if 'error' in oc_data.get('info', {}):
                    err_text = oc_data['info']['error'].get('data', {}).get('message', str(oc_data['info']['error']))
                    raise RuntimeError(err_text)
                    
                reply = ''.join(
                    part.get('text', '') for part in oc_data.get('parts', []) if part.get('type') == 'text'
                ).strip()
                
                if not reply:
                    raise RuntimeError("استجابة OpenCode كانت فارغة")
            except Exception as oc_err:
                gemini_key = get_api_key('gemini')
                if gemini_key:
                    add_feed("النظام", "System", "orange", "SY", "تأخر استجابة OpenCode، جاري الاسترداد التلقائي عبر Gemini...")
                    reply, _, tokens_recorded = call_agent_gemini(system_prompt, full_prompt, gemini_key, role_name, 'gemini-3.5-flash-lite')
                else:
                    raise oc_err
                    
        elif provider == 'gemini':
            reply, _, tokens_recorded = call_agent_gemini(system_prompt, full_prompt, api_key, role_name, model)
            
        elif provider == 'openrouter':
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            data = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"{full_prompt}\n(أجب باللغة العربية. قم بتوليد HTML داخل ```html و CSS داخل ```css و JS داخل ```javascript)"}
                ]
            }
            res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=90)
            res_json = res.json()
            if 'error' in res_json:
                err = res_json['error'].get('message', str(res_json['error']))
                gemini_key = get_api_key('gemini')
                if gemini_key and (api_key.startswith('AIzaSy') or gemini_key != api_key):
                    add_feed("النظام", "System", "orange", "SY", "فشل OpenRouter، جاري الاسترداد التلقائي عبر Gemini...")
                    reply, _, tokens_recorded = call_agent_gemini(system_prompt, full_prompt, gemini_key, role_name, 'gemini-3.5-flash-lite')
                else:
                    raise RuntimeError(f"OpenRouter Error: {err}")
            else:
                reply = res_json['choices'][0]['message']['content']
                usage = res_json.get('usage', {})
                # Phase 2.6-A1: Extract genuine provider request ID from OpenRouter response
                provider_req_id = res_json.get('id') or res.headers.get('x-request-id')
                if 'total_tokens' in usage:
                    try:
                        tok_cnt = max(0, int(usage.get('total_tokens') or 0))
                    except (ValueError, TypeError):
                        tok_cnt = 0
                    with state_lock:
                        cur_task = workflow_state.get('current_task', 'مهمة برمجية')
                    record_token_usage(role_name, short_name, cur_task, tok_cnt, source="provider_reported")
                    set_last_agent_execution(tok_cnt, "provider_reported", "openrouter", provider_request_id=provider_req_id)
                    tokens_recorded = True

        # If not recorded yet, calculate estimated tokens and record ONCE
        if not tokens_recorded:
            tokens_est = max(1, (len(prompt_str) + len(context_str) + len(reply or "")) // 4)
            with state_lock:
                cur_task = workflow_state.get('current_task', 'مهمة برمجية')
            record_token_usage(role_name, short_name, cur_task, tokens_est, source="estimated")
            set_last_agent_execution(tokens_est, "estimated", provider, provider_request_id=None)
            
        add_feed(role_name, role_name, color, short_name, "أنهيت عملي. المخرجات متوفرة الآن.")
        
        if return_metadata:
            meta = get_last_agent_execution() or {"tokens_used": 0, "token_source": "estimated"}
            return reply, meta["tokens_used"], meta["token_source"]
        return reply

    except Exception as e:
        safe_msg = db_store.sanitize_memory_content(str(e))[:150]
        add_feed(role_name, "Error", "red", "ERR", f"فشل: {safe_msg}")
        raise RuntimeError(f"فشل دور {role_name}: {safe_msg}")

# --- Autonomous Workflow Execution Engine ---

def run_workflow(task_desc, existing_session_id=None, call_agent_fn=None):
    squad_plan = None
    active_squad = []

    if existing_session_id:
        sess = db_store.get_session(existing_session_id)
        if sess:
            meta = sess.get("metadata") or {}
            squad_plan = meta.get("squad_plan")
            active_squad = meta.get("active_squad") or []
            if not task_desc:
                task_desc = sess.get("task_description", "")

    if not squad_plan:
        squad_plan = triage_project_squad(task_desc)
        active_squad = []
        for spec in squad_plan["specialists"]:
            active_squad.append({
                "id": spec["id"],
                "role_id": spec["id"],
                "name": spec["name"],
                "display_name": spec["display_name"],
                "category": spec["category"],
                "stage": spec["stage"],
                "reason": spec["reason"],
                "status": "selected"
            })

    # Initialize persistent session and build Directed Task Graph
    project_title = task_desc[:40].strip()
    session_id = existing_session_id
    if not session_id:
        try:
            session_id = db_store.create_session(
                project_name=project_title,
                task_description=task_desc,
                squad_archetype=squad_plan.get("archetype"),
                metadata={"squad_plan": squad_plan, "active_squad": active_squad}
            )
            db_store.update_session_status(session_id, "running", current_stage="strategy")
            orch.build_project_task_graph(session_id, project_title, task_desc, squad_plan)
        except Exception as e:
            logging.warning("Persistent session creation warning: %s", e)
    else:
        try:
            db_store.update_session_status(session_id, "running", current_stage="strategy")
        except Exception as e:
            logging.warning("Persistent session resume warning: %s", e)

    with state_lock:
        workflow_state["session_id"] = session_id
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_desc
        workflow_state["squad_plan"] = squad_plan
        workflow_state["active_squad"] = active_squad
        workflow_state["feed"] = []
        workflow_state["blockers"] = []
        workflow_state["qa_report"] = None
        
        workflow_state["departments"] = {
            "strategy": {"status": "active", "text": "يقوم بدراسة المشروع..."},
            "product": {"status": "idle", "text": "في انتظار بدء المشروع"},
            "engineering": {"status": "idle", "text": "في انتظار بدء المشروع"},
            "design": {"status": "idle", "text": "في انتظار بدء المشروع"},
            "development": {"status": "idle", "text": "في انتظار بدء المشروع"},
            "qa": {"status": "idle", "text": "في انتظار بدء المشروع"},
            "seo": {"status": "idle", "text": "في انتظار بدء المشروع"},
            "animation": {"status": "idle", "text": "في انتظار بدء المشروع"}
        }
        
    update_stats()

    if session_id and not existing_session_id:
        with state_lock:
            state_snap = copy.deepcopy(workflow_state)
        db_store.save_session_checkpoint(session_id, "workflow_started", state_snap)

    add_feed("النظام", "Squad Triage", "linear-gradient(135deg, #667eea, #764ba2)", "TRI", f"🎯 تم توجيه المشروع إلى تصنيف '{squad_plan['label']}'. تم اختيار الفريق المتخصص: " + "، ".join([s['name'] for s in active_squad]))
    add_feed("مستشار البزنس", "Business Strategist", "linear-gradient(135deg, #FF6B6B, #FF8E53)", "BS", f"بدأت في تحليل جدوى مشروع: {task_desc}")
    
    try:
        def _on_task_start(t):
            stg = t.get("stage", "strategy")
            with state_lock:
                if stg in workflow_state["departments"]:
                    workflow_state["departments"][stg] = {"status": "active", "text": f"يعمل على: {t.get('title')}"}
                for s in workflow_state.get("active_squad", []):
                    if s["id"] == t.get("assigned_agent_id"):
                        s["status"] = "running"
            update_stats()

        def _on_task_complete(t, out):
            stg = t.get("stage", "strategy")
            with state_lock:
                workflow_state["outputs"][stg] = out
                if stg in workflow_state["departments"]:
                    workflow_state["departments"][stg] = {"status": "done", "text": f"أنجز بنجاح: {t.get('title')}"}
                for s in workflow_state.get("active_squad", []):
                    if s["id"] == t.get("assigned_agent_id"):
                        s["status"] = "completed"
            update_stats()

        def _on_task_fail(t, err):
            stg = t.get("stage", "strategy")
            with state_lock:
                if stg in workflow_state["departments"]:
                    workflow_state["departments"][stg] = {"status": "failed", "text": f"تعثر: {err}"}
                for s in workflow_state.get("active_squad", []):
                    if s["id"] == t.get("assigned_agent_id"):
                        s["status"] = "failed"
            update_stats()

        # Run Pre-PRD stages: strategy and product via verified DAG execution
        effective_call_agent = call_agent_fn or call_agent
        success, completed_ids, err = orch.run_dag_stages(
            session_id=session_id,
            allowed_stages=["strategy", "product"],
            call_agent_fn=effective_call_agent,
            agent_prompt_resolver_fn=get_agent_prompt_meta,
            add_feed_fn=add_feed,
            on_task_start_fn=_on_task_start,
            on_task_complete_fn=_on_task_complete,
            on_task_fail_fn=_on_task_fail
        )

        if not success:
            raise RuntimeError(err or "فشل تنفيذ مراحل الاستراتيجية والمنتج عبر الـ DAG.")

        # Reached PRD approval gate
        with state_lock:
            workflow_state["departments"]["product"] = {"status": "done", "text": "تم كتابة وثيقة المتطلبات (PRD)."}
            workflow_state["departments"]["engineering"] = {"status": "waiting", "text": "في انتظار الاعتماد للبدء..."}
            workflow_state["status"] = "approval_needed"
            workflow_state["approval_request"] = {
                "title": "مطلوب اعتماد المتطلبات التقنية",
                "message": "نطلب موافقتك على البدء في هندسة النظام وتصميمه بناءً على وثيقة المتطلبات التي جهزها فريق المنتج.",
                "role": "مدير المنتج"
            }
        update_stats()

        # Save durable checkpoint at PRD approval gate
        if session_id:
            with state_lock:
                state_snap = copy.deepcopy(workflow_state)
            db_store.save_session_checkpoint(session_id, "prd_approval_gate", state_snap)
            db_store.update_session_status(session_id, "approval_needed", checkpoint_step="prd_approval_gate")
        
    except Exception as e:
        with state_lock:
            workflow_state["status"] = "failed"
            workflow_state["blockers"].append(str(e))
            for d in ["strategy", "product"]:
                if workflow_state["departments"][d]["status"] == "active":
                    workflow_state["departments"][d]["status"] = "failed"
                    workflow_state["departments"][d]["text"] = "تعثر التنفيذ بسبب خطأ"
            for s in workflow_state.get("active_squad", []):
                if s.get("status") == "running":
                    s["status"] = "failed"
        if session_id:
            db_store.update_session_status(session_id, "failed", error_message=str(e))
        update_stats()
        add_feed("النظام", "System", "red", "ERR", f"توقف سير العمل: {str(e)}")

class StrictHTMLStructureValidator(HTMLParser):
    """
    Production HTML Structural and Nesting Validator:
    Tracks opening and closing tags using a LIFO stack.
    Validates nesting hierarchy, detects mismatched tags, checks for unclosed elements,
    and enforces required top-level document elements (html, head, body).
    Correctly recognizes legitimate HTML5 void elements.

    Scope limitation notice:
    This validator enforces structural nesting and required document elements.
    It does not claim full HTML5 living standard conformance validation (which requires
    a full WHATWG compliant parser like html5lib).
    """
    VOID_TAGS = {
        'area', 'base', 'br', 'col', 'embed', 'hr', 'img',
        'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'
    }

    def __init__(self):
        super().__init__()
        self.tag_stack = []
        self.has_html = False
        self.has_head = False
        self.has_body = False
        self.head_closed = False
        self.errors = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self.VOID_TAGS:
            return

        if t == 'html':
            if self.has_html:
                self.errors.append("Multiple <html> root elements detected")
            if self.tag_stack:
                self.errors.append(f"<html> element cannot be nested inside <{self.tag_stack[-1]}>")
            self.has_html = True
        elif t == 'head':
            if self.has_head:
                self.errors.append("Multiple <head> elements detected")
            if not self.has_html or 'html' not in self.tag_stack:
                self.errors.append("<head> element must be nested inside <html>")
            if self.has_body:
                self.errors.append("<head> element cannot appear after <body>")
            self.has_head = True
        elif t == 'body':
            if self.has_body:
                self.errors.append("Multiple <body> elements detected")
            if not self.has_html or 'html' not in self.tag_stack:
                self.errors.append("<body> element must be nested inside <html>")
            self.has_body = True
        else:
            if not self.has_html:
                self.errors.append(f"Element <{t}> cannot appear outside <html> element")

        self.tag_stack.append(t)

    def handle_startendtag(self, tag, attrs):
        t = tag.lower()
        if t not in self.VOID_TAGS:
            self.errors.append(f"Non-void element '<{t}>' cannot use self-closing XML syntax ('<{t}/>') in HTML")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self.VOID_TAGS:
            self.errors.append(f"Void element '<{t}>' must not have a closing tag ('</{t}>')")
            return

        if not self.tag_stack:
            self.errors.append(f"Unexpected closing tag '</{t}>' with no matching open tag")
            return

        if self.tag_stack[-1] == t:
            self.tag_stack.pop()
            if t == 'head':
                self.head_closed = True
        else:
            expected = self.tag_stack[-1]
            self.errors.append(f"Mismatched closing tag '</{t}>', expected '</{expected}>'")
            if t in self.tag_stack:
                while self.tag_stack and self.tag_stack[-1] != t:
                    unclosed = self.tag_stack.pop()
                    self.errors.append(f"Unclosed element '<{unclosed}>' before closing '</{t}>'")
                if self.tag_stack:
                    self.tag_stack.pop()
            else:
                self.errors.append(f"Closing tag '</{t}>' does not match any open element")

    def close(self):
        super().close()
        if self.tag_stack:
            for unclosed in reversed(self.tag_stack):
                self.errors.append(f"Unclosed element '<{unclosed}>' at end of document")
        if not self.has_html:
            self.errors.append("Missing required document element: <html>")
        if not self.has_head:
            self.errors.append("Missing required document element: <head>")
        if not self.has_body:
            self.errors.append("Missing required document element: <body>")

def verify_js_syntax(js_code):
    """
    Validate JavaScript syntax using Node.js vm.Script without executing the code.
    Compiles the code into a script AST and checks syntax without running it.

    Returns:
        (status, error_message)
        status: "valid" | "invalid" | "unavailable"
    """
    if not js_code or not isinstance(js_code, str) or not js_code.strip():
        return "invalid", "JavaScript code is missing or empty"
    try:
        res = subprocess.run(
            ["node", "-e", "const vm = require('vm'); try { new vm.Script(process.argv[1]); process.exit(0); } catch(e) { console.error(e.message); process.exit(1); }", js_code],
            capture_output=True,
            text=True,
            timeout=5
        )
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip() or "Syntax error in JavaScript"
            return "invalid", err
        return "valid", None
    except FileNotFoundError:
        return "unavailable", "Node.js executable ('node') was not found on system PATH"
    except subprocess.TimeoutExpired:
        return "unavailable", "JavaScript syntax validation timed out after 5 seconds"
    except Exception as e:
        return "unavailable", f"Failed to invoke Node.js syntax validator: {str(e)}"

def _strip_css_strings_and_comments(css_text):
    """
    Remove block comments and mask quoted strings from CSS text,
    replacing comments with whitespace and string contents with safe characters
    to prevent semicolons, braces, and colons from disrupting structural analysis
    while preserving non-empty value presence. Returns (stripped_text, errors).
    """
    errors = []
    result = []
    i = 0
    n = len(css_text)
    while i < n:
        # Block comment
        if css_text[i:i+2] == '/*':
            end = css_text.find('*/', i + 2)
            if end == -1:
                errors.append("CSS has unclosed block comments (/* ... */)")
                result.append(' ' * (n - i))
                i = n
            else:
                result.append(' ' * (end + 2 - i))
                i = end + 2
        # Quoted strings
        elif css_text[i] in ('"', "'"):
            quote = css_text[i]
            j = i + 1
            closed = False
            while j < n:
                if css_text[j] == '\\' and j + 1 < n:
                    j += 2
                elif css_text[j] == quote:
                    j += 1
                    closed = True
                    break
                elif css_text[j] == '\n':
                    errors.append(f"CSS has unterminated quoted string starting near position {i}")
                    break
                else:
                    j += 1
            if not closed and not any(f"position {i}" in e for e in errors):
                errors.append(f"CSS has unterminated quoted string starting near position {i}")
            if closed:
                # Keep surrounding quotes, replace contents with 'x' of same length
                inner_len = max(0, j - i - 2)
                result.append(quote + ('x' * inner_len) + quote)
            else:
                result.append(' ' * (j - i))
            i = j
        else:
            result.append(css_text[i])
            i += 1
    return ''.join(result), errors


def validate_css_structure(css_text):
    """
    Production CSS Structural and Declaration Validator:
    Validates structural integrity, brace balance, comment closure, quote closure,
    and declaration-level key:value syntax within leaf rule blocks.

    Scope limitation notice:
    This check validates structural and lexical declarations after stripping
    comments and masking quoted strings. It does not perform full CSS3/CSS4 AST grammar
    parsing (which requires external dependencies). Characters inside quoted
    strings (e.g. semicolons, braces, colons) are safely masked so they do not
    interfere with structural or declaration analysis.
    """
    errors = []
    if not css_text or not isinstance(css_text, str) or not css_text.strip():
        return False, ["Required CSS artifact is missing or empty"]

    # Strip comments and mask strings first so delimiters inside them don't confuse analysis
    stripped, strip_errors = _strip_css_strings_and_comments(css_text)
    errors.extend(strip_errors)

    # Brace balance check on stripped text
    if stripped.count('{') != stripped.count('}'):
        errors.append(f"CSS has unbalanced braces ({{: {stripped.count('{')}, }}: {stripped.count('}')})")

    depth = 0
    for char in stripped:
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth < 0:
                errors.append("CSS contains unexpected closing brace '}' before opening brace")
                break
    if depth > 0 and not any("unbalanced braces" in e for e in errors):
        errors.append("CSS contains unclosed rule block")

    # Check for malformed declarations inside leaf blocks (blocks containing no nested braces)
    leaf_blocks = re.findall(r'\{([^{}]+)\}', stripped)
    for block in leaf_blocks:
        decls = block.split(';')
        for decl in decls:
            decl_str = decl.strip()
            if not decl_str:
                continue
            if ':' not in decl_str:
                if not decl_str.startswith('@') and not decl_str.startswith('&'):
                    errors.append(f"CSS malformed declaration missing colon: '{decl_str}'")
            else:
                prop, _, val = decl_str.partition(':')
                if not prop.strip():
                    errors.append(f"CSS malformed declaration missing property name: '{decl_str}'")
                elif not val.strip():
                    errors.append(f"CSS malformed declaration missing property value: '{decl_str}'")

    return len(errors) == 0, errors


def build_artifact_manifest(
    candidate_files: Dict[str, Union[str, bytes]],
    qa_run_id: Optional[str] = None,
    delivery_id: Optional[str] = None,
    validation_status: str = "passed"
) -> Dict[str, Any]:
    """
    Build a deterministic, validated artifact manifest linking verified candidate files
    to publication output. Rejects path traversal, absolute paths, null bytes, and symlink escapes.
    Computes exact byte size, SHA-256 digest, and artifact type for each file.
    """
    manifest_items = []
    seen_paths_lower = set()
    MAX_ARTIFACT_SIZE = 25 * 1024 * 1024  # 25 MB max per artifact
    
    # Sort files deterministically by relative path
    for rel_path in sorted(candidate_files.keys()):
        if not rel_path or not isinstance(rel_path, str) or "\0" in rel_path:
            raise ValueError(f"Invalid artifact relative path: {rel_path!r}")
        
        import unicodedata
        norm_unicode = unicodedata.normalize("NFC", rel_path)
        norm_path = os.path.normpath(norm_unicode).replace("\\", "/")
        lower_path = norm_path.lower()
        if lower_path in seen_paths_lower:
            raise ValueError(f"Case-insensitive or Unicode normalization artifact collision: {rel_path}")
        seen_paths_lower.add(lower_path)

        if (os.path.isabs(norm_path) or norm_path.startswith("/") or
                rel_path.startswith("\\\\") or rel_path.startswith("//") or norm_path.startswith("//")):
            raise ValueError(f"Unsafe artifact path (absolute or UNC): {rel_path}")
        if ":" in norm_path or ":" in rel_path:
            raise ValueError(f"Unsafe artifact path (drive escape or alternate stream): {rel_path}")
        if (norm_path == ".." or norm_path.startswith("../") or 
                "/../" in norm_path or norm_path.endswith("/..")):
            raise ValueError(f"Path traversal detected in artifact manifest: {rel_path}")

        # Reject Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
        reserved_devices = {
            "CON", "PRN", "AUX", "NUL",
            "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
        }
        for segment in norm_path.split("/"):
            base_part = segment.split(".")[0].upper()
            if base_part in reserved_devices:
                raise ValueError(f"Unsafe artifact path (reserved device name): {rel_path}")
        
        content = candidate_files[rel_path]
        if isinstance(content, str):
            content_bytes = content.encode("utf-8")
        elif isinstance(content, bytes):
            content_bytes = content
        else:
            raise ValueError(f"Artifact content for {rel_path} must be str or bytes, got {type(content)}")
        
        file_size = len(content_bytes)
        if file_size > MAX_ARTIFACT_SIZE:
            raise ValueError(f"Artifact {rel_path} exceeds maximum allowed size ({file_size} > {MAX_ARTIFACT_SIZE} bytes)")
        
        sha256_hash = hashlib.sha256(content_bytes).hexdigest()
        
        ext = os.path.splitext(norm_path)[1].lower()
        if ext == ".html":
            art_type = "document"
        elif ext == ".css":
            art_type = "stylesheet"
        elif ext == ".js":
            art_type = "script"
        elif ext == ".json":
            art_type = "metadata"
        elif ext in (".md", ".txt"):
            art_type = "documentation"
        else:
            art_type = "asset"
            
        manifest_items.append({
            "relative_path": norm_path,
            "file_size": file_size,
            "sha256": sha256_hash,
            "artifact_type": art_type,
            "validation_status": validation_status,
            "qa_run_id": qa_run_id,
            "delivery_id": delivery_id
        })
        
    return {
        "manifest_version": "1.0",
        "qa_run_id": qa_run_id,
        "delivery_id": delivery_id,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_artifacts": len(manifest_items),
        "artifacts": manifest_items
    }


def run_qa_checks(app_html, app_css="", app_js=""):
    """
    Production QA Gate Entry Point:
    Directly testable structural, syntactic, security, and asset completeness verification.
    Explicitly rejects missing or empty required HTML, CSS, and JS assets.
    Verifies HTML structure using StrictHTMLStructureValidator, validates JS syntax via Node parser,
    inspects local asset references for path traversal escapes, and returns a structured report
    with a deterministic candidate artifact manifest.
    """
    errors = []
    qa_run_id = f"qa_run_{uuid.uuid4().hex[:12]}"
    
    # 1. HTML Verification
    if not app_html or not isinstance(app_html, str) or not app_html.strip():
        errors.append("Required HTML artifact is missing or empty")
    elif len(app_html.strip()) < 50:
        errors.append("HTML document is truncated / too short (under 50 characters)")
    else:
        lower_html = app_html.lower()
        has_doctype = "<!doctype html" in lower_html
        if not has_doctype:
            errors.append("HTML document missing <!DOCTYPE html> declaration (invalid document structure)")
        
        validator = StrictHTMLStructureValidator()
        try:
            validator.feed(app_html)
            validator.close()
            errors.extend(validator.errors)
        except Exception as e:
            errors.append(f"HTML parsing failed with syntax error: {str(e)}")
            
        fatal_strings = [
            "فشل الاتصال", "read timed out", "connectionerror", 
            "traceback (most recent call last)", "500 internal server error",
            "404 not found"
        ]
        for fatal in fatal_strings:
            if fatal in lower_html:
                errors.append(f"Fatal error detected in HTML: '{fatal}'")
                break

    # 2. CSS Verification
    css_ok, css_errs = validate_css_structure(app_css)
    if not css_ok:
        errors.extend(css_errs)

    # 3. JavaScript Verification
    #    Uses the Node.js vm.Script AST compiler exclusively.
    #    Raw delimiter counting was removed because characters inside strings,
    #    comments, template literals and regexes cause false positives
    #    (e.g. `const msg = "{";` is valid JS with unequal brace counts).
    if not app_js or not isinstance(app_js, str) or not app_js.strip():
        errors.append("Required JavaScript artifact is missing or empty")
        js_status = "invalid"
    else:
        # Node.js syntax verification (sole JS validation gate)
        js_status, js_err = verify_js_syntax(app_js)
        if js_status == "invalid":
            errors.append(f"JavaScript syntax error: {js_err}")
        elif js_status == "unavailable":
            errors.append(f"JavaScript syntax validator unavailable: Node.js runtime is required for QA gate verification ({js_err})")

    # 4. Safe Relative Asset & Protocol References Check
    traversal_patterns = [
        (re.compile(r'(?:src|href|action)\s*=\s*["\']\s*(?:\.\.[/\\]|[/\\]\.\.[/\\])', re.IGNORECASE), "Path traversal reference detected in HTML asset links"),
        (re.compile(r'(?:src|href|action)\s*=\s*["\']\s*file:', re.IGNORECASE), "Disallowed file:// protocol reference detected in HTML"),
        (re.compile(r'(?:src|href|action)\s*=\s*["\']\s*(?:(?:javascript|vbscript):|data:\s*text/html)', re.IGNORECASE), "Disallowed dangerous URL scheme detected in HTML attributes"),
        (re.compile(r'@import\s+["\'](?:\.\.[/\\]|[/\\]\.\.[/\\])', re.IGNORECASE), "Path traversal detected in CSS @import"),
    ]
    for pat, desc in traversal_patterns:
        if pat.search(app_html or ""):
            errors.append(desc)
            break
        if pat.search(app_css or ""):
            errors.append(desc)
            break

    passed = len(errors) == 0
    checks = {
        "html_extracted": bool(app_html and len(app_html.strip()) >= 50),
        "html_doctype_or_tags": not any("HTML" in e or "<html>" in e or "<head>" in e or "<body>" in e for e in errors),
        "html_structure_valid": not any("HTML" in e or "tag" in e.lower() or "element" in e.lower() for e in errors),
        "no_fatal_error_strings": not any("Fatal error" in e for e in errors),
        "css_syntax_balanced": css_ok,
        "css_structural_validity": css_ok,
        "js_syntax_balanced": (js_status == "valid") and not any("bracket" in e or "brace" in e or "parenthese" in e for e in errors),
        "js_syntax_validated": (js_status == "valid"),
        "safe_asset_references": not any("Path traversal" in e or "file://" in e for e in errors)
    }

    # Deterministic candidate artifact manifest certified by this QA gate
    candidate_files = {}
    if app_html:
        candidate_files["index.html"] = app_html
    if app_css:
        candidate_files["style.css"] = app_css
        candidate_files["css/style.css"] = app_css
    if app_js:
        candidate_files["script.js"] = app_js
        candidate_files["js/script.js"] = app_js

    manifest = build_artifact_manifest(
        candidate_files,
        qa_run_id=qa_run_id,
        validation_status="passed" if passed else "failed"
    )
    
    return {
        "passed": passed,
        "qa_run_id": qa_run_id,
        "manifest": manifest,
        "checks": checks,
        "errors": errors,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }

def _strip_html_comments(html):
    """Remove HTML comments to prevent commented-out tags from satisfying detection."""
    if not html:
        return ""
    return re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)


def _has_valid_stylesheet_link(html):
    """
    Check if HTML contains an active, valid <link rel="stylesheet"> targeting the stylesheet.
    Rejects:
    - Commented-out references: <!-- <link ...> -->
    - Other tags: <a href="style.css">, <div data-href="style.css">
    - Similar filenames: not-style.css, old-style.css
    - Non-stylesheet rel values: <link rel="preload" href="style.css">
    """
    stripped = _strip_html_comments(html)
    valid_css_paths = {'style.css', './style.css', 'css/style.css', '/css/style.css', '/style.css'}

    for tag_match in re.finditer(r'<link\b([^>]*?)/?>', stripped, re.IGNORECASE):
        tag_attrs = tag_match.group(1)
        rel_match = re.search(r'\brel\s*=\s*[\'"]?([^\'"\s>]+)', tag_attrs, re.IGNORECASE)
        href_match = re.search(r'\bhref\s*=\s*[\'"]?([^\'"\s>]+)', tag_attrs, re.IGNORECASE)
        if rel_match and href_match:
            rel_val = rel_match.group(1).lower().strip('\'"')
            if 'stylesheet' in rel_val:
                href_raw = href_match.group(1).strip('\'"')
                clean_href = href_raw.split('?')[0].split('#')[0].strip()
                if clean_href in valid_css_paths:
                    return True
    return False


def _has_valid_script_reference(html):
    """
    Check if HTML contains an active, valid <script src="..."> targeting the script.
    Rejects:
    - Commented-out references: <!-- <script src="script.js"></script> -->
    - Other tags: <a href="script.js">, <img src="script.js">
    - Similar filenames: not-script.js, old-script.js
    """
    stripped = _strip_html_comments(html)
    valid_js_paths = {'script.js', './script.js', 'js/script.js', '/js/script.js', '/script.js'}

    for tag_match in re.finditer(r'<script\b([^>]*?)>', stripped, re.IGNORECASE):
        tag_attrs = tag_match.group(1)
        src_match = re.search(r'\bsrc\s*=\s*[\'"]?([^\'"\s>]+)', tag_attrs, re.IGNORECASE)
        if src_match:
            src_raw = src_match.group(1).strip('\'"')
            clean_src = src_raw.split('?')[0].split('#')[0].strip()
            if clean_src in valid_js_paths:
                return True
    return False


def inject_asset_links(html, css_content, js_content):
    """
    Inject CSS stylesheet and JS script links into HTML document.
    - Case-insensitive tag matching for </head> and </body> with optional whitespace.
    - Idempotent and precise: uses _has_valid_stylesheet_link and _has_valid_script_reference
      to ensure active, non-commented, strictly matched asset references.
    - Preserves unrelated document content.
    """
    if not html:
        return html

    # Inject CSS link if CSS exists and not already referenced
    if css_content and not _has_valid_stylesheet_link(html):
        head_end = re.search(r'</\s*head\s*>', html, re.IGNORECASE)
        if head_end:
            insert_pos = head_end.start()
            html = html[:insert_pos] + '  <link rel="stylesheet" href="style.css">\n' + html[insert_pos:]
        else:
            html = '<link rel="stylesheet" href="style.css">\n' + html

    # Inject JS script if JS exists and not already referenced
    if js_content and not _has_valid_script_reference(html):
        body_end = re.search(r'</\s*body\s*>', html, re.IGNORECASE)
        if body_end:
            insert_pos = body_end.start()
            html = html[:insert_pos] + '  <script src="script.js"></script>\n' + html[insert_pos:]
        else:
            html = html + '\n<script src="script.js"></script>'

    return html


def publish_workflow_deliverables(
    app_dir: str,
    task_title: str,
    validated_html: str,
    validated_css: str,
    validated_js: str,
    qa_report: dict,
    workflow_outputs: dict,
    failpoint: Optional[str] = None,
    defer_backup_cleanup: bool = False,
    session_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Real application publication and promotion transaction boundary.
    Performs atomic deliverable persistence with staging, pre-existing backup retention,
    strict byte-for-byte post-write verification, deterministic failpoints, candidate anti-tampering
    SHA-256 manifest verification, manifest.json deliverable generation, and atomic rollback.
    """
    if failpoint == "before_publication":
        raise IOError("Simulated failure before publication starts")

    # 0. Candidate Manifest Verification (Anti-tampering / Identity Gate)
    candidate_manifest = qa_report.get("manifest") if isinstance(qa_report, dict) else None
    qa_run_id = qa_report.get("qa_run_id") if isinstance(qa_report, dict) else None
    delivery_id = f"deliv_{uuid.uuid4().hex[:12]}"

    if candidate_manifest and isinstance(candidate_manifest, dict) and "artifacts" in candidate_manifest:
        manifest_map = {item["relative_path"]: item for item in candidate_manifest["artifacts"]}
        
        # Verify index.html against certified QA manifest digest
        if "index.html" in manifest_map:
            actual_sha = hashlib.sha256(validated_html.encode("utf-8")).hexdigest()
            expected_sha = manifest_map["index.html"]["sha256"]
            if actual_sha != expected_sha:
                raise IOError(
                    f"Candidate artifact tampering or corruption detected: index.html SHA-256 digest ({actual_sha[:12]}...) "
                    f"does not match QA manifest ({expected_sha[:12]}...)"
                )
        # Verify style.css against certified QA manifest digest
        if validated_css and "style.css" in manifest_map:
            actual_sha = hashlib.sha256(validated_css.encode("utf-8")).hexdigest()
            expected_sha = manifest_map["style.css"]["sha256"]
            if actual_sha != expected_sha:
                raise IOError(
                    f"Candidate artifact tampering or corruption detected: style.css SHA-256 digest ({actual_sha[:12]}...) "
                    f"does not match QA manifest ({expected_sha[:12]}...)"
                )
        # Verify script.js against certified QA manifest digest
        if validated_js and "script.js" in manifest_map:
            actual_sha = hashlib.sha256(validated_js.encode("utf-8")).hexdigest()
            expected_sha = manifest_map["script.js"]["sha256"]
            if actual_sha != expected_sha:
                raise IOError(
                    f"Candidate artifact tampering or corruption detected: script.js SHA-256 digest ({actual_sha[:12]}...) "
                    f"does not match QA manifest ({expected_sha[:12]}...)"
                )

    os.makedirs(app_dir, exist_ok=True)
    os.makedirs(os.path.join(app_dir, "css"), exist_ok=True)
    os.makedirs(os.path.join(app_dir, "js"), exist_ok=True)

    written_files = []
    expected_artifacts = {}  # target_path -> expected_bytes
    pre_existing_backups = {}  # path -> backup_path

    try:
        def _stage_write_file(target_path, content, encoding="utf-8"):
            content_bytes = content.encode(encoding)
            expected_artifacts[target_path] = content_bytes
            if target_path not in written_files:
                written_files.append(target_path)
            if os.path.exists(target_path) and target_path not in pre_existing_backups:
                bak = target_path + f".pre_{uuid.uuid4().hex[:8]}.bak"
                import shutil
                shutil.copy2(target_path, bak)
                pre_existing_backups[target_path] = bak
            atomic_write_file(target_path, content, encoding)

        def _stage_write_json(target_path, data):
            content = json.dumps(data, indent=2, ensure_ascii=False)
            content_bytes = content.encode("utf-8")
            expected_artifacts[target_path] = content_bytes
            if target_path not in written_files:
                written_files.append(target_path)
            if os.path.exists(target_path) and target_path not in pre_existing_backups:
                bak = target_path + f".pre_{uuid.uuid4().hex[:8]}.bak"
                import shutil
                shutil.copy2(target_path, bak)
                pre_existing_backups[target_path] = bak
            atomic_write_json(target_path, data)

        # 1. index.html
        html_path = os.path.join(app_dir, "index.html")
        _stage_write_file(html_path, validated_html)

        # 2. style.css and css/style.css
        if validated_css:
            for c_path in ["style.css", "css/style.css"]:
                _stage_write_file(os.path.join(app_dir, c_path), validated_css)

        # 3. script.js and js/script.js
        if validated_js:
            for j_path in ["script.js", "js/script.js"]:
                _stage_write_file(os.path.join(app_dir, j_path), validated_js)

        # 4. plan.md
        plan_content = (
            f"# المنتج النهائي لمشروع: {task_title}\n\n"
            f"## 1. دراسة الجدوى (الاستراتيجية)\n{workflow_outputs.get('strategy', '')}\n\n"
            f"## 2. وثيقة المتطلبات (PRD)\n{workflow_outputs.get('product', '')}\n\n"
            f"## 3. الهيكلة الهندسية (Architecture)\n{workflow_outputs.get('engineering', '')}\n\n"
            f"## 4. تصميم الواجهات (UI/UX)\n{workflow_outputs.get('design', '')}\n\n"
            f"## 5. تقرير فحص الجودة (QA Report)\n{json.dumps(qa_report, indent=2, ensure_ascii=False)}\n\n"
        )
        _stage_write_file(os.path.join(app_dir, "plan.md"), plan_content)

        # 5. qa_report.json
        _stage_write_json(os.path.join(app_dir, "qa_report.json"), qa_report)

        # 6. manifest.json (Comprehensive delivery manifest)
        manifest_files = {
            "index.html": validated_html,
            "plan.md": plan_content,
            "qa_report.json": json.dumps(qa_report, indent=2, ensure_ascii=False)
        }
        if validated_css:
            manifest_files["style.css"] = validated_css
            manifest_files["css/style.css"] = validated_css
        if validated_js:
            manifest_files["script.js"] = validated_js
            manifest_files["js/script.js"] = validated_js

        final_manifest = build_artifact_manifest(
            manifest_files,
            qa_run_id=qa_run_id,
            delivery_id=delivery_id,
            validation_status="published"
        )
        _stage_write_json(os.path.join(app_dir, "manifest.json"), final_manifest)

        if failpoint == "after_staging_before_promotion":
            raise IOError("Simulated failure after staging but before promotion")

        # Post-write verification: every published deliverable exists, non-empty, byte match
        for fpath, expected_bytes in expected_artifacts.items():
            if not os.path.exists(fpath):
                raise IOError(f"Deliverable file verification failed: {os.path.basename(fpath)} is missing")
            if os.path.getsize(fpath) == 0:
                raise IOError(f"Deliverable file verification failed: {os.path.basename(fpath)} is empty")
            with open(fpath, 'rb') as f:
                persisted_bytes = f.read()
            if persisted_bytes != expected_bytes:
                raise IOError(f"Persisted artifact {os.path.basename(fpath)} does not match the QA-validated candidate (byte mismatch)")

        if failpoint == "during_promotion":
            raise IOError("Simulated failure during promotion")

    except Exception as persist_err:
        rollback_failures = []
        failed_restorations = {}

        all_rollback_targets = list(written_files)
        for p in pre_existing_backups:
            if p not in all_rollback_targets:
                all_rollback_targets.append(p)

        # Rollback: restore pre-existing files, delete newly created files
        for fpath in all_rollback_targets:
            try:
                if fpath in pre_existing_backups:
                    bak_path = pre_existing_backups[fpath]
                    os.replace(bak_path, fpath)
                elif os.path.exists(fpath):
                    os.remove(fpath)
            except Exception as rb_err:
                if fpath in pre_existing_backups:
                    bak_path = pre_existing_backups[fpath]
                    failed_restorations[fpath] = (bak_path, str(rb_err))
                    rollback_failures.append(
                        f"Failed to rollback {os.path.basename(fpath)} (restoration failed, retained recovery backup at {bak_path}): {rb_err}"
                    )
                    if session_id:
                        try:
                            db_store.record_incomplete_rollback(
                                session_id=session_id,
                                delivery_id=delivery_id,
                                app_dir=app_dir,
                                target_path=fpath,
                                backup_path=bak_path,
                                error_message=str(rb_err)
                            )
                        except Exception as rec_err:
                            logging.error("Failed to record incomplete rollback: %s", rec_err)
                else:
                    rollback_failures.append(f"Failed to rollback {os.path.basename(fpath)}: {rb_err}")

        clean_errs = cleanup_backups(pre_existing_backups, failed_restorations)
        rollback_failures.extend(clean_errs)

        if rollback_failures:
            raise IOError(f"Persistence error: {persist_err}; Rollback errors: {'; '.join(rollback_failures)}") from persist_err
        raise persist_err

    # Clean up backup files on success unless deferred across SQLite persistence boundary
    if not defer_backup_cleanup:
        cleanup_backups(pre_existing_backups)

    if failpoint == "after_promotion_before_workflow_record":
        raise IOError("Simulated failure after promotion but before workflow records success")

    return {
        "app_dir": app_dir,
        "delivery_id": delivery_id,
        "qa_run_id": qa_run_id,
        "manifest": final_manifest,
        "files": written_files,
        "expected_artifacts": expected_artifacts,
        "pre_existing_backups": pre_existing_backups
    }


def continue_workflow(feedback="", session_id=None, call_agent_fn=None):
    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["approval_request"] = None
        workflow_state["departments"]["engineering"] = {"status": "active", "text": "يصمم الآن معمارية النظام..."}
        active_squad = workflow_state.get("active_squad", [])
        squad_plan = workflow_state.get("squad_plan")
        if not session_id:
            session_id = workflow_state.get("session_id")
        else:
            workflow_state["session_id"] = session_id
        cur_task = workflow_state.get('current_task') or 'موقع ويب متكامل'
    update_stats()
    
    if session_id and db_store.get_session(session_id):
        sess_obj = db_store.get_session(session_id)
        proj_name = sess_obj.get("project_name") or str(cur_task)[:40].strip()
        try:
            db_store.update_session_status(session_id, "running", current_stage="engineering")
            if feedback:
                db_store.add_project_memory(proj_name, session_id, "decision", "PRD_Approval_Feedback", feedback, "human_reviewer")
                if cur_task != proj_name:
                    db_store.add_project_memory(cur_task, session_id, "decision", "PRD_Approval_Feedback", feedback, "human_reviewer")
            with state_lock:
                state_snap = copy.deepcopy(workflow_state)
            db_store.save_session_checkpoint(session_id, "engineering_started", state_snap)
        except Exception as pe:
            logging.warning("Session status update warning: %s", pe)
    else:
        # Auto-create session + task graph for direct continue_workflow invocations (e.g. tests or resumed runs)
        project_title = str(cur_task)[:40].strip()
        if not squad_plan:
            squad_plan = triage_project_squad(cur_task)
            with state_lock:
                workflow_state["squad_plan"] = squad_plan
        if not active_squad and squad_plan:
            active_squad = []
            for spec in squad_plan.get("specialists", []):
                active_squad.append({
                    "id": spec["id"],
                    "role_id": spec["id"],
                    "name": spec["name"],
                    "display_name": spec.get("display_name", spec["name"]),
                    "category": spec.get("category", "specialized"),
                    "stage": spec.get("stage", "strategy"),
                    "reason": spec.get("reason", ""),
                    "status": "selected"
                })
            with state_lock:
                workflow_state["active_squad"] = active_squad
        try:
            session_id = db_store.create_session(
                project_name=project_title,
                task_description=cur_task,
                squad_archetype=squad_plan.get("archetype") if squad_plan else "general",
                metadata={"squad_plan": squad_plan, "active_squad": active_squad}
            )
            with state_lock:
                workflow_state["session_id"] = session_id
            db_store.update_session_status(session_id, "running", current_stage="engineering")
            orch.build_project_task_graph(session_id, project_title, cur_task, squad_plan)

            # Pre-populate tasks in DB with existing outputs so DAG dependencies are satisfied
            tasks = db_store.list_tasks_for_session(session_id)
            with state_lock:
                existing_outs = copy.deepcopy(workflow_state.get("outputs", {}))
            
            for t in tasks:
                stg = t.get("stage")
                if stg in ("strategy", "product"):
                    out_val = existing_outs.get(stg) or f"Output for {stg}"
                    db_store.update_task_status(t["task_id"], "completed", output_text=out_val)
                elif stg in ("engineering", "design") and existing_outs.get(stg):
                    db_store.update_task_status(t["task_id"], "completed", output_text=existing_outs[stg])
        except Exception as se:
            logging.warning("Auto session creation warning: %s", se)
    
    if feedback:
        add_feed("النظام", "System", "linear-gradient(135deg, #2c3e50, #3498db)", "SY", f"تم تقديم ملاحظات: {feedback}. جاري مواصلة العمل...")
    else:
        add_feed("النظام", "System", "linear-gradient(135deg, #2c3e50, #3498db)", "SY", "✅ تم الاعتماد. فريق الهندسة يبدأ العمل الآن.")
        
    try:
        def _on_task_start(t):
            stg = t.get("stage", "engineering")
            assigned_id = t.get("assigned_agent_id")
            with state_lock:
                if stg in workflow_state["departments"]:
                    workflow_state["departments"][stg] = {"status": "active", "text": f"يعمل على: {t.get('title')}"}
                for s in workflow_state.get("active_squad", []):
                    if s["id"] == assigned_id:
                        s["status"] = "running"
            update_stats()

        def _on_task_complete(t, out):
            stg = t.get("stage", "engineering")
            assigned_id = t.get("assigned_agent_id")
            with state_lock:
                workflow_state["outputs"][stg] = out
                if stg in ("qa", "testing") and assigned_id != "engineering-code-reviewer":
                    cur_qa = workflow_state["outputs"].get("qa_specialists", "")
                    workflow_state["outputs"]["qa_specialists"] = (cur_qa + "\n\n" + out).strip() if cur_qa else out.strip()
                if stg in workflow_state["departments"]:
                    workflow_state["departments"][stg] = {"status": "done", "text": f"أنجز بنجاح: {t.get('title')}"}
                for s in workflow_state.get("active_squad", []):
                    if s["id"] == assigned_id:
                        s["status"] = "completed"
            update_stats()

        def _on_task_fail(t, err):
            stg = t.get("stage", "engineering")
            assigned_id = t.get("assigned_agent_id")
            with state_lock:
                if "_spec_" not in t.get("task_id", "") and stg in workflow_state["departments"]:
                    workflow_state["departments"][stg] = {"status": "failed", "text": f"تعثر: {err}"}
                for s in workflow_state.get("active_squad", []):
                    if s["id"] == assigned_id:
                        s["status"] = "failed"
            update_stats()

        # Run Post-PRD stages: engineering, design, development, qa, animation, seo via verified DAG
        effective_call_agent = call_agent_fn or call_agent
        post_prd_stages = ["engineering", "design", "development", "qa", "animation", "seo"]
        success, completed_ids, err = orch.run_dag_stages(
            session_id=session_id,
            allowed_stages=post_prd_stages,
            call_agent_fn=effective_call_agent,
            agent_prompt_resolver_fn=get_agent_prompt_meta,
            add_feed_fn=add_feed,
            on_task_start_fn=_on_task_start,
            on_task_complete_fn=_on_task_complete,
            on_task_fail_fn=_on_task_fail
        )

        if not success:
            raise RuntimeError(err or "فشل تنفيذ مهام ما بعد الاعتماد في الـ DAG.")

        # Re-populate code variables from DAG task outputs
        dev_out = ""
        anim_out = ""
        with state_lock:
            dev_out = workflow_state["outputs"].get("development", "")
            anim_out = workflow_state["outputs"].get("animation", "")
            
        if session_id:
            tasks = db_store.list_tasks_for_session(session_id)
            for t in tasks:
                if t.get("stage") == "development" and t.get("output_text"):
                    dev_out = t["output_text"]
                if t.get("stage") == "animation" and t.get("output_text"):
                    anim_out = t["output_text"]

        # --- Code Extraction ---
        html_match = re.search(r"```html(.*?)```", dev_out, re.DOTALL)
        app_html = html_match.group(1).strip() if html_match else ""
        
        dev_css_blocks = re.findall(r"```css(.*?)```", dev_out, re.DOTALL)
        anim_css_blocks = re.findall(r"```css(.*?)```", anim_out, re.DOTALL)
        dev_css = "\n\n".join([c.strip() for c in dev_css_blocks if c.strip()])
        anim_css = "\n\n".join([c.strip() for c in anim_css_blocks if c.strip()])
        app_css = anim_css if (anim_css and dev_css in anim_css) else (f"{dev_css}\n\n/* Motion */\n{anim_css}" if anim_css and dev_css else (anim_css or dev_css))
        
        dev_js_blocks = re.findall(r"```(?:javascript|js)(.*?)```", dev_out, re.DOTALL)
        anim_js_blocks = re.findall(r"```(?:javascript|js)(.*?)```", anim_out, re.DOTALL)
        dev_js = "\n\n".join([j.strip() for j in dev_js_blocks if j.strip()])
        anim_js = "\n\n".join([j.strip() for j in anim_js_blocks if j.strip()])
        app_js = anim_js if (anim_js and dev_js in anim_js) else (f"{dev_js}\n\n/* Interactions */\n{anim_js}" if anim_js and dev_js else (anim_js or dev_js))

        # --- Apply ALL transformations BEFORE validation ---
        # Inject CSS/JS asset links into HTML (case-insensitive, idempotent, no duplicates)
        app_html = inject_asset_links(app_html, app_css, app_js)

        # --- Run Production QA Gate on FINAL transformed artifacts ---
        qa_report = run_qa_checks(app_html, app_css, app_js)
        with state_lock:
            workflow_state["qa_report"] = qa_report

        # --- Bounded Closed-Loop QA Self-Healing (Max 2 Attempts) ---
        remediation_count = 0
        if session_id:
            remediation_count = db_store.get_session_remediation_count(session_id)

        while not qa_report["passed"] and remediation_count < 2:
            remediation_count += 1
            err_summary = ", ".join(qa_report["errors"])
            add_feed("مهندس الجودة", "QA Auditor", "orange", "HEAL", f"⚠️ كشف فحص الجودة عن أخطاء: {err_summary}. جاري بدء دورة المعالجة الذاتية (محاولة {remediation_count} من 2)...")
            
            # Persist failure diagnostics in project memory
            if session_id:
                sess_obj = db_store.get_session(session_id)
                proj_name = sess_obj.get("project_name") or str(cur_task)[:40].strip() if sess_obj else str(cur_task)[:40].strip()
                diag_content = f"Errors: {err_summary}\nHTML_Valid: {qa_report.get('html_valid')}\nCSS_Valid: {qa_report.get('css_valid')}\nJS_Valid: {qa_report.get('js_valid')}"
                db_store.add_project_memory(proj_name, session_id, "qa_finding", f"QA_Defect_Cycle_{remediation_count}", diag_content, "engineering-code-reviewer")
                if cur_task != proj_name:
                    db_store.add_project_memory(cur_task, session_id, "qa_finding", f"QA_Defect_Cycle_{remediation_count}", diag_content, "engineering-code-reviewer")

            # Find developer task to set as dependency
            dev_task_id = "task_eng_dev"
            if session_id:
                tasks = db_store.list_tasks_for_session(session_id)
                for t in tasks:
                    if t.get("stage") == "development" and "remediation" not in t.get("title", "").lower() and "إصلاح" not in t.get("title", ""):
                        dev_task_id = t["task_id"]
                        break

            # Create and execute remediation task in DAG
            if session_id:
                rem_task_id = orch.create_remediation_task(
                    session_id=session_id,
                    original_task_id=dev_task_id,
                    qa_errors=qa_report["errors"],
                    candidate_code={"html": app_html, "css": app_css, "js": app_js},
                    attempt_num=remediation_count
                )
                claimed = db_store.claim_task(rem_task_id)
                if not claimed:
                    add_feed("مهندس الجودة", "QA Auditor", "orange", "CLAIM", f"تنبيه: مهمة المعالجة {rem_task_id} قيد التنفيذ أو تم حجزها بالفعل من قبل عامل آخر.")
                    break
                rem_task = db_store.get_task(rem_task_id)
                session_obj = db_store.get_session(session_id)
                
                # Execute remediation task
                ok, rem_output, rem_err = orch.execute_single_task(
                    task=rem_task,
                    session=session_obj,
                    call_agent_fn=effective_call_agent,
                    agent_prompt_resolver_fn=get_agent_prompt_meta,
                    add_feed_fn=add_feed
                )
                
                if ok and rem_output:
                    # Re-extract corrected code
                    h_m = re.search(r"```html(.*?)```", rem_output, re.DOTALL)
                    if h_m:
                        app_html = h_m.group(1).strip()
                    c_m = re.findall(r"```css(.*?)```", rem_output, re.DOTALL)
                    if c_m:
                        app_css = "\n\n".join([c.strip() for c in c_m if c.strip()])
                    j_m = re.findall(r"```(?:javascript|js)(.*?)```", rem_output, re.DOTALL)
                    if j_m:
                        app_js = "\n\n".join([j.strip() for j in j_m if j.strip()])
                    
                    if not h_m and not c_m and not j_m:
                        qa_report["errors"].append(f"Remediation agent returned no parseable code blocks (attempt {remediation_count})")
                    else:
                        app_html = inject_asset_links(app_html, app_css, app_js)
                        qa_report = run_qa_checks(app_html, app_css, app_js)
                        with state_lock:
                            workflow_state["qa_report"] = qa_report
                            workflow_state["outputs"]["development"] = rem_output
                            
                        if qa_report["passed"]:
                            add_feed("مهندس الجودة", "QA Auditor", "linear-gradient(135deg, #11998e, #38ef7d)", "HEALED", f"✅ نجحت دورة المعالجة الذاتية (محاولة {remediation_count}) وتجاوز الكود كافة الفحوصات!")
                            sess_obj = db_store.get_session(session_id)
                            proj_name = sess_obj.get("project_name") or str(cur_task)[:40].strip() if sess_obj else str(cur_task)[:40].strip()
                            success_content = "All structural and syntax defects were successfully resolved by self-healing."
                            db_store.add_project_memory(proj_name, session_id, "qa_finding", f"QA_Remediation_Success_{remediation_count}", success_content, "engineering-senior-developer")
                            if cur_task != proj_name:
                                db_store.add_project_memory(cur_task, session_id, "qa_finding", f"QA_Remediation_Success_{remediation_count}", success_content, "engineering-senior-developer")
                            break
                else:
                    qa_report["errors"].append(f"Remediation execution failed (attempt {remediation_count}): {rem_err or 'unknown error'}")
                    with state_lock:
                        workflow_state["qa_report"] = qa_report

        # If still failing after bounded retries, fail closed!
        if not qa_report["passed"]:
            err_msg = f"فشل فحص الجودة البرمجية النهائي واستنفاد محاولات الإصلاح الذاتي: {', '.join(qa_report['errors'])}"
            with state_lock:
                workflow_state["status"] = "failed"
                workflow_state["blockers"].append(err_msg)
                workflow_state["departments"]["qa"]["status"] = "failed"
                workflow_state["departments"]["qa"]["text"] = "فشل فحص المعايير البرمجية"
            if session_id:
                db_store.update_session_status(session_id, "failed", error_message=err_msg)
            update_stats()
            add_feed("مهندس الجودة", "QA", "red", "QA", err_msg)
            return

        # Delivery gate task execution in DAG
        if session_id:
            sess_curr = db_store.get_session(session_id)
            if sess_curr and sess_curr.get("status") == "cancelled":
                add_feed("مدير المشروع", "Project Manager", "orange", "PM", "تم إلغاء الجلسة، تم إيقاف عملية النشر.")
                return
            tasks = db_store.list_tasks_for_session(session_id)
            deliv_task = next((t for t in tasks if t.get("stage") == "delivery"), None)
            if deliv_task:
                claimed = db_store.claim_task(deliv_task["task_id"])
                if not claimed:
                    add_feed("مدير المشروع", "Project Manager", "orange", "PM", "مهمة التسليم قيد التنفيذ أو اكتملت بالفعل.")
                    return

        # Capture the exact validated candidate artifacts for identity verification
        validated_html = app_html
        validated_css = app_css
        validated_js = app_js
                
        # Safe atomic deliverable persistence with staging-and-verify guarantees
        with state_lock:
            task_title = workflow_state.get('current_task', 'تطبيق_جديد')
        app_dir, clean_folder_name = get_project_folder_name(task_title)
        
        failpoint = os.environ.get("TEST_PUBLICATION_FAILPOINT")
        pub_result = publish_workflow_deliverables(
            app_dir=app_dir,
            task_title=task_title,
            validated_html=validated_html,
            validated_css=validated_css,
            validated_js=validated_js,
            qa_report=qa_report,
            workflow_outputs=workflow_state.get('outputs', {}),
            failpoint=failpoint,
            defer_backup_cleanup=True,
            session_id=session_id
        )
        expected_artifacts = pub_result["expected_artifacts"]
        written_files = pub_result["files"]
        delivery_id = pub_result.get("delivery_id")
        qa_run_id = pub_result.get("qa_run_id")
        pre_existing_backups = pub_result.get("pre_existing_backups", {})

        if session_id:
            try:
                # Durably record artifact manifest in database
                if pub_result.get("manifest") and "artifacts" in pub_result["manifest"]:
                    db_store.record_artifact_manifest(
                        session_id=session_id,
                        qa_run_id=qa_run_id,
                        manifest=pub_result["manifest"]["artifacts"],
                        delivery_id=delivery_id
                    )

                tasks = db_store.list_tasks_for_session(session_id)
                deliv_task = next((t for t in tasks if t.get("stage") == "delivery"), None)
                if deliv_task:
                    db_store.complete_task_with_agent_run(
                        task_id=deliv_task["task_id"],
                        session_id=session_id,
                        agent_id="project-manager",
                        agent_name="مدير المشروع",
                        division_id="project-management",
                        prompt_file=r"product\project-manager.md",
                        output_text=f"Atomic publication verified at {app_dir} (Delivery ID: {delivery_id})",
                        tokens_used=100,
                        token_source="estimated",
                        output_preview=f"Published to {clean_folder_name}"
                    )
                    db_store.record_task_handoff(
                        session_id=session_id,
                        from_task_id=deliv_task["task_id"],
                        to_task_id=deliv_task["task_id"],
                        from_agent="project-manager",
                        to_agent="production-environment",
                        artifact_type="published_application",
                        artifact_summary=f"Published to {clean_folder_name} (Delivery ID: {delivery_id})",
                        artifacts={"files": list(expected_artifacts.keys()), "delivery_id": delivery_id, "qa_run_id": qa_run_id},
                        status="acknowledged"
                    )
                with state_lock:
                    state_snap = copy.deepcopy(workflow_state)
                db_store.save_session_checkpoint(session_id, "delivery_completed", state_snap)
                db_store.update_session_status(session_id, "completed", checkpoint_step="delivery_completed")
                cleanup_backups(pre_existing_backups)
            except Exception as pe:
                logging.error("Delivery completion persistence failed, restoring pre-existing backups and rolling back files: %s", pe)
                rollback_failures = []
                failed_restorations = {}
                all_rollback_targets = list(written_files)
                for p in pre_existing_backups:
                    if p not in all_rollback_targets:
                        all_rollback_targets.append(p)

                for fpath in all_rollback_targets:
                    try:
                        if fpath in pre_existing_backups:
                            bak_path = pre_existing_backups[fpath]
                            if os.path.exists(bak_path):
                                os.replace(bak_path, fpath)
                        elif os.path.exists(fpath):
                            os.remove(fpath)
                    except Exception as rb_e:
                        if fpath in pre_existing_backups:
                            bak_path = pre_existing_backups[fpath]
                            failed_restorations[fpath] = (bak_path, str(rb_e))
                            rollback_failures.append(
                                f"Failed to rollback {os.path.basename(fpath)} (restoration failed, retained recovery backup at {bak_path}): {rb_e}"
                            )
                            if session_id:
                                try:
                                    db_store.record_incomplete_rollback(
                                        session_id=session_id,
                                        delivery_id=delivery_id,
                                        app_dir=app_dir,
                                        target_path=fpath,
                                        backup_path=bak_path,
                                        error_message=str(rb_e)
                                    )
                                except Exception as rec_e:
                                    logging.error("Failed to record incomplete rollback in continue_workflow: %s", rec_e)
                        else:
                            rollback_failures.append(f"Failed to rollback {os.path.basename(fpath)}: {rb_e}")

                clean_errs = cleanup_backups(pre_existing_backups, failed_restorations)
                rollback_failures.extend(clean_errs)
                if rollback_failures:
                    raise IOError(f"Database delivery persistence failed: {pe}; Rollback errors: {'; '.join(rollback_failures)}") from pe
                raise IOError(f"Database delivery persistence failed: {pe}") from pe
        else:
            cleanup_backups(pre_existing_backups)

        with state_lock:
            workflow_state["status"] = "completed"
        add_feed("مدير المشروع", "Project Manager", "linear-gradient(135deg, #f6d365, #fda085)", "PM", f"تم اكتمال المشروع وتجاوز فحص الجودة بنجاح! 🚀 المجلد: `{clean_folder_name}`")
        update_stats()
        
    except Exception as e:
        with state_lock:
            workflow_state["status"] = "failed"
            workflow_state["blockers"].append(str(e))
        if session_id:
            try:
                db_store.update_session_status(session_id, "failed", error_message=str(e))
            except Exception as db_err:
                logging.warning("Failed to update session status to failed for %s: %s", session_id, db_err)
        update_stats()
        add_feed("النظام", "System", "red", "ERR", f"توقف سير العمل: {str(e)}")

# --- Explicit, Secure HTTP Routes ---

@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/style.css')
def serve_css():
    return send_from_directory('.', 'style.css')

@app.route('/script.js')
def serve_js():
    return send_from_directory('.', 'script.js')

@app.route('/favicon.ico')
def favicon():
    return Response(status=204)

# Explicitly block access to sensitive files
@app.route('/<path:filename>')
def block_sensitive_files(filename):
    sensitive_exts = ('.py', '.json', '.env', '.git', '.log', '.md')
    if any(filename.lower().endswith(ext) for ext in sensitive_exts) or '..' in filename:
        return jsonify({"error": "Access denied"}), 403
    return jsonify({"error": "Not Found"}), 404

@app.route('/api/state', methods=['GET'])
def get_state():
    with state_lock:
        state_copy = copy.deepcopy(workflow_state)
    state_copy["budget"] = load_budget()
    
    session_id = state_copy.get("session_id")
    if session_id:
        try:
            tasks = db_store.get_session_tasks(session_id)
            handoffs = db_store.get_session_handoffs(session_id)
            checkpoints = db_store.get_session_checkpoints(session_id)
            state_copy["persistence"] = {
                "session_id": session_id,
                "tasks": tasks,
                "handoffs": handoffs,
                "checkpoints": checkpoints,
                "task_count": len(tasks),
                "completed_tasks": sum(1 for t in tasks if t.get("status") == "completed")
            }
            project_title = state_copy.get("current_task", "")
            if project_title:
                state_copy["memory"] = db_store.get_project_memory(project_title)
        except Exception as e:
            state_copy["persistence_error"] = str(e)
    return jsonify(state_copy)

SESSION_ID_REGEX = re.compile(r'^[a-zA-Z0-9_\-]{1,64}$')

def is_valid_session_id(session_id: str) -> bool:
    """Validate that session_id contains only safe alphanumeric characters, hyphens, or underscores (1-64 chars)."""
    return bool(session_id and isinstance(session_id, str) and SESSION_ID_REGEX.match(session_id))

RUN_ID_REGEX = re.compile(r'^[a-zA-Z0-9_\-]{1,64}$')

def is_valid_run_id(run_id: Any) -> bool:
    """
    Phase 2.6-B2: Validate that run_id contains only safe alphanumeric characters,
    hyphens, or underscores (1-64 chars). Guards against traversal and injection.
    """
    return bool(run_id and isinstance(run_id, str) and RUN_ID_REGEX.match(run_id))

def authenticate_operator(req, require_admin: bool = False) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Phase 2.6-C: Authenticate operator identity and authorize recovery actions.
    Credentials configured via environment variables or settings:
      OPERATOR_AUTH_TOKEN
      ADMIN_AUTH_TOKEN
    Fails closed if credentials are absent or invalid.
    Returns (is_authorized, principal, error_message).
    """
    settings = load_settings()
    env_op = os.environ.get("OPERATOR_AUTH_TOKEN")
    env_admin = os.environ.get("ADMIN_AUTH_TOKEN")
    sett_op = settings.get("operator_auth_token")
    sett_admin = settings.get("admin_auth_token")

    op_token = env_op or sett_op
    admin_token = env_admin or sett_admin

    auth_header = req.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    if not token:
        token = req.headers.get("X-Operator-Token") or req.headers.get("X-Admin-Token")

    if not token:
        return False, None, "Missing operator authentication credentials"

    if not op_token and not admin_token:
        return False, None, "Operator authentication is not configured on this server"

    is_admin = bool(admin_token and hmac.compare_digest(token, admin_token))
    is_operator = bool(op_token and hmac.compare_digest(token, op_token))

    if not is_admin and not is_operator:
        return False, None, "Invalid operator authentication credentials"

    if require_admin and not is_admin:
        return False, "operator", "Elevated administrator privileges required for this operation"

    principal = "admin" if is_admin else "operator"
    return True, principal, None

@app.route('/api/sessions', methods=['GET'])
def list_sessions():
    """List recent persistent workflow sessions."""
    project_id = request.args.get('project_id')
    sessions = db_store.get_recent_sessions(project_id=project_id, limit=50)
    return jsonify({"sessions": sessions, "total": len(sessions)})

@app.route('/api/sessions/<session_id>', methods=['GET'])
def get_session_detail(session_id):
    """Retrieve full detail for a single workflow session."""
    if not is_valid_session_id(session_id):
        return jsonify({"error": "Invalid session identifier format"}), 400
    session = db_store.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    tasks = db_store.get_session_tasks(session_id)
    handoffs = db_store.get_session_handoffs(session_id)
    checkpoints = db_store.get_session_checkpoints(session_id)
    return jsonify({
        "session": session,
        "tasks": tasks,
        "handoffs": handoffs,
        "checkpoints": checkpoints
    })

@app.route('/api/sessions/<session_id>/tasks', methods=['GET'])
def get_session_tasks(session_id):
    """Retrieve all structured tasks for a workflow session."""
    if not is_valid_session_id(session_id):
        return jsonify({"error": "Invalid session identifier format"}), 400
    session = db_store.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    tasks = db_store.get_session_tasks(session_id)
    return jsonify({"session_id": session_id, "tasks": tasks, "total": len(tasks)})

@app.route('/api/sessions/<session_id>/handoffs', methods=['GET'])
def get_session_handoffs(session_id):
    """Retrieve all agent-to-agent collaboration handoffs."""
    if not is_valid_session_id(session_id):
        return jsonify({"error": "Invalid session identifier format"}), 400
    session = db_store.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    handoffs = db_store.get_session_handoffs(session_id)
    return jsonify({"session_id": session_id, "handoffs": handoffs, "total": len(handoffs)})

@app.route('/api/sessions/<session_id>/manifest', methods=['GET'])
def get_session_manifest(session_id):
    """Retrieve verified deliverable artifact manifest entries for a session."""
    if not is_valid_session_id(session_id):
        return jsonify({"error": "Invalid session identifier format"}), 400
    session = db_store.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    manifest = db_store.get_artifact_manifest(session_id)
    return jsonify({"session_id": session_id, "manifest": manifest, "total": len(manifest)})

@app.route('/api/sessions/<session_id>/audit', methods=['GET'])
def get_session_operator_audit(session_id):
    """Retrieve full operator-facing diagnostic audit report for a workflow session."""
    if not is_valid_session_id(session_id):
        return jsonify({"error": "Invalid session identifier format"}), 400
    audit_report = db_store.get_operator_session_audit(session_id)
    if not audit_report or not audit_report.get("session"):
        return jsonify({"error": "Session not found", "session_id": session_id}), 404
    return jsonify(audit_report)

@app.route('/api/sessions/<session_id>/reconcile_rollback/preview', methods=['GET'])
def preview_rollback_reconciliation_endpoint(session_id):
    """
    Produce non-destructive preview of incomplete rollback reconciliation for operator audit.
    """
    if not is_valid_session_id(session_id):
        return jsonify({"error": "Invalid session identifier format"}), 400
    session = db_store.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found", "session_id": session_id}), 404

    preview = db_store.preview_rollback_reconciliation(session_id)
    return jsonify(preview)

@app.route('/api/sessions/<session_id>/reconcile_rollback/apply', methods=['POST'])
def apply_rollback_reconciliation_endpoint(session_id):
    """
    Phase 2.6-C: Authorized execution of incomplete rollback reconciliation.
    Strictly validates origin, authenticates operator, prevents concurrent execution,
    requires elevated admin privileges for force=True, and refuses automatic overwrite
    if newer user edits or conflicts are detected.
    Records all authorization attempts and outcomes to durable recovery audit log.
    """
    if not is_safe_origin(request):
        return jsonify({"error": "Forbidden: Invalid origin"}), 403
    if not is_valid_session_id(session_id):
        return jsonify({"error": "Invalid session identifier format"}), 400

    data = request.get_json(silent=True) or {}
    recovery_ids = data.get("recovery_ids")
    operator_action = data.get("operator_action", "restore_safe")
    force = bool(data.get("force", False))

    # Authenticate and Authorize operator before any state mutation
    is_auth, principal, auth_err = authenticate_operator(request, require_admin=force)
    if not is_auth:
        status_code = 403 if principal == "operator" else 401
        db_store.record_recovery_audit_event(
            principal=principal or "anonymous",
            action="reconcile_rollback_apply",
            target_id=session_id,
            force_requested=force,
            authorized=False,
            outcome="denied",
            error_message=auth_err
        )
        return jsonify({"error": auth_err}), status_code

    session = db_store.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found", "session_id": session_id}), 404

    res = db_store.apply_rollback_reconciliation(
        session_id=session_id,
        recovery_ids=recovery_ids,
        operator_action=operator_action,
        force=force
    )

    outcome = "success" if res.get("success") else ("conflicted" if res.get("conflicts_count", 0) > 0 else "failed")
    db_store.record_recovery_audit_event(
        principal=principal or "unknown",
        action="reconcile_rollback_apply",
        target_id=session_id,
        force_requested=force,
        authorized=True,
        outcome=outcome,
        error_message=res.get("error")
    )

    if not res.get("success"):
        if res.get("conflicts_count", 0) > 0:
            return jsonify({
                "error": "Conflict detected: newer user edits exist on disk. Automatic overwrite refused.",
                "details": res
            }), 409
        return jsonify({"error": res.get("error", "Reconciliation failed"), "details": res}), 400

    return jsonify(res)

@app.route('/api/runs/<run_id>/reconcile_usage', methods=['POST'])
def reconcile_run_usage_endpoint(run_id):
    """
    Phase 2.6-A2 & C: Reconciles an agent run using authoritative provider usage/billing data.
    Enforces safe origin, run ID regex validation, operator authentication,
    and durable transactional outbox projection to budget.json.
    """
    if not is_safe_origin(request):
        return jsonify({"error": "Forbidden: Invalid origin"}), 403
    if not is_valid_run_id(run_id):
        return jsonify({"error": "Invalid run identifier format"}), 400

    # Authenticate operator before write
    is_auth, principal, auth_err = authenticate_operator(request, require_admin=False)
    if not is_auth:
        db_store.record_recovery_audit_event(
            principal=principal or "anonymous",
            action="reconcile_usage",
            target_id=run_id,
            force_requested=False,
            authorized=False,
            outcome="denied",
            error_message=auth_err
        )
        return jsonify({"error": auth_err}), 401

    data = request.get_json(silent=True) or {}
    provider_tokens = data.get("provider_tokens")
    cost = data.get("cost")
    provider_req_id = data.get("provider_request_id")

    lookup_fn = None
    if provider_tokens is not None:
        def _authoritative_lookup(rid, req_id):
            return {
                "found": True,
                "tokens": provider_tokens,
                "cost": cost,
                "provider_request_id": provider_req_id or req_id
            }
        lookup_fn = _authoritative_lookup

    res = db_store.reconcile_provider_run_usage(
        run_id,
        provider_lookup_fn=lookup_fn,
        budget_path=BUDGET_FILE
    )

    outcome = "success" if res.get("success") else ("unresolved" if res.get("reconciliation_status") == "unresolved" else "failed")
    db_store.record_recovery_audit_event(
        principal=principal or "unknown",
        action="reconcile_usage",
        target_id=run_id,
        force_requested=False,
        authorized=True,
        outcome=outcome,
        error_message=res.get("error")
    )

    if not res.get("success"):
        return jsonify(res), 400
    return jsonify(res)

@app.route('/api/memory/<project_id>', methods=['GET'])
def get_project_memory_endpoint(project_id):
    """Retrieve project-scoped memory facts, architecture decisions, and requirements."""
    category = request.args.get('category')
    verified_only = request.args.get('verified_only', 'false').lower() == 'true'
    memories = db_store.get_project_memory(project_id, category=category, verified_only=verified_only)
    return jsonify({"project_id": project_id, "memories": memories, "total": len(memories)})

@app.route('/api/sessions/<session_id>/cancel', methods=['POST'])
def cancel_session(session_id):
    """Manually cancel a running or paused workflow session."""
    if not is_safe_origin(request):
        return jsonify({"error": "Forbidden: Invalid origin"}), 403
    if not is_valid_session_id(session_id):
        return jsonify({"error": "Invalid session identifier format"}), 400
    session = db_store.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    if session["status"] in ("completed", "cancelled"):
        return jsonify({"error": f"Cannot cancel session in '{session['status']}' state"}), 400
    
    db_store.update_session_status(session_id, "cancelled")
    with db_store.get_db_connection() as conn:
        conn.execute("UPDATE workflow_tasks SET status = 'cancelled', error_message = 'Session cancelled by user.' WHERE session_id = ? AND status IN ('pending', 'ready', 'running');", (session_id,))
        conn.commit()
        
    with state_lock:
        if workflow_state.get("session_id") == session_id:
            workflow_state["status"] = "cancelled"
            workflow_state["approval_request"] = None
    update_stats()
    return jsonify({"message": f"Session {session_id} cancelled successfully"})

@app.route('/api/sessions/<session_id>/resume', methods=['POST'])
def resume_session(session_id):
    """Resume an interrupted or waiting workflow session from its last verified checkpoint."""
    if not is_safe_origin(request):
        return jsonify({"error": "Forbidden: Invalid origin"}), 403
    if not is_valid_session_id(session_id):
        return jsonify({"error": "Invalid session identifier format"}), 400
    session = db_store.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    if session["status"] == "running":
        return jsonify({"error": "Session is already running"}), 409
    if session["status"] in ("completed", "cancelled"):
        return jsonify({"error": f"Cannot resume session in terminal '{session['status']}' state"}), 400
    if session["status"] not in ("interrupted", "failed", "approval_needed"):
        return jsonify({"error": f"Cannot resume session in '{session['status']}' state"}), 400
    
    last_cp = db_store.get_latest_checkpoint(session_id)
    if not last_cp:
        return jsonify({"error": "No verified checkpoint found to resume from"}), 400

    state_data = last_cp.get("state_data") or {}
    cp_step = last_cp.get("step_name")

    with state_lock:
        if workflow_state.get("status") == "running" and workflow_state.get("session_id") != session_id:
            return jsonify({"error": "Another workflow is currently running"}), 409

    # Atomically lock session in SQLite if it was interrupted, failed, or approval_needed to coordinate workers
    if session["status"] in ("interrupted", "failed", "approval_needed"):
        locked = db_store.acquire_session_resumption_lock(session_id, allow_approval_needed=True)
        if not locked:
            return jsonify({"error": "Session is already being resumed or running"}), 409

    with state_lock:
        workflow_state.update(state_data)
        workflow_state["session_id"] = session_id

    if cp_step == "prd_approval_gate":
        with state_lock:
            workflow_state["status"] = "approval_needed"
        db_store.update_session_status(session_id, "approval_needed", checkpoint_step="prd_approval_gate")
        update_stats()
        return jsonify({
            "message": "Session resumed successfully at PRD approval gate",
            "session_id": session_id,
            "resumed_from": cp_step
        })
    elif cp_step in ("workflow_started", "strategy", "product"):
        task_desc = state_data.get("current_task") or session.get("task_description", "")
        threading.Thread(target=run_workflow, args=(task_desc,), kwargs={"existing_session_id": session_id}, daemon=True).start()
        return jsonify({
            "message": f"Session resumed successfully from {cp_step}",
            "session_id": session_id,
            "resumed_from": cp_step
        })
    elif cp_step in ("engineering_completed", "engineering_started", "post_prd_running", "development", "qa", "animation", "seo"):
        data = request.get_json(silent=True) or {}
        feedback = str(data.get('feedback', '')).strip()
        threading.Thread(target=continue_workflow, args=(feedback,), kwargs={"session_id": session_id}, daemon=True).start()
        return jsonify({
            "message": f"Session resumed successfully from {cp_step}",
            "session_id": session_id,
            "resumed_from": cp_step
        })
    else:
        return jsonify({"error": f"Unsupported resumption checkpoint: {cp_step}"}), 400

@app.route('/api/start_task', methods=['POST'])
def start_task():
    if not is_safe_origin(request):
        return jsonify({"error": "Forbidden: Invalid origin"}), 403
    data = request.get_json(silent=True) or {}
    task_desc = data.get('task', '').strip()
    if not task_desc:
        return jsonify({"error": "Task description is required"}), 400
    if len(task_desc) > 50000:
        return jsonify({"error": "Task description exceeds maximum allowed length"}), 400
        
    with state_lock:
        if workflow_state["status"] in ["running", "approval_needed"]:
            return jsonify({"error": "A workflow is already in progress"}), 409
        workflow_state["status"] = "running"
        workflow_state["execution_id"] = uuid.uuid4().hex[:12]
        
    threading.Thread(target=run_workflow, args=(task_desc,), daemon=True).start()
    return jsonify({"message": "Workflow started", "execution_id": workflow_state["execution_id"]})

@app.route('/api/approve', methods=['POST'])
def approve():
    if not is_safe_origin(request):
        return jsonify({"error": "Forbidden: Invalid origin"}), 403
    data = request.get_json(silent=True) or {}
    req_session_id = data.get("session_id")

    with state_lock:
        if workflow_state["status"] != "approval_needed":
            return jsonify({"error": "No pending approval required"}), 400
        if req_session_id and workflow_state.get("session_id") and req_session_id != workflow_state.get("session_id"):
            return jsonify({"error": "Approval request session mismatch"}), 400
        if req_session_id:
            sess_db = db_store.get_session(req_session_id)
            if sess_db and sess_db.get("status") in ("cancelled", "failed"):
                return jsonify({"error": f"Cannot approve {sess_db['status']} session"}), 409
        workflow_state["status"] = "running"
            
    feedback = str(data.get('feedback', '')).strip()
    if len(feedback) > 10000:
        with state_lock:
            workflow_state["status"] = "approval_needed"
        return jsonify({"error": "Feedback exceeds maximum allowed length"}), 400
        
    threading.Thread(target=continue_workflow, args=(feedback,), daemon=True).start()
    return jsonify({"message": "Approved and continuing workflow"})

@app.route('/api/projects', methods=['GET'])
def list_projects():
    projects = []
    if os.path.exists(GENERATED_APPS_DIR):
        for folder in os.listdir(GENERATED_APPS_DIR):
            folder_path = os.path.join(GENERATED_APPS_DIR, folder)
            if os.path.isdir(folder_path):
                try:
                    files = [f for f in os.listdir(folder_path) if not f.endswith(('.py', '.env'))]
                    has_index = "index.html" in files
                    projects.append({
                        "name": folder,
                        "files": files,
                        "path": os.path.abspath(folder_path),
                        "url": f"/apps/{folder}/index.html" if has_index else None
                    })
                except Exception:
                    pass
    projects.sort(key=lambda x: x["name"], reverse=True)
    return jsonify(projects)

@app.route('/api/open_folder', methods=['POST'])
def open_project_folder():
    """
    Secure folder opening endpoint: Strictly constrained to generated_apps children.
    Rejects directory traversal, relative paths escaping base, or arbitrary host paths.
    """
    if not is_safe_origin(request):
        return jsonify({"error": "Forbidden: Invalid origin"}), 403
    data = request.get_json(silent=True) or {}
    folder_name = data.get('name')
    folder_path = data.get('path')
    
    target_dir = None
    if folder_name:
        target_dir = safe_resolve_project_dir(folder_name)
    elif folder_path:
        # Validate that client-provided folder_path resolves strictly inside GENERATED_APPS_DIR
        abs_p = os.path.abspath(folder_path)
        try:
            if os.path.commonpath([GENERATED_APPS_DIR, abs_p]) == GENERATED_APPS_DIR and abs_p != GENERATED_APPS_DIR:
                target_dir = abs_p
        except ValueError:
            target_dir = None
            
    if not target_dir or not os.path.isdir(target_dir):
        return jsonify({"error": "Invalid or unauthorized folder path"}), 403
        
    try:
        os.startfile(target_dir)
        return jsonify({"status": "success", "message": "Folder opened in Windows Explorer"})
    except Exception:
        try:
            subprocess.Popen(['explorer', os.path.normpath(target_dir)])
            return jsonify({"status": "success", "message": "Folder opened via explorer"})
        except Exception as e:
            return jsonify({"error": f"Failed to launch explorer: {str(e)}"}), 500

@app.route('/apps/<project_name>/')
@app.route('/apps/<project_name>/<path:filename>')
def serve_project_app(project_name, filename='index.html'):
    """
    Secure static serving for generated web applications.
    Strictly forbids directory traversal and sensitive file extensions.
    """
    project_dir = safe_resolve_project_dir(project_name)
    if not project_dir or not os.path.isdir(project_dir):
        return jsonify({"error": "Project not found or invalid"}), 404
        
    # Check filename extensions
    allowed_exts = ('.html', '.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff', '.woff2', '.ttf', '.csv', '.md')
    if not any(filename.lower().endswith(ext) for ext in allowed_exts) and filename != '':
        return jsonify({"error": "Access denied for requested file type"}), 403
        
    return send_from_directory(project_dir, filename)

@app.route('/api/agents', methods=['GET'])
def get_agents():
    with state_lock:
        working_depts = [k.lower() for k, v in workflow_state["departments"].items() if v.get("status") == "active"]
        done_depts = [k.lower() for k, v in workflow_state["departments"].items() if v.get("status") == "done"]
        active_squad_map = {s["id"]: s["status"] for s in workflow_state.get("active_squad", [])}

    core_stage_map = {
        "strategy": ["business-strategist"],
        "product": ["product-manager"],
        "engineering": ["engineering-software-architect"],
        "design": ["design-ui-designer"],
        "development": ["engineering-senior-developer"],
        "qa": ["engineering-code-reviewer", "engineering-qa-engineer"],
        "animation": ["design-whimsy-injector", "design-motion-designer"],
        "seo": ["marketing-seo-specialist", "seo-expert"]
    }
    
    inventory = discover_agents()
    agents_list = []
    
    for agent in inventory["agents"]:
        raw_id = agent["id"]
        dept = agent["department"]
        status = "idle"
        
        # Check if specialist is in active_squad
        if raw_id in active_squad_map:
            s_status = active_squad_map[raw_id]
            if s_status == "running":
                status = "working"
            elif s_status == "completed":
                status = "completed"
            elif s_status == "selected":
                status = "selected"
            elif s_status == "failed":
                status = "failed"
            else:
                status = "idle"
        else:
            # Check core stage execution truthfully based on exact matching role
            for wd in working_depts:
                if raw_id in core_stage_map.get(wd, []):
                    status = "working"
                    break
            if status == "idle":
                for dd in done_depts:
                    if raw_id in core_stage_map.get(dd, []):
                        status = "completed"
                        break
                        
        agents_list.append({
            "id": raw_id,
            "name": agent["name"],
            "department": dept,
            "division_id": agent.get("division_id", "specialized"),
            "division_name": agent.get("division_name", dept),
            "division_name_ar": agent.get("division_name_ar", dept),
            "description": agent.get("description", ""),
            "status": status
        })
        
    order = {"working": 0, "selected": 1, "completed": 2, "failed": 3, "idle": 4}
    agents_list.sort(key=lambda a: (order.get(a["status"], 4), a["name"]))
    return jsonify(agents_list)

@app.route('/api/divisions', methods=['GET'])
def get_divisions():
    inventory = discover_agents()
    return jsonify({
        "status": inventory.get("status", "discovered"),
        "total_divisions": len(inventory.get("divisions", [])),
        "total_agents": inventory.get("total", 0),
        "divisions": inventory.get("divisions", []),
        "unclassified": inventory.get("unclassified", []),
        "error": inventory.get("error")
    })

@app.route('/api/budget', methods=['GET'])
def get_budget():
    return jsonify(load_budget())

@app.route('/api/settings', methods=['GET', 'POST'])
def manage_settings():
    if request.method == 'POST':
        if not is_safe_origin(request):
            return jsonify({"error": "Forbidden: Invalid origin"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid request body, expected JSON object"}), 400
            
        current = load_settings()
        sanitized = {}
        
        # Enforce strict allowlist of permitted fields and type checks
        for field in CONFIG_ALLOWLIST:
            if field in data:
                val = data[field]
                if not isinstance(val, str):
                    return jsonify({"error": f"Field '{field}' must be a string"}), 400
                if len(val) > 2048:
                    return jsonify({"error": f"Field '{field}' exceeds max length"}), 400
                sanitized[field] = val
            elif field in current:
                sanitized[field] = current[field]
                
        # Protect existing key: never overwrite with masked/preview string
        new_key = data.get('openrouter_key', '')
        if isinstance(new_key, str) and new_key and not ('...' in new_key or '***' in new_key):
            sanitized['openrouter_key'] = new_key.strip()
        else:
            sanitized['openrouter_key'] = current.get('openrouter_key', '')
            
        with settings_lock:
            try:
                atomic_write_json(SETTINGS_FILE, sanitized)
            except Exception as e:
                return jsonify({"error": f"Failed to save settings: {str(e)}"}), 500
                
        return jsonify({"status": "success", "message": "Settings updated safely"})
    else:
        # GET returns safe sanitized settings (NEVER exposes plaintext secrets)
        return jsonify(get_safe_settings_view())

# Standard error handlers
@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": "Bad Request"}), 400

@app.errorhandler(403)
def forbidden(e):
    return jsonify({"error": "Forbidden"}), 403

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not Found"}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal Server Error"}), 500

if __name__ == '__main__':
    host = os.environ.get('HOST', '127.0.0.1')  # Secure localhost default
    port = int(os.environ.get('PORT', 8080))
    app.run(host=host, port=port)
