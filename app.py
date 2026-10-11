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
import subprocess
from urllib.parse import urlparse

# Secure Flask initialization: Never expose current working directory as static root
app = Flask(__name__, static_folder=None)

AGENTS_DIR = r"c:\Users\sam20\agency-agents"
SETTINGS_FILE = 'settings.json'
BUDGET_FILE = 'budget.json'
GENERATED_APPS_DIR = os.path.abspath("generated_apps")
os.makedirs(GENERATED_APPS_DIR, exist_ok=True)

# Windows reserved device names that cannot be created or accessed as regular files
WINDOWS_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {f'COM{i}' for i in range(1, 10)} | {f'LPT{i}' for i in range(1, 10)}

def is_safe_origin(req):
    """
    Prevent Cross-Site Request Forgery (CSRF) from malicious third-party websites
    triggering local server actions via developer's browser.
    """
    origin = req.headers.get('Origin')
    if origin:
        try:
            parsed = urlparse(origin)
            allowed_hosts = ('127.0.0.1', 'localhost', req.host.split(':')[0])
            if parsed.hostname not in allowed_hosts:
                return False
        except Exception:
            return False
    return True

# Thread synchronization locks
state_lock = threading.Lock()
budget_lock = threading.Lock()
settings_lock = threading.Lock()

CONFIG_ALLOWLIST = {
    'provider',
    'openrouter_model',
    'opencode_url',
    'opencode_ai_provider',
    'opencode_model',
    'openrouter_key'
}

# Scan available agents count
try:
    total_agents = 0
    if os.path.exists(AGENTS_DIR):
        for root, dirs, files in os.walk(AGENTS_DIR):
            if ".git" in root:
                continue
            total_agents += len([f for f in files if f.endswith('.md') and f not in ['README.md', 'CONTRIBUTING.md', 'SECURITY.md']])
    if total_agents == 0:
        total_agents = 282
except Exception:
    total_agents = 282

# Global workflow state
workflow_state = {
    "execution_id": None,
    "status": "idle",  # idle | running | approval_needed | completed | failed
    "current_task": None,
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
                    return json.load(f)
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
            with open(BUDGET_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ERROR] Error saving budget: {e}")

def record_token_usage(agent_name, dept_name, task_name, tokens_count, source="provider_reported"):
    """
    Safely record token consumption without race conditions or double-counting.
    """
    with budget_lock:
        budget = {}
        if os.path.exists(BUDGET_FILE):
            try:
                with open(BUDGET_FILE, 'r', encoding='utf-8') as f:
                    budget = json.load(f)
            except Exception:
                pass
        if not budget:
            budget = {"tokens_used": 0, "estimated_cost": 0.0, "budget_limit": 50.0, "transactions": []}
            
        tokens_count = max(int(tokens_count), 0)
        budget["tokens_used"] = budget.get("tokens_used", 0) + tokens_count
        
        # Gemini Flash token pricing: ~$0.15 per 1M tokens
        cost = round((tokens_count / 1_000_000.0) * 0.15, 4)
        if cost == 0 and tokens_count > 0:
            cost = 0.0001
        budget["estimated_cost"] = round(budget.get("estimated_cost", 0.0) + cost, 4)
        
        tx = {
            "id": int(time.time() * 1000),
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
        
        try:
            with open(BUDGET_FILE, 'w', encoding='utf-8') as f:
                json.dump(budget, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ERROR] Saving budget failed: {e}")
            
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
        common = os.path.commonpath([GENERATED_APPS_DIR, target_dir])
        if common != GENERATED_APPS_DIR or target_dir == GENERATED_APPS_DIR:
            return None
    except ValueError:
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
        for dept_key, dept_data in workflow_state["departments"].items():
            if dept_data["status"] == "active":
                active_squad_count += dept_squad_sizes.get(dept_key, 3)
                
        workflow_state["stats"]["active_agents"] = active_squad_count
        workflow_state["stats"]["pending_approvals"] = 1 if workflow_state["status"] == "approval_needed" else 0

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
                token_count = res_json.get('usageMetadata', {}).get('totalTokenCount', 0)
                is_reported = bool(token_count)
                if not token_count:
                    token_count = (len(system_prompt) + len(full_prompt) + len(reply)) // 4
                
                with state_lock:
                    task_name = workflow_state.get('current_task', 'تطبيق ويب')
                
                # Record token usage ONCE with source tag
                record_token_usage(
                    role_name,
                    "البرمجة والتطوير",
                    task_name,
                    token_count,
                    source="provider_reported" if is_reported else "estimated"
                )
                return reply, token_count, True
                
            err_msg = res_json.get('error', {}).get('message', str(res_json.get('error', '')))
            last_err = err_msg
            time.sleep(1)
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
            
    raise RuntimeError(f"فشل الاتصال بـ Gemini: {last_err}")

def call_agent(agent_file, role_name, short_name, color, prompt, context_text=""):
    """
    Orchestrate specialized agent invocation with honest failure handling.
    Returns string response or raises RuntimeError on genuine failure.
    """
    agent_path = os.path.join(AGENTS_DIR, agent_file)
    system_prompt = f"أنت {role_name} محترف."
    if os.path.exists(agent_path):
        try:
            with open(agent_path, 'r', encoding='utf-8', errors='ignore') as f:
                system_prompt = f.read()
        except Exception:
            pass
            
    full_prompt = f"{prompt}\n\nالسياق:\n{context_text}" if context_text else prompt
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
                tok_cnt = usage.get('total_tokens')
                if tok_cnt:
                    with state_lock:
                        cur_task = workflow_state.get('current_task', 'مهمة برمجية')
                    record_token_usage(role_name, short_name, cur_task, tok_cnt, source="provider_reported")
                    tokens_recorded = True

        # If not recorded yet, calculate estimated tokens and record ONCE
        if not tokens_recorded:
            tokens_est = (len(prompt) + len(context_text) + len(reply)) // 4
            with state_lock:
                cur_task = workflow_state.get('current_task', 'مهمة برمجية')
            record_token_usage(role_name, short_name, cur_task, tokens_est, source="estimated")
            
        add_feed(role_name, role_name, color, short_name, "أنهيت عملي. المخرجات متوفرة الآن.")
        return reply

    except Exception as e:
        safe_msg = str(e)[:150]
        add_feed(role_name, "Error", "red", "ERR", f"فشل: {safe_msg}")
        raise RuntimeError(f"فشل دور {role_name}: {safe_msg}")

# --- Autonomous Workflow Execution Engine ---

def run_workflow(task_desc):
    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["current_task"] = task_desc
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
    add_feed("مستشار البزنس", "Business Strategist", "linear-gradient(135deg, #FF6B6B, #FF8E53)", "BS", f"بدأت في تحليل جدوى مشروع: {task_desc}")
    
    try:
        # 1. Strategy Stage
        strat_out = call_agent(
            r"specialized\business-strategist.md",
            "مستشار البزنس", "BS",
            "linear-gradient(135deg, #FF6B6B, #FF8E53)",
            f"المشروع هو: {task_desc}. قدم دراسة جدوى استراتيجية شاملة وموجزة."
        )
        with state_lock:
            workflow_state["outputs"]["strategy"] = strat_out
            workflow_state["departments"]["strategy"] = {"status": "done", "text": "أنهى دراسة الجدوى."}
            workflow_state["departments"]["product"] = {"status": "active", "text": "يكتب وثيقة المتطلبات..."}
        update_stats()
        
        # 2. Product Stage
        add_feed("مدير المنتج", "Product Manager", "linear-gradient(135deg, #4facfe, #00f2fe)", "PM", "أقوم بكتابة وثيقة متطلبات المنتج (PRD).")
        prod_out = call_agent(
            r"product\product-manager.md",
            "مدير المنتج", "PM",
            "linear-gradient(135deg, #4facfe, #00f2fe)",
            "قم بكتابة وثيقة متطلبات المنتج (PRD) بناءً على الاستراتيجية التالية.",
            strat_out
        )
        with state_lock:
            workflow_state["outputs"]["product"] = prod_out
            workflow_state["departments"]["product"] = {"status": "done", "text": "تم كتابة وثيقة المتطلبات (PRD)."}
            workflow_state["departments"]["engineering"] = {"status": "waiting", "text": "في انتظار الاعتماد للبدء..."}
            workflow_state["status"] = "approval_needed"
            workflow_state["approval_request"] = {
                "title": "مطلوب اعتماد المتطلبات التقنية",
                "message": "نطلب موافقتك على البدء في هندسة النظام وتصميمه بناءً على وثيقة المتطلبات التي جهزها فريق المنتج.",
                "role": "مدير المنتج"
            }
        update_stats()
        
    except Exception as e:
        with state_lock:
            workflow_state["status"] = "failed"
            workflow_state["blockers"].append(str(e))
            for d in ["strategy", "product"]:
                if workflow_state["departments"][d]["status"] == "active":
                    workflow_state["departments"][d]["status"] = "failed"
                    workflow_state["departments"][d]["text"] = "تعثر التنفيذ بسبب خطأ"
        update_stats()
        add_feed("النظام", "System", "red", "ERR", f"توقف سير العمل: {str(e)}")

def continue_workflow(feedback=""):
    with state_lock:
        workflow_state["status"] = "running"
        workflow_state["approval_request"] = None
        workflow_state["departments"]["engineering"] = {"status": "active", "text": "يصمم الآن معمارية النظام..."}
    update_stats()
    
    if feedback:
        add_feed("النظام", "System", "linear-gradient(135deg, #2c3e50, #3498db)", "SY", f"تم تقديم ملاحظات: {feedback}. جاري مواصلة العمل...")
    else:
        add_feed("النظام", "System", "linear-gradient(135deg, #2c3e50, #3498db)", "SY", "✅ تم الاعتماد. فريق الهندسة يبدأ العمل الآن.")
        
    try:
        # 3. Engineering Stage
        with state_lock:
            prod_out = workflow_state["outputs"].get("product", "")
        eng_prompt = f"ضع خطة هندسية وهيكلية لقواعد البيانات والنظام بناء على هذه المتطلبات.\n{('ملاحظات المدير: ' + feedback) if feedback else ''}"
        eng_out = call_agent(r"engineering\engineering-software-architect.md", "المهندس المعماري", "ENG", "linear-gradient(135deg, #20E2D7, #F9FEA5)", eng_prompt, prod_out)
        
        with state_lock:
            workflow_state["outputs"]["engineering"] = eng_out
            workflow_state["departments"]["engineering"] = {"status": "done", "text": "تم بناء الأساسات وقاعدة البيانات."}
            workflow_state["departments"]["design"] = {"status": "active", "text": "يصمم واجهات المستخدم الفاخرة..."}
        update_stats()
        
        # 4. Design Stage
        des_prompt = f"""أنت رئيس المصممين الإبداعيين (Creative UI/UX Director). صمم هوية بصرية كاملة وتجربة مستخدم فاخرة بناءً على المخطط المعماري:
1. لوحة ألوان دقيقة ومتناسقة.
2. خطوط عربية ولاتينية راقية.
3. هيكلة تحريرية كاملة للأقسام.
المخطط:
{eng_out}"""
        des_out = call_agent(r"design\design-ui-designer.md", "مصمم الواجهات", "UI", "linear-gradient(135deg, #a18cd1, #fbc2eb)", des_prompt, eng_out)
        
        with state_lock:
            workflow_state["outputs"]["design"] = des_out
            workflow_state["departments"]["design"] = {"status": "done", "text": "تم الانتهاء من التصميم الأولي."}
            workflow_state["departments"]["development"] = {"status": "active", "text": "يقوم ببرمجة التطبيق الشامل..."}
        update_stats()
        
        # 5. Development Stage
        with state_lock:
            cur_task = workflow_state.get('current_task', 'موقع ويب متكامل')
            
        dev_prompt = f"""بناءً على المعمارية المرفقة:
{eng_out}
وبناءً على التصميم المرفق:
{des_out}
المشروع المطلوب: {cur_task}
قم ببرمجة هذا التطبيق فوراً كمشروع ويب متكامل وفائق الاحترافية (HTML, CSS, JS منفصلين).
أرجع الكود الخاص بكل ملف بداخل بلوك مخصص كالتالي:
```html
<!-- كود HTML الكامل هنا -->
```
```css
/* كود CSS الكامل هنا */
```
```javascript
// كود JS الكامل هنا
```
تأكد من استدعاء style.css و script.js بداخل HTML ولا تستخدم أي اختصارات."""

        dev_out = call_agent(r"engineering\engineering-senior-developer.md", "كبير المبرمجين", "DEV", "linear-gradient(135deg, #11998e, #38ef7d)", dev_prompt, "")
        
        with state_lock:
            workflow_state["outputs"]["development"] = dev_out
            workflow_state["departments"]["development"] = {"status": "done", "text": "تمت كتابة الأكواد بنجاح."}
            workflow_state["departments"]["qa"] = {"status": "active", "text": "يقوم باختبار النظام وإصلاح الأخطاء..."}
        update_stats()
        
        # 6. QA Stage (Independent Concrete Inspection)
        qa_out = call_agent(r"engineering\engineering-qa-engineer.md", "مهندس الجودة", "QA", "linear-gradient(135deg, #FF9A9E, #FECFEF)", "قم بمراجعة الأكواد المرفقة أدناه، وتأكد من خلوها من الأخطاء وراجع معايير الأمان وقابلية الاستخدام.", dev_out)
        
        with state_lock:
            workflow_state["outputs"]["qa"] = qa_out
            workflow_state["departments"]["qa"] = {"status": "done", "text": "تم إجراء الفحص وضمان الجودة."}
            workflow_state["departments"]["animation"] = {"status": "active", "text": "يضيف حركات وتأثيرات بصرية..."}
        update_stats()
        
        # 7. Animation Stage
        anim_out = call_agent(r"design\design-motion-designer.md", "خبير الأنيميشن", "MOTION", "linear-gradient(135deg, #ff758c, #ff7eb3)", "طور كود CSS و JS لإضافة تأثيرات بصرية وحركية احترافية تجعل الموقع حياً وفخماً. أرجع كود CSS المحدث داخل ```css وكود JS المحدث داخل ```javascript.", dev_out)
        
        with state_lock:
            workflow_state["outputs"]["animation"] = anim_out
            workflow_state["departments"]["animation"] = {"status": "done", "text": "تم إضافة التأثيرات الحركية."}
            workflow_state["departments"]["seo"] = {"status": "active", "text": "يحسن أرشفة النظام..."}
        update_stats()
        
        # 8. SEO Stage
        seo_out = call_agent(r"specialized\seo-expert.md", "خبير SEO", "SEO", "linear-gradient(135deg, #f093fb, #f5576c)", "اقترح تحسينات محركات البحث وعلامات Meta tags والكلمات المفتاحية لتحسين الظهور.", dev_out)
        
        with state_lock:
            workflow_state["outputs"]["seo"] = seo_out
            workflow_state["departments"]["seo"] = {"status": "done", "text": "تم تحسين محركات البحث."}
        update_stats()
        
        # --- Code Extraction & Verification ---
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

        # --- Concrete QA Acceptance Criteria Check ---
        html_has_structure = bool(
            ("<html" in app_html and "</html>" in app_html) or
            ("<!DOCTYPE html" in app_html and ("<body" in app_html or "<div" in app_html))
        )
        css_balanced = bool(not app_css or (app_css.count('{') == app_css.count('}')))
        js_balanced = bool(not app_js or (app_js.count('{') == app_js.count('}') and app_js.count('(') == app_js.count(')')))
        
        qa_checks = {
            "html_extracted": bool(app_html and len(app_html) > 50),
            "html_doctype_or_tags": html_has_structure,
            "no_fatal_error_strings": bool("فشل الاتصال" not in app_html and "Read timed out" not in app_html and "Traceback" not in app_html),
            "css_syntax_balanced": css_balanced,
            "js_syntax_balanced": js_balanced
        }
        
        all_passed = all(qa_checks.values())
        with state_lock:
            workflow_state["qa_report"] = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "checks": qa_checks,
                "passed": all_passed
            }
            
        if not all_passed:
            failed_keys = [k for k, v in qa_checks.items() if not v]
            err_msg = f"فشل فحص الجودة البرمجية: المعايير غير المستوفاة ({', '.join(failed_keys)})"
            with state_lock:
                workflow_state["status"] = "failed"
                workflow_state["blockers"].append(err_msg)
                workflow_state["departments"]["qa"]["status"] = "failed"
                workflow_state["departments"]["qa"]["text"] = "فشل فحص المعايير البرمجية"
            update_stats()
            add_feed("مهندس الجودة", "QA", "red", "QA", err_msg)
            return

        # Link stylesheet and script cleanly
        if app_css and 'href="style.css"' not in app_html and "href='style.css'" not in app_html:
            if "</head>" in app_html:
                app_html = app_html.replace("</head>", '  <link rel="stylesheet" href="style.css">\n</head>')
            else:
                app_html = '<link rel="stylesheet" href="style.css">\n' + app_html
                
        if app_js and 'src="script.js"' not in app_html and "src='script.js'" not in app_html:
            if "</body>" in app_html:
                app_html = app_html.replace("</body>", '  <script src="script.js"></script>\n</body>')
            else:
                app_html = app_html + '\n<script src="script.js"></script>'
                
        # Safe directory persistence
        with state_lock:
            task_title = workflow_state.get('current_task', 'تطبيق_جديد')
        app_dir, clean_folder_name = get_project_folder_name(task_title)
        
        os.makedirs(app_dir, exist_ok=True)
        os.makedirs(os.path.join(app_dir, "css"), exist_ok=True)
        os.makedirs(os.path.join(app_dir, "js"), exist_ok=True)
        
        with open(os.path.join(app_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(app_html)
            
        if app_css:
            for c_path in ["style.css", "css/style.css"]:
                target_p = os.path.join(app_dir, c_path)
                with open(target_p, "w", encoding="utf-8") as f:
                    f.write(app_css)
                    
        if app_js:
            for j_path in ["script.js", "js/script.js"]:
                target_p = os.path.join(app_dir, j_path)
                with open(target_p, "w", encoding="utf-8") as f:
                    f.write(app_js)
                    
        with open(os.path.join(app_dir, "plan.md"), "w", encoding="utf-8") as f:
            f.write(f"# المنتج النهائي لمشروع: {task_title}\n\n")
            f.write(f"## 1. دراسة الجدوى (الاستراتيجية)\n{workflow_state['outputs'].get('strategy', '')}\n\n")
            f.write(f"## 2. وثيقة المتطلبات (PRD)\n{workflow_state['outputs'].get('product', '')}\n\n")
            f.write(f"## 3. الهيكلة الهندسية (Architecture)\n{eng_out}\n\n")
            f.write(f"## 4. تصميم الواجهات (UI/UX)\n{des_out}\n\n")
            f.write(f"## 5. تقرير فحص الجودة (QA Report)\n{json.dumps(qa_checks, indent=2)}\n\n")

        add_feed("مدير المشروع", "Project Manager", "linear-gradient(135deg, #f6d365, #fda085)", "PM", f"تم اكتمال المشروع وتجاوز فحص الجودة بنجاح! 🚀 المجلد: `{clean_folder_name}`")
        
        with state_lock:
            workflow_state["status"] = "completed"
        update_stats()
        
    except Exception as e:
        with state_lock:
            workflow_state["status"] = "failed"
            workflow_state["blockers"].append(str(e))
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
    return jsonify(state_copy)

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
    with state_lock:
        if workflow_state["status"] != "approval_needed":
            return jsonify({"error": "No pending approval required"}), 400
            
    data = request.get_json(silent=True) or {}
    feedback = str(data.get('feedback', '')).strip()
    if len(feedback) > 10000:
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
    agents_list = []
    with state_lock:
        working_depts = [k.lower() for k, v in workflow_state["departments"].items() if v.get("status") == "active"]
        done_depts = [k.lower() for k, v in workflow_state["departments"].items() if v.get("status") == "done"]
    
    dept_map = {
        "strategy": ["strateg", "business", "market", "pricing", "analyst"],
        "product": ["product", "prd", "feature", "trend", "nudge"],
        "engineering": ["architect", "backend", "database", "security", "api", "sre", "devops"],
        "design": ["ui-designer", "ux", "brand", "design", "visual"],
        "development": ["senior-developer", "frontend", "developer", "code", "dev", "fullstack"],
        "qa": ["qa", "tester", "audit", "benchmark", "quality", "test"],
        "animation": ["motion", "whimsy", "animation"],
        "seo": ["seo", "content", "search", "metadata"]
    }
    
    try:
        if os.path.exists(AGENTS_DIR):
            for root, dirs, files in os.walk(AGENTS_DIR):
                if ".git" in root:
                    continue
                dept = os.path.basename(root)
                if dept == "agency-agents":
                    dept = "General"
                for f in files:
                    if f.endswith('.md') and f not in ['README.md', 'CONTRIBUTING.md', 'SECURITY.md']:
                        raw_id = f.replace('.md', '')
                        clean_name = raw_id.replace('-', ' ').title()
                        
                        status = "idle"
                        for wd in working_depts:
                            kw_list = dept_map.get(wd, [wd])
                            if any(kw in raw_id.lower() or kw in dept.lower() for kw in kw_list):
                                status = "working"
                                break
                        if status == "idle":
                            for dd in done_depts:
                                kw_list = dept_map.get(dd, [dd])
                                if any(kw in raw_id.lower() or kw in dept.lower() for kw in kw_list):
                                    status = "completed"
                                    break
                                    
                        agents_list.append({
                            "id": raw_id,
                            "name": clean_name,
                            "department": dept,
                            "status": status
                        })
    except Exception:
        pass
        
    order = {"working": 0, "completed": 1, "idle": 2}
    agents_list.sort(key=lambda a: (order.get(a["status"], 2), a["name"]))
    return jsonify(agents_list)

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
                with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(sanitized, f, indent=2)
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
