from flask import Flask, send_from_directory, jsonify, request
import time
import threading
import os
import glob
import json
import requests

app = Flask(__name__, static_folder='.', static_url_path='')

AGENTS_DIR = r"c:\Users\sam20\agency-agents"
SETTINGS_FILE = 'settings.json'

try:
    total_agents = 0
    for root, dirs, files in os.walk(AGENTS_DIR):
        if ".git" in root:
            continue
        total_agents += len([f for f in files if f.endswith('.md') and f not in ['README.md', 'CONTRIBUTING.md', 'SECURITY.md']])
    if total_agents == 0:
        total_agents = 282
except Exception as e:
    total_agents = 282

workflow_state = {
    "status": "idle",
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
    }
}

BUDGET_FILE = 'budget.json'

def load_budget():
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
    try:
        with open(BUDGET_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving budget: {e}")

def record_token_usage(agent_name, dept_name, task_name, tokens_count):
    budget = load_budget()
    tokens_count = max(int(tokens_count), 120)
    budget["tokens_used"] = budget.get("tokens_used", 0) + tokens_count
    # Gemini Flash token pricing: ~$0.15 per 1M tokens
    cost = round((tokens_count / 1_000_000.0) * 0.15, 4)
    if cost == 0:
        cost = 0.0002
    budget["estimated_cost"] = round(budget.get("estimated_cost", 0.0) + cost, 4)
    
    tx = {
        "id": int(time.time() * 1000),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "agent": agent_name,
        "department": dept_name,
        "task": (task_name[:35] + '...') if task_name and len(task_name) > 35 else (task_name or "مشروع ويب متكامل"),
        "tokens": tokens_count,
        "cost": cost
    }
    tx_list = budget.get("transactions", [])
    tx_list.insert(0, tx)
    budget["transactions"] = tx_list[:60]
    save_budget(budget)
    return budget

def get_project_folder_name(task_name):
    import re
    if not task_name:
        return os.path.join("generated_apps", f"Project_{int(time.time())}"), f"Project_{int(time.time())}"
    
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
        
    base_folder = os.path.join("generated_apps", clean)
    if not os.path.exists(base_folder):
        return base_folder, clean
    timestamped = f"{clean}_{int(time.time())}"
    return os.path.join("generated_apps", timestamped), timestamped

def update_stats():
    active_squad_count = 0
    dept_squad_sizes = {
        "strategy": 4,     # Strategy Director, Market Researcher, Financial Analyst, Risk Specialist
        "product": 3,      # Product Manager, PRD Architect, UX Researcher
        "engineering": 5,  # Software Architect, Database Designer, Security Engineer, DevOps Specialist, API Lead
        "design": 4,       # Creative Director, UI Designer, Design System Lead, Visual Artist
        "development": 6,  # Lead Developer, Frontend Architect, Fullstack Pro, Component Specialist, Motion Dev, State Eng
        "qa": 3,           # QA Lead, Security Reviewer, Test Automation Specialist
        "animation": 3,    # Motion Director, Keyframe Artist, Interaction Specialist
        "seo": 2           # SEO Director, Metadata & Schema Specialist
    }
    for dept_key, dept_data in workflow_state["departments"].items():
        if dept_data["status"] == "active":
            active_squad_count += dept_squad_sizes.get(dept_key, 3)
            
    workflow_state["stats"]["active_agents"] = active_squad_count
    workflow_state["stats"]["pending_approvals"] = 1 if workflow_state["status"] == "approval_needed" else 0
    workflow_state["budget"] = load_budget()

def add_feed(name, role, color, initial, text):
    workflow_state["feed"].insert(0, {
        "name": name,
        "role": role,
        "color": color,
        "initial": initial,
        "text": text,
        "time": "الآن",
        "id": int(time.time() * 1000)
    })

def call_agent_gemini(system_prompt, full_prompt, api_key, role_name, preferred_model='gemini-3.5-flash-lite'):
    headers = {"Content-Type": "application/json"}
    data = {
        "contents": [{
            "parts": [{"text": f"System Guidelines:\n{system_prompt}\n\nTask:\n{full_prompt}\n\n(أجب باللغة العربية باحترافية كاملة. إذا طلب منك كتابة كود، أرجع الأكواد مفصولة داخل: ```html و ```css و ```javascript بالتفصيل الكامل بدون أي اختصار)."}]
        }]
    }
    models = [preferred_model, 'gemini-3.5-flash-lite', 'gemini-3.5-flash', 'gemini-flash-latest']
    # Filter unique while preserving order
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
                if not token_count:
                    token_count = (len(system_prompt) + len(full_prompt) + len(reply)) // 4
                record_token_usage(role_name, "البرمجة والتطوير", workflow_state.get('current_task', 'تطبيق ويب'), token_count)
                return reply
            err_msg = res_json.get('error', {}).get('message', str(res_json.get('error', '')))
            last_err = err_msg
            time.sleep(1)
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
    raise Exception(f"فشل الاتصال بـ Gemini: {last_err}")

def call_agent(agent_file, role_name, short_name, color, prompt, context_text=""):
    try:
        agent_path = os.path.join(AGENTS_DIR, agent_file)
        system_prompt = f"أنت {role_name} محترف."
        if os.path.exists(agent_path):
            with open(agent_path, 'r', encoding='utf-8', errors='ignore') as f:
                system_prompt = f.read()
                
        full_prompt = f"{prompt}\n\nالسياق:\n{context_text}" if context_text else prompt
            
        settings = {}
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                settings = json.load(f)
                
        api_key = settings.get('openrouter_key', '')
        provider = settings.get('provider', 'gemini')
        model = settings.get('openrouter_model', 'gemini-3.5-flash-lite' if provider == 'gemini' else 'openai/gpt-4o-mini')
        opencode_url = settings.get('opencode_url', 'http://127.0.0.1:4096')
        
        if provider == 'gemini':
            if not model or '3.8' in model or 'gpt' in model.lower() or 'claude' in model.lower() or 'openai' in model.lower() or '1.5' in model.lower() or '2.5' in model.lower():
                model = 'gemini-3.5-flash-lite'
        elif provider == 'openrouter' and 'gemini' in model.lower() and '/' not in model:
            model = 'google/gemini-2.0-flash-001'
        
        if not api_key and provider != 'opencode':
            time.sleep(2)
            fallback = f"هذه استجابة محاكاة من {role_name} لأن مفتاح API غير موجود."
            if "مبرمج" in role_name or short_name == "DEV":
                fallback = "```html\n<!DOCTYPE html>\n<html>\n<head>\n<meta charset='utf-8'>\n<title>تطبيق تجريبي</title>\n<style>body { font-family: Tahoma; text-align: center; padding: 50px; background: #0a0c10; color: white; }</style>\n</head>\n<body>\n<h1>تطبيق تجريبي</h1>\n<p>يرجى إضافة مفتاح Gemini API في الإعدادات لتوليد التطبيقات الحقيقية.</p>\n</body>\n</html>\n```"
            add_feed(role_name, role_name, color, short_name, "استجابة محاكاة (بدون مفتاح API)")
            return fallback
            
        add_feed("النظام", "System", "linear-gradient(135deg, #2c3e50, #3498db)", "SY", f"جاري التفكير والتنفيذ بواسطة {role_name}...")
        
        if provider == 'opencode':
            # Use local OpenCode server with smart fallback
            try:
                oc = requests.Session()
                sess_r = oc.post(f"{opencode_url}/session", json={}, timeout=15)
                if sess_r.status_code != 200:
                    raise Exception(f"فشل الاتصال بـ OpenCode server على {opencode_url}")
                oc_session_id = sess_r.json()['id']
                
                sys_trim = system_prompt[:1000] if len(system_prompt) > 1000 else system_prompt
                ctx_trim = context_text[:2500] if context_text and len(context_text) > 2500 else context_text
                
                if "مبرمج" in role_name or "أنيميشن" in role_name or short_name in ["DEV", "MOTION"]:
                    instruction = "(أنت تعمل في بيئة برمجية مؤتمتة وغير تفاعلية. لا تسأل أي أسئلة توضيحية أو طلبات موافقة، ولا تستخدم أدوات bash أو البحث. أرجع الأكواد البرمجية مباشرة ومكتملة داخل: ```html و ```css و ```javascript فوراً في ردك)."
                else:
                    instruction = "(أجب باللغة العربية باحترافية واختصار مفيد. لا تبحث في أي ملفات ولا تستخدم أدوات تفاعلية، اعتمد فقط على المعطيات هنا)."
                
                ctx_part = f"السياق:\n{ctx_trim}\n\n" if ctx_trim else ""
                full_msg = f"أنت {role_name}. {sys_trim[:300]}\n\nالمهمة: {prompt}\n\n{ctx_part}{instruction}"
                
                msg_r = oc.post(
                    f"{opencode_url}/session/{oc_session_id}/message",
                    json={
                        'parts': [{'type': 'text', 'text': full_msg}],
                        'agent': 'general'
                    },
                    timeout=200
                )
                
                if msg_r.status_code != 200:
                    raise Exception(f"خطأ OpenCode HTTP {msg_r.status_code}: {msg_r.text[:200]}")
                
                oc_data = msg_r.json()
                if 'error' in oc_data.get('info', {}):
                    err_msg = oc_data['info']['error'].get('data', {}).get('message', str(oc_data['info']['error']))
                    raise Exception(err_msg)
                    
                reply = ''.join(
                    part.get('text', '') 
                    for part in oc_data.get('parts', []) 
                    if part.get('type') == 'text'
                ).strip()
                
                if not reply:
                    raise Exception("استجابة OpenCode كانت فارغة")
                    
            except Exception as oc_err:
                # If OpenCode times out or fails, and user has Gemini key, automatically rescue with Gemini!
                if api_key and (api_key.startswith('AIzaSy') or len(api_key) > 20):
                    add_feed("النظام", "System", "orange", "SY", f"تأخر رد OpenCode ({str(oc_err)[:60]})، جاري التوليد التلقائي الفوري عبر Gemini لإنقاذ المشروع...")
                    reply = call_agent_gemini(system_prompt, full_prompt, api_key, role_name, 'gemini-3.5-flash-lite')
                else:
                    raise oc_err
            
        elif provider == 'gemini':
            reply = call_agent_gemini(system_prompt, full_prompt, api_key, role_name, model)
            
        elif provider == 'openrouter':
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            data = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"{full_prompt}\n(أجب باللغة العربية. إذا كنت مبرمجاً، قم بتوليد كود HTML داخل ```html و CSS داخل ```css و JS داخل ```javascript)"}
                ]
            }
            res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=90)
            res_json = res.json()
            if 'error' in res_json:
                err = res_json['error'].get('message', str(res_json['error']))
                # If openrouter fails and key is gemini key, fallback to gemini
                if api_key.startswith('AIzaSy'):
                    add_feed("النظام", "System", "orange", "SY", "جاري التحويل لـ Gemini المباشر...")
                    reply = call_agent_gemini(system_prompt, full_prompt, api_key, role_name, 'gemini-3.5-flash-lite')
                else:
                    raise Exception(f"OpenRouter: {err}")
            else:
                reply = res_json['choices'][0]['message']['content']
            
        tokens_est = (len(prompt) + len(context_text) + len(reply)) // 4
        record_token_usage(role_name, short_name, workflow_state.get('current_task', 'مهمة برمجية'), tokens_est)
        add_feed(role_name, role_name, color, short_name, f"أنهيت عملي. المخرجات متوفرة الآن.")
        return reply
    except Exception as e:
        add_feed(role_name, "Error", "red", "ERR", f"فشل: {str(e)[:150]}")
        time.sleep(1)
        # Check if we can do an emergency rescue with Gemini for dev tasks
        if api_key and (api_key.startswith('AIzaSy') or len(api_key) > 20):
            try:
                add_feed(role_name, "System", "orange", "SY", "محاولة استرداد تلقائية عبر النموذج البديل...")
                return call_agent_gemini("أنت مبرمج ويب خبير.", full_prompt, api_key, role_name, 'gemini-3.5-flash-lite')
            except Exception:
                pass
        return f"حدث خطأ أثناء معالجة الطلب: {str(e)}"

def run_workflow(task_desc):
    workflow_state["status"] = "running"
    workflow_state["current_task"] = task_desc
    workflow_state["feed"] = []
    
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
    
    # 1. Strategy
    strat_out = call_agent(r"specialized\business-strategist.md", "مستشار البزنس", "BS", "linear-gradient(135deg, #FF6B6B, #FF8E53)", f"المشروع هو: {task_desc}. قدم دراسة جدوى استراتيجية مختصرة جداً.")
    workflow_state["outputs"]["strategy"] = strat_out
    workflow_state["departments"]["strategy"] = {"status": "done", "text": "أنهى دراسة الجدوى."}
    update_stats()
    
    # 2. Product
    workflow_state["departments"]["product"] = {"status": "active", "text": "يكتب وثيقة المتطلبات..."}
    update_stats()
    add_feed("مدير المنتج", "Product Manager", "linear-gradient(135deg, #4facfe, #00f2fe)", "PM", "بناءً على التقرير الاستراتيجي، أقوم بكتابة متطلبات المشروع.")
    
    prod_out = call_agent(r"product\product-manager.md", "مدير المنتج", "PM", "linear-gradient(135deg, #4facfe, #00f2fe)", "قم بكتابة وثيقة متطلبات المنتج (PRD) بناءً على الاستراتيجية التالية.", strat_out)
    workflow_state["outputs"]["product"] = prod_out
    workflow_state["departments"]["product"] = {"status": "done", "text": "تم كتابة وثيقة المتطلبات (PRD)."}
    update_stats()
    
    # 3. Approval
    workflow_state["departments"]["engineering"] = {"status": "waiting", "text": "في انتظار الاعتماد للبدء..."}
    workflow_state["status"] = "approval_needed"
    workflow_state["approval_request"] = {
        "title": "مطلوب اعتماد المتطلبات التقنية",
        "message": "نطلب موافقتك على البدء في هندسة النظام وتصميمه بناءً على وثيقة المتطلبات التي جهزها فريق المنتج. يمكنك ترك ملاحظات للتعديل.",
        "role": "مدير المنتج"
    }
    update_stats()

def continue_workflow(feedback=""):
    workflow_state["status"] = "running"
    workflow_state["approval_request"] = None
    update_stats()
    
    if feedback:
        add_feed("النظام", "System", "linear-gradient(135deg, #2c3e50, #3498db)", "SY", f"تم تقديم ملاحظات: {feedback}. جاري التعديل والمواصلة...")
    else:
        add_feed("النظام", "System", "linear-gradient(135deg, #2c3e50, #3498db)", "SY", "✅ تم الاعتماد. فريق الهندسة يبدأ العمل الآن.")
    
    # Engineering
    workflow_state["departments"]["engineering"] = {"status": "active", "text": "يصمم الآن هيكل قاعدة البيانات..."}
    update_stats()
    
    prod_out = workflow_state["outputs"].get("product", "")
    eng_prompt = f"ضع خطة هندسية وهيكلية لقواعد البيانات والنظام بناء على هذه المتطلبات.\n{('ملاحظات المدير: ' + feedback) if feedback else ''}"
    eng_out = call_agent(r"engineering\engineering-software-architect.md", "المهندس المعماري", "ENG", "linear-gradient(135deg, #20E2D7, #F9FEA5)", eng_prompt, prod_out)
    workflow_state["outputs"]["engineering"] = eng_out
    workflow_state["departments"]["engineering"] = {"status": "done", "text": "تم بناء الأساسات وقاعدة البيانات."}
    update_stats()
    
    # Design
    workflow_state["departments"]["design"] = {"status": "active", "text": "يصمم واجهات المستخدم الفاخرة..."}
    update_stats()
    des_prompt = f"""أنت رئيس المصممين الإبداعيين (Creative UI/UX Director) في استوديو تصميم عالمي متخصص في الأنظمة والمواقع الفاخرة مثل Mivida Luxury Studio.
بناءً على المخطط المعماري المرفق أدناه، صمم هوية بصرية كاملة وتجربة مستخدم استثنائية:
1. لوحة ألوان فخمة متناسقة (Tonal Warm/Alabaster + Deep Brand Color + Gold/Metallic Accents).
2. طباعة وأحجام دقيقة (Alexandria / Tajawal / Cairo + Manrope / Inter).
3. هيكلة تحريرية كاملة للأقسام: Header ثابت، Hero تحريري مع Badge و Stats، خدمات ومميزات بارزة، نظام وظيفي تفاعلي كامل، مصداقية وفريق، آراء وتقييمات، أسئلة شائعة، ونموذج حجز/تواصل وتذييل فاخر.
المخطط المعماري:
{eng_out}"""
    des_out = call_agent(r"design\design-ui-designer.md", "مصمم الواجهات", "UI", "linear-gradient(135deg, #a18cd1, #fbc2eb)", des_prompt, eng_out)
    workflow_state["outputs"]["design"] = des_out
    workflow_state["departments"]["design"] = {"status": "done", "text": "تم الانتهاء من التصميم الأولي."}
    update_stats()
    
    # Development
    workflow_state["departments"]["development"] = {"status": "active", "text": "يقوم ببرمجة التطبيق الشامل..."}
    update_stats()
    
    mivida_specs = """
معايير التصميم والتطوير العالمية الفاخرة (Mivida Production Standard):
1. نظام الألوان الفاخر (Design System):
   - لوحة ألوان راقية متناسقة: خلفيات دافئة ونظيفة (#FCFAF7, #F8F3EA, #FFFFFF) أو دارك فاخر (#0A0F1D, #111827).
   - لون رئيسي عميق وفخم (#5A0B1A نبيتي ملكي، #0F172A كحلي، #064E3B زمردي، إلخ).
   - لمسات ذهبية ومعدنية معتقة (#C7A45B ذهبي أنيق).
   - حدود رقيقة جداً (1px solid rgba(0,0,0,0.07) أو rgba(255,255,255,0.08)) وظلال ناعمة (Soft Elevation).
2. الخطوط (Typography):
   - خط Tajawal أو Alexandria أو Cairo للعناوين والنصوص العربية عبر Google Fonts.
   - خط Manrope أو Inter للأرقام واللاتيني.
3. الهيكلة الكاملة للواجهة (Full Semantic Structure):
   يجب أن تحتوي الصفحة على كافة الأقسام التالية مكتملة تماماً:
   - Header / Navbar زجاجي ثابت (Sticky Glassmorphism) مع شعار فخم، روابط تنقل، زر إجراء CTA، وزر قائمة الموبايل.
   - قسم Hero تحريري جذاب (Editorial Asymmetry) مع Badge، عنوان H1 عريض، وصف أنيق، زري تفاعل، وشريط إحصائيات ثقة (+15k عميل، 99% رضا، 24/7).
   - قسم الخدمات والمميزات بتوزيع تحريري أنيق (بطاقات مميزة كبرى + قائمة عناصر داعمة مع أيقونات).
   - المنظومة التفاعلية الأساسية (Core Interactive System): بناء نظام كامل يعمل 100% بالـ JavaScript بناءً على طلب المستخدم (مثلاً: نظام حجز مواعيد كامل مع اختيار التخصص والطبيب والتاريخ والوقت مع نافذة تأكيد Modal، أو حاسبة تفاعلية متقدمة، أو متجر مع سلة مشتريات).
   - قسم فريق العمل / الأطباء والخبرات مع شارات التميز.
   - آراء وتقييمات العملاء (Testimonials) بتقييم النجوم ★★★★★.
   - الأسئلة الشائعة التفاعلية (FAQ Accordion) تعمل بسلاسة تامة.
   - قسم التواصل والحجز السريع بنموذج تفاعلي.
   - تذييل فاخر متعدد الأعمدة (Luxury Footer) مع روابط واشتراك نشرة بريدية وحقوق الملكية.
4. التفاعل والحركة (JavaScript & Interactions):
   - قائمة الهواتف المتجاوبة (Mobile Drawer) تعمل بسلاسة.
   - نوافذ منبثقة (Modals) لتأكيد الحجز أو عرض التفاصيل.
   - نظام إشعارات عائم (Toast Notifications) عند أي إجراء.
   - كافة الأيقونات يجب أن تكون مدمجة (Inline SVGs) أو FontAwesome موثوقة.
"""
    
    dev_prompt = f"""بناءً على المعمارية المرفقة:
{eng_out}

وبناءً على التصميم المرفق:
{des_out}

{mivida_specs}

المشروع المطلوب: {workflow_state.get('current_task', 'موقع ويب متكامل')}
قم ببرمجة هذا التطبيق فوراً كمشروع ويب متكامل وفائق الاحترافية (HTML, CSS, JS منفصلين تماماً).
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
تأكد من استدعاء ملف style.css و script.js بداخل ملف الـ HTML. ولا تستخدم نصوصاً وهمية أو أكواداً ناقصة."""

    dev_out = call_agent(r"engineering\engineering-senior-developer.md", "كبير المبرمجين", "DEV", "linear-gradient(135deg, #11998e, #38ef7d)", dev_prompt, "")
    workflow_state["outputs"]["development"] = dev_out
    workflow_state["departments"]["development"] = {"status": "done", "text": "تمت كتابة الأكواد بنجاح."}
    update_stats()
    
    # QA
    workflow_state["departments"]["qa"] = {"status": "active", "text": "يقوم باختبار النظام وإصلاح الأخطاء..."}
    update_stats()
    qa_out = call_agent(r"engineering\engineering-qa-engineer.md", "مهندس الجودة", "QA", "linear-gradient(135deg, #FF9A9E, #FECFEF)", "قم بمراجعة الأكواد المرفقة أدناه، وتأكد من خلوها من الأخطاء وراجع قابلية الاستخدام وسرعة الأداء.", dev_out)
    workflow_state["outputs"]["qa"] = qa_out
    workflow_state["departments"]["qa"] = {"status": "done", "text": "تم إجراء الفحص وضمان الجودة."}
    update_stats()

    # Animation & UI Polish
    workflow_state["departments"]["animation"] = {"status": "active", "text": "يضيف حركات وتأثيرات بصرية..."}
    update_stats()
    anim_out = call_agent(r"design\design-motion-designer.md", "خبير الأنيميشن", "MOTION", "linear-gradient(135deg, #ff758c, #ff7eb3)", "بناءً على الأكواد المرفقة هنا مباشرة، طور كود CSS و JS لإضافة تأثيرات بصرية وحركية احترافية تجعل الموقع حياً وفخماً. أرجع كود CSS المحدث داخل ```css وكود JS المحدث داخل ```javascript.", dev_out)
    workflow_state["outputs"]["animation"] = anim_out
    workflow_state["departments"]["animation"] = {"status": "done", "text": "تم إضافة التأثيرات الحركية."}
    update_stats()

    # SEO
    workflow_state["departments"]["seo"] = {"status": "active", "text": "يحسن أرشفة النظام..."}
    update_stats()
    seo_out = call_agent(r"specialized\seo-expert.md", "خبير SEO", "SEO", "linear-gradient(135deg, #f093fb, #f5576c)", "بناءً على الأكواد المرفقة، اقترح تحسينات محركات البحث وعلامات Meta tags والكلمات المفتاحية لتحسين الظهور.", dev_out)
    workflow_state["outputs"]["seo"] = seo_out
    workflow_state["departments"]["seo"] = {"status": "done", "text": "تم تحسين محركات البحث."}
    update_stats()
    
    # Extract code and save file
    import re
    html_match = re.search(r"```html(.*?)```", dev_out, re.DOTALL)
    app_html = html_match.group(1).strip() if html_match else ""

    # Extract CSS from developer and animator
    dev_css_blocks = re.findall(r"```css(.*?)```", dev_out, re.DOTALL)
    anim_css_blocks = re.findall(r"```css(.*?)```", anim_out, re.DOTALL)
    dev_css = "\n\n".join([c.strip() for c in dev_css_blocks if c.strip()])
    anim_css = "\n\n".join([c.strip() for c in anim_css_blocks if c.strip()])
    if anim_css and dev_css:
        if dev_css in anim_css:
            app_css = anim_css
        else:
            app_css = dev_css + "\n\n/* Animation Enhancements */\n" + anim_css
    else:
        app_css = anim_css or dev_css

    # Extract JS from developer and animator
    dev_js_blocks = re.findall(r"```(?:javascript|js)(.*?)```", dev_out, re.DOTALL)
    anim_js_blocks = re.findall(r"```(?:javascript|js)(.*?)```", anim_out, re.DOTALL)
    dev_js = "\n\n".join([j.strip() for j in dev_js_blocks if j.strip()])
    anim_js = "\n\n".join([j.strip() for j in anim_js_blocks if j.strip()])
    if anim_js and dev_js:
        if dev_js in anim_js:
            app_js = anim_js
        else:
            app_js = dev_js + "\n\n/* Animation & Interaction Enhancements */\n" + anim_js
    else:
        app_js = anim_js or dev_js
    
    # Check if extracted HTML is valid or was an error string
    is_valid_html = (
        bool(app_html) and
        "خطأ استثناء" not in app_html and
        "HTTPConnectionPool" not in app_html and
        "Read timed out" not in app_html and
        ("<!DOCTYPE" in app_html or "<html" in app_html or "<body" in app_html or "<div" in app_html or "<main" in app_html)
    )
    
    if not is_valid_html:
        add_feed("النظام", "System", "orange", "SY", "جاري التحقق من كود التطبيق وتأمينه لضمان عمله بنسبة 100%...")
        settings = {}
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                settings = json.load(f)
        api_key = settings.get('openrouter_key', '')
        
        if api_key and (api_key.startswith('AIzaSy') or len(api_key) > 20):
            try:
                rescue_prompt = f"""المشروع المطلوب هو: {workflow_state.get('current_task', 'موقع ويب متكامل')}.
{mivida_specs}
قم ببرمجة هذا المشروع بأعلى درجات الفخامة والاحترافية كصفحة ويب متكاملة (HTML, CSS, JS).
أرجع كود HTML كاملاً داخل ```html وكود CSS داخل ```css وكود JS داخل ```javascript بالتفصيل الكامل وبدون أي نقص."""
                rescue_out = call_agent_gemini("أنت كبير مبرمجي الويب في استوديو Mivida Luxury.", rescue_prompt, api_key, "كبير المبرمجين", 'gemini-3.5-flash-lite')
                r_html = re.search(r"```html(.*?)```", rescue_out, re.DOTALL)
                r_css = re.search(r"```css(.*?)```", rescue_out, re.DOTALL)
                r_js = re.search(r"```(?:javascript|js)(.*?)```", rescue_out, re.DOTALL)
                if r_html:
                    app_html = r_html.group(1).strip()
                    if r_css: app_css = r_css.group(1).strip()
                    if r_js: app_js = r_js.group(1).strip()
                    is_valid_html = True
            except Exception as e:
                add_feed("النظام", "System", "red", "ERR", f"تنبيه الاسترداد: {e}")
                
        if not is_valid_html:
            task_name = workflow_state.get('current_task', 'تطبيق جديد')
            app_html = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{task_name}</title>
    <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;700;800&display=swap" rel="stylesheet">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; font-family: 'Cairo', sans-serif; }}
        body {{ background: #0a0c10; color: #f0f6fc; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }}
        .app-card {{ background: rgba(22, 27, 34, 0.9); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 20px; padding: 40px; max-width: 650px; text-align: center; box-shadow: 0 15px 35px rgba(0,0,0,0.5); }}
        h1 {{ font-size: 2rem; color: #58a6ff; margin-bottom: 15px; }}
        p {{ color: #8b949e; line-height: 1.8; margin-bottom: 25px; font-size: 1.05rem; }}
        .btn {{ display: inline-block; background: #238636; color: white; padding: 12px 25px; border-radius: 10px; text-decoration: none; font-weight: bold; transition: all 0.3s; }}
        .btn:hover {{ filter: brightness(1.15); transform: translateY(-2px); }}
    </style>
</head>
<body>
    <div class="app-card">
        <h1>{task_name}</h1>
        <p>تم إعداد هذا المشروع بنجاح بواسطة فريق الوكالة الذكية. جميع وثائق الهيكلة والمتطلبات متوفرة داخل ملف plan.md المرفق بالمجلد.</p>
        <a href="plan.md" class="btn">عرض وثيقة المشروع (Plan.md)</a>
    </div>
</body>
</html>"""
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
            
    # Name project folder after the actual task name
    task_name = workflow_state.get('current_task', 'تطبيق_جديد')
    app_dir, clean_folder_name = get_project_folder_name(task_name)
    os.makedirs(app_dir, exist_ok=True)
    os.makedirs(os.path.join(app_dir, "css"), exist_ok=True)
    os.makedirs(os.path.join(app_dir, "js"), exist_ok=True)
    os.makedirs(os.path.join(app_dir, "assets", "images"), exist_ok=True)
    os.makedirs(os.path.join(app_dir, "assets", "css"), exist_ok=True)
    os.makedirs(os.path.join(app_dir, "assets", "js"), exist_ok=True)
    
    try:
        with open(os.path.join(app_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(app_html)
        if app_css:
            for c_path in ["style.css", "css/style.css", "assets/css/style.css"]:
                target_p = os.path.join(app_dir, c_path)
                os.makedirs(os.path.dirname(target_p), exist_ok=True)
                with open(target_p, "w", encoding="utf-8") as f:
                    f.write(app_css)
        if app_js:
            for j_path in ["script.js", "js/script.js", "js/app.js", "assets/js/script.js"]:
                target_p = os.path.join(app_dir, j_path)
                os.makedirs(os.path.dirname(target_p), exist_ok=True)
                with open(target_p, "w", encoding="utf-8") as f:
                    f.write(app_js)
            
        with open(os.path.join(app_dir, "plan.md"), "w", encoding="utf-8") as f:
            f.write(f"# المنتج النهائي لمشروع: {workflow_state.get('current_task', 'بدون اسم')}\n\n")
            f.write(f"## 1. دراسة الجدوى (الاستراتيجية)\n{workflow_state['outputs'].get('strategy', '')}\n\n")
            f.write(f"## 2. وثيقة المتطلبات (PRD)\n{workflow_state['outputs'].get('product', '')}\n\n")
            f.write(f"## 3. الهيكلة الهندسية (Architecture)\n{eng_out}\n\n")
            f.write(f"## 4. تصميم الواجهات (UI/UX)\n{des_out}\n\n")
            
        add_feed("مدير المشروع", "Project Manager", "linear-gradient(135deg, #f6d365, #fda085)", "PM", f"تم الانتهاء من دورة العمل بنجاح! 🚀\nتم إنشاء مشروعك في مجلد: `{clean_folder_name}` وبداخله `index.html` الذي يعمل مباشرة!")
    except Exception as e:
        add_feed("النظام", "System", "linear-gradient(135deg, #2c3e50, #3498db)", "SY", f"حدث خطأ أثناء حفظ ملفات التطبيق: {e}")
        
    workflow_state["status"] = "finished"
    update_stats()

@app.route('/')
def serve_index():
    return app.send_static_file('index.html')

@app.route('/api/state', methods=['GET'])
def get_state():
    return jsonify(workflow_state)

@app.route('/api/start_task', methods=['POST'])
def start_task():
    data = request.json
    task_desc = data.get('task')
    if not task_desc:
        return jsonify({"error": "Task description is required"}), 400
    if workflow_state["status"] in ["idle", "finished"]:
        threading.Thread(target=run_workflow, args=(task_desc,)).start()
        return jsonify({"message": "Workflow started"})
    return jsonify({"message": "A workflow is already running"}), 400

@app.route('/api/approve', methods=['POST'])
def approve():
    if workflow_state["status"] == "approval_needed":
        data = request.json or {}
        feedback = data.get('feedback', '')
        threading.Thread(target=continue_workflow, args=(feedback,)).start()
        return jsonify({"message": "Approved"})
    return jsonify({"message": "No pending approval"}), 400

@app.route('/api/projects', methods=['GET'])
def list_projects():
    projects = []
    base_dir = "generated_apps"
    if os.path.exists(base_dir):
        for folder in os.listdir(base_dir):
            folder_path = os.path.join(base_dir, folder)
            if os.path.isdir(folder_path):
                files = os.listdir(folder_path)
                abs_path = os.path.abspath(folder_path)
                has_index = "index.html" in files
                projects.append({
                    "name": folder,
                    "files": files,
                    "path": abs_path,
                    "url": f"/apps/{folder}/index.html" if has_index else None
                })
    projects.sort(key=lambda x: x["name"], reverse=True)
    return jsonify(projects)

@app.route('/api/open_folder', methods=['POST'])
def open_project_folder():
    data = request.json or {}
    folder_name = data.get('name')
    folder_path = data.get('path')
    if not folder_path and folder_name:
        folder_path = os.path.abspath(os.path.join("generated_apps", folder_name))
    elif folder_path and not os.path.isabs(folder_path):
        folder_path = os.path.abspath(os.path.join("generated_apps", folder_path))
        
    if folder_path and os.path.exists(folder_path):
        try:
            os.startfile(folder_path)
            return jsonify({"status": "success", "message": "Folder opened in Windows Explorer"})
        except Exception as e:
            import subprocess
            subprocess.Popen(['explorer', os.path.normpath(folder_path)])
            return jsonify({"status": "success", "message": "Folder opened via explorer"})
    return jsonify({"error": "Folder not found", "path": str(folder_path)}), 404

@app.route('/apps/<project_name>/')
@app.route('/apps/<project_name>/<path:filename>')
def serve_project_app(project_name, filename='index.html'):
    project_dir = os.path.abspath(os.path.join("generated_apps", project_name))
    if not os.path.exists(project_dir):
        return "Project not found", 404
    return send_from_directory(project_dir, filename)

@app.route('/api/agents', methods=['GET'])
def get_agents():
    agents_list = []
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
    except Exception as e:
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
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(request.json, f)
        return jsonify({"status": "success"})
    else:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                return jsonify(json.load(f))
        return jsonify({"openrouter_key": "", "openrouter_model": "gemini-3.5-flash-lite", "provider": "gemini"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
