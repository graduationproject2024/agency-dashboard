const feedContainer = document.getElementById('feed-container');
const deptStrategy = document.getElementById('dept-strategy');
const deptProduct = document.getElementById('dept-product');
const deptEngineering = document.getElementById('dept-engineering');
const deptDesign = document.getElementById('dept-design');
const deptDevelopment = document.getElementById('dept-development');
const deptQA = document.getElementById('dept-qa');
const deptAnimation = document.getElementById('dept-animation');
const deptSEO = document.getElementById('dept-seo');
const approvalBox = document.getElementById('approval-box');
const taskModal = document.getElementById('taskModal');
const projectTitle = document.getElementById('project-title');

let lastFeedId = 0;
let globalOutputs = {};

// --- Security Utilities: XSS Sanitization & HTML Escaping ---

function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function sanitizeHtml(dirtyHtml) {
    if (!dirtyHtml) return '';
    if (typeof DOMPurify !== 'undefined' && DOMPurify.sanitize) {
        return DOMPurify.sanitize(dirtyHtml, {
            ALLOWED_TAGS: [
                'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'b', 'i', 'strong', 'em', 'strike',
                'code', 'pre', 'ul', 'ol', 'li', 'blockquote', 'hr', 'table', 'thead', 'tbody',
                'tr', 'th', 'td', 'br', 'span', 'div'
            ],
            ALLOWED_ATTR: ['class', 'style']
        });
    }
    // Fallback sanitizer if DOMPurify is unavailable: strip script, iframe, and inline handlers
    return String(dirtyHtml)
        .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '')
        .replace(/<iframe\b[^<]*(?:(?!<\/iframe>)<[^<]*)*<\/iframe>/gi, '')
        .replace(/<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>/gi, '')
        .replace(/\son\w+="[^"]*"/gi, '')
        .replace(/\son\w+='[^']*'/gi, '')
        .replace(/javascript:[^"']*/gi, '');
}

function updateNodeState(nodeId, status) {
    const node = document.getElementById('node-' + nodeId);
    if (!node) return;
    node.classList.remove('active', 'done', 'failed');
    if (status === 'active') node.classList.add('active');
    if (status === 'done') node.classList.add('done');
    if (status === 'failed') node.classList.add('failed');
}

function createFeedItem(feed) {
    const div = document.createElement('div');
    div.className = 'feed-item';
    const safeInitial = escapeHtml(feed.initial);
    const safeName = escapeHtml(feed.name);
    const safeRole = escapeHtml(feed.role);
    const safeText = escapeHtml(feed.text);
    const safeTime = escapeHtml(feed.time);
    const safeColor = escapeHtml(feed.color || 'var(--accent-blue)');

    div.innerHTML = `
        <div class="feed-avatar" style="background: ${safeColor}">${safeInitial}</div>
        <div class="feed-content">
            <h4>${safeName} <span>${safeRole}</span></h4>
            <p>${safeText}</p>
            <span class="feed-time">${safeTime}</span>
        </div>
    `;
    return div;
}

function updateDepartmentUI(element, data) {
    if (!element) return;
    const textEl = element.querySelector('.status');
    element.className = 'dept-item';
    if (textEl) textEl.textContent = data.text;
    
    if (data.status === 'active') {
        element.classList.add('active-dept');
        if (textEl) textEl.className = 'status text-blue';
    } else if (data.status === 'done') {
        if (textEl) textEl.className = 'status text-green';
    } else if (data.status === 'failed') {
        element.classList.add('failed-dept');
        if (textEl) textEl.className = 'status text-red';
    } else if (data.status === 'waiting') {
        element.classList.add('waiting-dept');
        if (textEl) textEl.className = 'status text-gray';
    } else {
        if (textEl) textEl.className = 'status text-gray';
    }
}

async function fetchState() {
    try {
        const res = await fetch('/api/state');
        if (!res.ok) return;
        const state = await res.json();
        
        // Update Title safely
        if (state.current_task && projectTitle) {
            projectTitle.textContent = state.current_task;
        }

        if (state.outputs) {
            globalOutputs = state.outputs;
        }

        // Update Departments & Pipeline Nodes
        const depts = ['strategy', 'product', 'engineering', 'design', 'development', 'qa', 'animation', 'seo'];
        const elements = {
            strategy: deptStrategy, product: deptProduct, engineering: deptEngineering,
            design: deptDesign, development: deptDevelopment, qa: deptQA,
            animation: deptAnimation, seo: deptSEO
        };

        depts.forEach(d => {
            if (state.departments && state.departments[d]) {
                updateDepartmentUI(elements[d], state.departments[d]);
                updateNodeState(d, state.departments[d].status);
            }
        });

        // Update Feed safely
        if (state.feed && state.feed.length > 0) {
            const newFeeds = state.feed.filter(f => f.id > lastFeedId);
            if (newFeeds.length > 0) {
                newFeeds.reverse().forEach(feed => {
                    const item = createFeedItem(feed);
                    if (feedContainer) {
                        feedContainer.insertBefore(item, feedContainer.firstChild);
                    }
                });
                lastFeedId = state.feed[0].id;
            }
        }

        // Update Approval Box
        if (state.status === 'approval_needed' && state.approval_request) {
            if (approvalBox) {
                approvalBox.style.display = 'block';
                const titleEl = document.getElementById('approval-title');
                const msgEl = document.getElementById('approval-msg');
                const roleEl = document.getElementById('approval-role-badge');
                if (titleEl) titleEl.textContent = state.approval_request.title;
                if (msgEl) msgEl.textContent = state.approval_request.message;
                if (roleEl) roleEl.textContent = state.approval_request.role || 'مدير المنتج';
            }
            renderPendingApprovalSection(state);
        } else {
            if (approvalBox) approvalBox.style.display = 'none';
            renderPendingApprovalSection(state);
        }

        // Update Stats Counters
        if (state.stats) {
            const totalEl = document.getElementById('stat-total-agents');
            const activeEl = document.getElementById('stat-active-agents');
            const pendingEl = document.getElementById('stat-pending-approvals');
            if (totalEl) totalEl.textContent = state.stats.total_agents;
            if (activeEl) activeEl.textContent = state.stats.active_agents;
            if (pendingEl) pendingEl.textContent = state.stats.pending_approvals;
            
            const badge = document.getElementById('sidebar-badge');
            if (badge) {
                badge.textContent = state.stats.pending_approvals;
                badge.style.display = state.stats.pending_approvals > 0 ? 'inline-block' : 'none';
            }
        }

        // Update Live Budget Metrics
        if (state.budget) {
            updateBudgetUI(state.budget);
        }

        // Calculate and update Progress bar honestly
        let progress = 0;
        const progressTextEl = document.getElementById('project-progress-text');
        const progressBarEl = document.getElementById('project-progress');
        
        if (state.status === 'completed' || state.status === 'finished') {
            progress = 100;
            if (progressTextEl) progressTextEl.textContent = '100% مكتمل ومفحوص';
        } else if (state.status === 'failed') {
            if (progressTextEl) progressTextEl.textContent = 'تعثر سير العمل (فشل الفحص)';
            if (progressBarEl) progressBarEl.style.background = 'var(--accent-red)';
        } else if (state.status !== 'idle' && state.departments) {
            const deptsList = Object.values(state.departments);
            const doneCount = deptsList.filter(d => d.status === 'done').length;
            progress = Math.min(95, doneCount * 12 + (state.status === 'approval_needed' ? 10 : 0));
            if (progressTextEl) progressTextEl.textContent = progress + '% جاري التنفيذ';
            if (progressBarEl) progressBarEl.style.background = 'linear-gradient(90deg, var(--accent-blue), #8a2be2)';
        } else {
            if (progressTextEl) progressTextEl.textContent = '0% في انتظار البدء';
        }
        
        if (progressBarEl && state.status !== 'failed') {
            progressBarEl.style.width = progress + '%';
        }

    } catch (e) {
        console.error("Error fetching state:", e);
    }
}

setInterval(fetchState, 1500);

function renderPendingApprovalSection(state) {
    const pendingContainer = document.getElementById('section-approvals-pending');
    if (!pendingContainer) return;
    
    if (state.status === 'approval_needed' && state.approval_request) {
        const safeRole = escapeHtml(state.approval_request.role || 'مدير المنتج');
        const safeTitle = escapeHtml(state.approval_request.title);
        const safeTask = escapeHtml(state.current_task || 'المشروع الحالي');
        const safeMessage = escapeHtml(state.approval_request.message);

        pendingContainer.innerHTML = `
            <div class="approval-card-large">
                <div class="approval-card-header">
                    <div class="approval-badge-urgent"><i class="fa-solid fa-triangle-exclamation"></i> مطلوب قرارك الآن</div>
                    <span class="approval-role-badge"><i class="fa-solid fa-user-tie"></i> ${safeRole}</span>
                </div>
                <h2 style="margin: 10px 0 8px 0; color: #fff; font-size: 1.35rem;">${safeTitle}</h2>
                <p style="color: var(--accent-blue); font-size: 0.95rem; margin-bottom: 12px;">
                    <i class="fa-solid fa-folder-open"></i> المشروع قيد التنفيذ: <strong>${safeTask}</strong>
                </p>
                <div class="approval-desc-box">
                    <p style="margin: 0; line-height: 1.6;">${safeMessage}</p>
                </div>
                <div class="approval-docs-row">
                    <h4 style="margin-bottom: 12px; font-size: 0.95rem; color: #fff;"><i class="fa-solid fa-book-open text-blue"></i> تقارير ومخرجات الفريق للاعتماد:</h4>
                    <div class="approval-buttons-group">
                        <button class="btn btn-secondary" onclick="viewOutput('product')">
                            <i class="fa-solid fa-file-contract text-blue"></i> قراءة وثيقة المتطلبات (PRD)
                        </button>
                        <button class="btn btn-secondary" onclick="viewOutput('strategy')">
                            <i class="fa-solid fa-chart-line text-blue"></i> قراءة دراسة الجدوى والاستراتيجية
                        </button>
                    </div>
                </div>
                <div class="form-group" style="margin-top: 18px;">
                    <label for="section-approvals-feedback"><i class="fa-solid fa-comment-dots"></i> ملاحظات أو توجيهات إضافية للفريق (اختياري):</label>
                    <textarea id="section-approvals-feedback" rows="3" placeholder="اكتب أي ملاحظات أو توجيهات ترغب في أخذها بالحسبان قبل المتابعة..."></textarea>
                </div>
                <div class="actions" style="margin-top: 18px;">
                    <button class="btn btn-success btn-lg approval-btn-submit" onclick="approveTask('section-approvals-feedback')">
                        <i class="fa-solid fa-circle-check"></i> موافق / اعتماد الخطوة ومواصلة العمل
                    </button>
                </div>
            </div>
        `;
    } else {
        pendingContainer.innerHTML = `
            <div class="glass-panel" style="text-align: center; padding: 40px; color: var(--text-muted);">
                <i class="fa-solid fa-circle-check" style="font-size: 3rem; color: var(--accent-green); margin-bottom: 15px; display: block;"></i>
                <h3>لا توجد موافقات معلقة حالياً</h3>
                <p>جميع خطط وسير عمل الفريق تسير بشكل آلي ومستمر، وسيظهر لك تنبيه هنا فور وصول دورك لاعتماد أي خطوة.</p>
            </div>
        `;
    }
}

function openModal() {
    if (taskModal) taskModal.style.display = 'flex';
}

async function startTask() {
    const taskInputEl = document.getElementById('task-input');
    const taskInput = taskInputEl ? taskInputEl.value.trim() : '';
    if (!taskInput) return;
    
    try {
        const res = await fetch('/api/start_task', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task: taskInput })
        });
        const data = await res.json();
        if (!res.ok) {
            alert(data.error || 'تعذر بدء المهمة');
            return;
        }
        closeModal('taskModal');
        if (taskInputEl) taskInputEl.value = '';
        if (feedContainer) feedContainer.innerHTML = '';
        lastFeedId = 0;
    } catch(err) {
        alert('فشل الاتصال بالخادم');
    }
}

async function approveTask(feedbackInputId = 'approval-feedback') {
    const feedbackEl = document.getElementById(feedbackInputId) || document.getElementById('approval-feedback') || document.getElementById('section-approvals-feedback');
    const feedback = feedbackEl ? feedbackEl.value.trim() : '';
    
    const submitBtns = document.querySelectorAll('.approval-btn-submit');
    submitBtns.forEach(b => {
        b.disabled = true;
        b.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> جاري الاعتماد...`;
    });
    
    try {
        const res = await fetch('/api/approve', { 
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ feedback: feedback })
        });
        const data = await res.json();
        if (res.ok) {
            saveApprovalToHistory({
                date: new Date().toLocaleDateString('ar-EG', { year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
                title: document.getElementById('approval-title')?.textContent || 'اعتماد المتطلبات والمواصفات',
                department: 'إدارة المنتجات والهندسة',
                status: 'تم الاعتماد'
            });
            if (feedbackEl) feedbackEl.value = '';
        } else {
            alert(data.error || 'تعذر اعتماد الخطوة');
        }
    } catch (e) {
        console.error("Error in approveTask:", e);
        alert('حدث خطأ أثناء إرسال الاعتماد');
    }
    
    submitBtns.forEach(b => {
        b.disabled = false;
        b.innerHTML = `<i class="fa-solid fa-check"></i> موافق / نفذ التعديلات`;
    });
}

const deptNamesAr = {
    strategy: "دراسة الجدوى والاستراتيجية (Strategy)",
    product: "وثيقة متطلبات المنتج (PRD)",
    engineering: "المعمارية وهندسة النظام (Architecture)",
    design: "تصميم الواجهات وتجربة المستخدم (UI/UX)",
    development: "أكواد البرمجة والتطوير (Code)",
    qa: "تقرير فحص وضمان الجودة (QA)",
    animation: "تأثيرات الأنيميشن والحركة (Motion)",
    seo: "تقرير تحسين محركات البحث (SEO)"
};

let currentModalOutputText = "";

function viewOutput(dept) {
    const rawOutput = (globalOutputs && globalOutputs[dept]) ? globalOutputs[dept].trim() : '';
    const deptTitle = deptNamesAr[dept] || `قسم ${dept.toUpperCase()}`;
    
    const titleEl = document.getElementById('outputModalTitle');
    const bodyEl = document.getElementById('outputModalBody');
    const modalEl = document.getElementById('outputModal');
    
    if (titleEl) titleEl.textContent = `مخرجات: ${deptTitle}`;
    
    if (!rawOutput) {
        currentModalOutputText = "";
        if (bodyEl) {
            bodyEl.innerHTML = `
                <div class="empty-output-notice">
                    <i class="fa-solid fa-hourglass-half" style="font-size: 2.5rem; color: var(--accent-yellow); margin-bottom: 15px;"></i>
                    <h3>لا توجد مخرجات متوفرة لهذا القسم بعد</h3>
                    <p>هذا القسم لم يقم بإنتاج أي تقارير بعد، أو لا يزال في انتظار بدء دوره في سير العمل.</p>
                </div>
            `;
        }
    } else {
        currentModalOutputText = rawOutput;
        if (bodyEl) {
            if (typeof marked !== 'undefined' && marked.parse) {
                try {
                    bodyEl.innerHTML = sanitizeHtml(marked.parse(rawOutput));
                } catch(err) {
                    bodyEl.innerHTML = sanitizeHtml(formatMarkdownFallback(rawOutput));
                }
            } else {
                bodyEl.innerHTML = sanitizeHtml(formatMarkdownFallback(rawOutput));
            }
        }
    }
    
    if (modalEl) modalEl.style.display = 'flex';
}

function formatMarkdownFallback(text) {
    if (!text) return '';
    let escaped = escapeHtml(text);
    
    // Code blocks
    escaped = escaped.replace(/```([a-zA-Z0-9]*)\n([\s\S]*?)```/g, '<pre><code class="$1">$2</code></pre>');
    // Inline code
    escaped = escaped.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Headers
    escaped = escaped.replace(/^### (.*$)/gim, '<h3>$1</h3>');
    escaped = escaped.replace(/^## (.*$)/gim, '<h2>$1</h2>');
    escaped = escaped.replace(/^# (.*$)/gim, '<h1>$1</h1>');
    // Bold & italic
    escaped = escaped.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    escaped = escaped.replace(/\*(.*?)\*/g, '<em>$1</em>');
    // Blockquotes
    escaped = escaped.replace(/^> (.*$)/gim, '<blockquote>$1</blockquote>');
    // Unordered lists
    escaped = escaped.replace(/^\s*-\s+(.*$)/gim, '<li>$1</li>');
    // Newlines
    escaped = escaped.replace(/\n/g, '<br>');
    return escaped;
}

function copyModalOutput() {
    if (!currentModalOutputText) return;
    navigator.clipboard.writeText(currentModalOutputText).then(() => {
        const btn = document.getElementById('copyOutputBtn');
        if (btn) {
            const originalHtml = btn.innerHTML;
            btn.innerHTML = `<i class="fa-solid fa-check text-green"></i> تم النسخ!`;
            setTimeout(() => { btn.innerHTML = originalHtml; }, 2000);
        }
    }).catch(err => {
        console.error("Copy failed:", err);
    });
}

function loadApprovalHistory() {
    const historyTbody = document.getElementById('approvals-history');
    if (!historyTbody) return;
    
    let history = [];
    try {
        const stored = localStorage.getItem('agency_approvals_history');
        if (stored) history = JSON.parse(stored);
    } catch(e) {}
    
    if (history.length === 0) {
        historyTbody.innerHTML = `
            <tr>
                <td colspan="4" style="padding: 25px; text-align: center; color: var(--text-muted);">
                    <i class="fa-solid fa-folder-open" style="font-size: 1.5rem; margin-bottom: 8px; display:block;"></i>
                    لا توجد قرارات سابقة مسجلة في الأرشيف حتى الآن.
                </td>
            </tr>
        `;
        return;
    }
    
    historyTbody.innerHTML = history.map(item => `
        <tr style="border-bottom: 1px solid rgba(255,255,255,0.05);">
            <td style="padding: 12px 10px; color: var(--text-muted); font-size: 0.9rem;">${escapeHtml(item.date)}</td>
            <td style="padding: 12px 10px; font-weight: 600; color: #fff;">${escapeHtml(item.title)}</td>
            <td style="padding: 12px 10px;"><span style="background: rgba(88,166,255,0.1); color: var(--accent-blue); padding: 3px 8px; border-radius: 6px; font-size: 0.85rem;">${escapeHtml(item.department)}</span></td>
            <td style="padding: 12px 10px; color: var(--accent-green); font-weight: 600;"><i class="fa-solid fa-circle-check"></i> ${escapeHtml(item.status)}</td>
        </tr>
    `).join('');
}

function saveApprovalToHistory(item) {
    let history = [];
    try {
        const stored = localStorage.getItem('agency_approvals_history');
        if (stored) history = JSON.parse(stored);
    } catch(e) {}
    history.unshift(item);
    if (history.length > 25) history = history.slice(0, 25);
    try {
        localStorage.setItem('agency_approvals_history', JSON.stringify(history));
    } catch(e) {}
    loadApprovalHistory();
}

function clearApprovalHistory() {
    if (confirm('هل أنت متأكد من مسح سجل القرارات والموافقات السابقة؟')) {
        localStorage.removeItem('agency_approvals_history');
        loadApprovalHistory();
    }
}

function closeModal(modalId) {
    if (modalId) {
        const el = document.getElementById(modalId);
        if (el) el.style.display = 'none';
    } else {
        if (taskModal) taskModal.style.display = 'none';
        const outModal = document.getElementById('outputModal');
        if (outModal) outModal.style.display = 'none';
    }
}

window.addEventListener('click', function(event) {
    if (event.target === taskModal) taskModal.style.display = 'none';
    const outputModal = document.getElementById('outputModal');
    if (event.target === outputModal) outputModal.style.display = 'none';
});

window.addEventListener('keydown', function(event) {
    if (event.key === 'Escape') {
        if (taskModal) taskModal.style.display = 'none';
        const outputModal = document.getElementById('outputModal');
        if (outputModal) outputModal.style.display = 'none';
    }
});

async function showSection(sectionId, navElement) {
    const sections = document.querySelectorAll('.view-section');
    sections.forEach(sec => sec.style.display = 'none');
    
    const target = document.getElementById('section-' + sectionId);
    if (target) {
        target.style.display = 'block';
    }

    if (navElement) {
        const links = document.querySelectorAll('nav a');
        links.forEach(link => link.classList.remove('active'));
        navElement.classList.add('active');
    }
    
    if (sectionId === 'approvals') {
        loadApprovalHistory();
    } else if (sectionId === 'agents') {
        loadAgents();
    } else if (sectionId === 'projects') {
        const list = document.getElementById('projects-container');
        if (!list) return;
        list.innerHTML = '<div style="text-align:center; grid-column: 1 / -1;"><i class="fa-solid fa-spinner fa-spin"></i> جاري جلب المشاريع...</div>';
        try {
            const res = await fetch('/api/projects');
            const projects = await res.json();
            list.innerHTML = '';
            if (projects.length === 0) {
                list.innerHTML = '<div style="text-align:center; grid-column: 1 / -1; color: var(--text-muted); padding: 30px;">لم يتم إنشاء مشاريع بعد.</div>';
                return;
            }
            projects.forEach(proj => {
                const div = document.createElement('div');
                div.style.cssText = 'background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); padding: 20px; border-radius: 12px; margin-bottom: 18px;';
                
                const safeName = escapeHtml(proj.name);
                const safePath = escapeHtml(proj.path);
                
                const safeUrl = (proj.url && typeof proj.url === 'string' && proj.url.startsWith('/apps/')) ? encodeURI(proj.url) : '';
                const playBtn = safeUrl 
                    ? `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="btn btn-sm btn-primary" style="display:inline-flex; align-items:center; gap:8px; text-decoration:none;"><i class="fa-solid fa-play"></i> تشغيل / تجربة المشروع في المتصفح</a>`
                    : '';

                const openFolderBtn = `<button type="button" class="btn btn-sm btn-secondary btn-open-folder" style="display:inline-flex; align-items:center; gap:8px;"><i class="fa-solid fa-folder-open" style="color:#d29922;"></i> فتح مجلد المشروع في اللاب توب</button>`;

                let fileLinks = (proj.files || []).map(f => {
                    const safeF = escapeHtml(f);
                    return `<a href="/apps/${encodeURIComponent(proj.name)}/${encodeURIComponent(f)}" target="_blank" rel="noopener noreferrer" style="display:inline-block; margin-left:8px; margin-top:6px; background:rgba(88,166,255,0.1); border: 1px solid rgba(88,166,255,0.2); padding:5px 12px; border-radius:6px; color:var(--accent-blue); text-decoration:none; font-size:0.85rem;"><i class="fa-solid fa-file-code"></i> ${safeF}</a>`;
                }).join('');
                
                div.innerHTML = `
                    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; margin-bottom:12px;">
                        <div style="display:flex; align-items:center; gap:12px;">
                            <div style="width:42px; height:42px; border-radius:10px; background:rgba(88,166,255,0.15); display:flex; align-items:center; justify-content:center; color:var(--accent-blue); font-size:1.3rem;">
                                <i class="fa-solid fa-laptop-code"></i>
                            </div>
                            <div>
                                <h3 style="margin:0; font-size:1.15rem; color:#fff;">${safeName}</h3>
                                <span style="font-size:0.8rem; color:#8b949e; direction:ltr; text-align:right; display:block;">📁 ${safePath}</span>
                            </div>
                        </div>
                        <div style="display:flex; gap:10px; flex-wrap:wrap;">
                            ${playBtn}
                            ${openFolderBtn}
                        </div>
                    </div>
                    <div style="border-top: 1px solid rgba(255,255,255,0.06); padding-top:12px; margin-top:10px;">
                        <div style="font-size:0.85rem; color:#8b949e; margin-bottom:6px;">ملفات المشروع المتاحة:</div>
                        <div>${fileLinks}</div>
                    </div>
                `;
                list.appendChild(div);
                
                const folderBtn = div.querySelector('.btn-open-folder');
                if (folderBtn) {
                    folderBtn.addEventListener('click', (e) => openFolderInExplorer(proj.name, e));
                }
            });
        } catch (e) {
            list.innerHTML = '<div style="text-align:center; grid-column: 1 / -1; color: red;">فشل في تحميل المشاريع.</div>';
        }
    } else if (sectionId === 'budget') {
        loadBudgetDetails();
    } else if (sectionId === 'settings') {
        try {
            const res = await fetch('/api/settings');
            const settings = await res.json();
            const keyInput = document.getElementById('openrouter-key');
            if (keyInput) {
                if (settings.has_key) {
                    keyInput.placeholder = "المفتاح مسجل ومحمي (" + (settings.key_masked || "محمي") + ") - اتركه فارغاً للاحتفاظ به";
                    keyInput.value = "";
                } else {
                    keyInput.placeholder = "AIzaSy... أو sk-or-v1-...";
                    keyInput.value = "";
                }
            }
            if (settings.openrouter_model && document.getElementById('openrouter-model')) {
                document.getElementById('openrouter-model').value = settings.openrouter_model;
            }
            if (settings.provider && document.getElementById('api-provider')) {
                document.getElementById('api-provider').value = settings.provider;
                toggleProvider();
            }
        } catch (e) {}
    }
}

async function openFolderInExplorer(folderName, evt) {
    const btn = evt ? evt.currentTarget : null;
    const origHtml = btn ? btn.innerHTML : '';
    if (btn) btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> جاري الفتح...';
    try {
        const res = await fetch('/api/open_folder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: folderName })
        });
        const data = await res.json();
        if (res.ok) {
            if (btn) {
                btn.innerHTML = '<i class="fa-solid fa-check text-green"></i> تم الفتح في اللاب!';
                setTimeout(() => { btn.innerHTML = origHtml; }, 2500);
            }
        } else {
            if (btn) btn.innerHTML = origHtml;
            alert(data.error || 'تعذر فتح المجلد على جهازك');
        }
    } catch (e) {
        if (btn) btn.innerHTML = origHtml;
        alert('تعذر الاتصال بالخادم لفتح المجلد');
    }
}

async function saveSettings() {
    const rawKey = (document.getElementById('openrouter-key')?.value || '').trim();
    const model = (document.getElementById('openrouter-model')?.value || '').trim();
    const provider = document.getElementById('api-provider')?.value || 'gemini';
    const opencodeAiProvider = document.getElementById('opencode-ai-provider')?.value || 'anthropic';
    const opencodeModel = (document.getElementById('opencode-model-id')?.value || '').trim();
    const btn = document.getElementById('save-settings-btn');
    const msg = document.getElementById('settings-msg');
    
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> جاري الحفظ...';
    }
    
    const payload = {
        openrouter_model: model,
        provider: provider,
        opencode_url: 'http://127.0.0.1:4096',
        opencode_ai_provider: opencodeAiProvider,
        opencode_model: opencodeModel
    };
    
    // Only send the key if the user entered a fresh, unmasked string
    if (rawKey && !rawKey.includes('...') && !rawKey.includes('***')) {
        payload.openrouter_key = rawKey;
    }
    
    try {
        const res = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = 'حفظ الإعدادات';
        }
        if (msg) {
            msg.textContent = data.message || 'تم حفظ الإعدادات بأمان!';
            msg.style.display = 'block';
            setTimeout(() => { msg.style.display = 'none'; }, 3000);
        }
        // Refresh settings view
        showSection('settings');
    } catch(err) {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = 'حفظ الإعدادات';
        }
        alert('فشل حفظ الإعدادات');
    }
}

function toggleProvider() {
    const providerEl = document.getElementById('api-provider');
    if (!providerEl) return;
    const provider = providerEl.value;
    const keyDiv = document.getElementById('lbl-api-key')?.parentElement;
    const modelDiv = document.getElementById('openrouter-model')?.parentElement;
    const opencodeDiv = document.getElementById('opencode-settings');
    
    if (provider === 'opencode') {
        if (keyDiv) keyDiv.style.display = 'none';
        if (modelDiv) modelDiv.style.display = 'none';
        if (opencodeDiv) opencodeDiv.style.display = 'block';
    } else {
        if (keyDiv) keyDiv.style.display = 'block';
        if (modelDiv) modelDiv.style.display = 'block';
        if (opencodeDiv) opencodeDiv.style.display = 'none';
        const lbl = document.getElementById('lbl-api-key');
        if (provider === 'gemini') {
            if (lbl) lbl.innerText = "مفتاح Gemini API";
        } else {
            if (lbl) lbl.innerText = "مفتاح OpenRouter API";
        }
    }
}

// Initial load for approval history
document.addEventListener('DOMContentLoaded', () => {
    loadApprovalHistory();
});
loadApprovalHistory();

// Agents Management
let allAgentsData = [];
let currentAgentDivision = 'all';
let currentStatusFilter = 'all';

async function loadAgents() {
    const list = document.getElementById('agents-list');
    if (!list) return;
    list.innerHTML = '<div style="text-align:center; grid-column: 1 / -1;"><i class="fa-solid fa-spinner fa-spin"></i> جاري جلب قائمة الموظفين...</div>';
    try {
        const res = await fetch('/api/agents');
        allAgentsData = await res.json();
        
        const workingCount = allAgentsData.filter(a => a.status === 'working').length;
        const completedCount = allAgentsData.filter(a => a.status === 'completed').length;
        const idleCount = allAgentsData.filter(a => a.status === 'idle').length;
        const totalCount = allAgentsData.length;

        const countDisplay = document.getElementById('agents-count-display');
        if (countDisplay) countDisplay.textContent = totalCount;
        const totalEl = document.getElementById('agents-total-count');
        if (totalEl) totalEl.textContent = totalCount;

        const workingEl = document.getElementById('agents-working-count');
        if (workingEl) workingEl.textContent = workingCount;
        const tabWorkingEl = document.getElementById('tab-working-count');
        if (tabWorkingEl) tabWorkingEl.textContent = workingCount;

        const completedEl = document.getElementById('agents-completed-count');
        if (completedEl) completedEl.textContent = completedCount;
        const tabCompletedEl = document.getElementById('tab-completed-count');
        if (tabCompletedEl) tabCompletedEl.textContent = completedCount;

        const idleEl = document.getElementById('agents-idle-count');
        if (idleEl) idleEl.textContent = idleCount;
        const tabIdleEl = document.getElementById('tab-idle-count');
        if (tabIdleEl) tabIdleEl.textContent = idleCount;
        
        renderDivisionFilters();
        renderAgentsList();
    } catch (e) {
        list.innerHTML = '<div style="text-align:center; grid-column: 1 / -1; color: red;">فشل في تحميل الموظفين.</div>';
    }
}

function setStatusFilter(status) {
    currentStatusFilter = status;
    const tabs = ['all', 'working', 'completed', 'idle'];
    tabs.forEach(tab => {
        const btn = document.getElementById(`filter-btn-${tab}`);
        if (btn) {
            btn.className = (tab === status) ? 'btn btn-sm btn-primary' : 'btn btn-sm btn-secondary';
        }
    });
    renderAgentsList();
}

function renderDivisionFilters() {
    const filterContainer = document.getElementById('agents-division-filters');
    if (!filterContainer) return;
    
    const depts = Array.from(new Set(allAgentsData.map(a => a.department))).filter(Boolean);
    let html = `<button class="btn btn-sm ${currentAgentDivision === 'all' ? 'btn-primary' : 'btn-secondary'}" onclick="setAgentDivision('all')">كل الأقسام (${allAgentsData.length})</button>`;
    
    depts.forEach(d => {
        const count = allAgentsData.filter(a => a.department === d).length;
        const activeClass = currentAgentDivision === d ? 'btn-primary' : 'btn-secondary';
        const safeDept = escapeHtml(d);
        html += `<button class="btn btn-sm ${activeClass}" onclick="setAgentDivision('${safeDept}')">${safeDept} (${count})</button>`;
    });
    
    filterContainer.innerHTML = html;
}

function setAgentDivision(div) {
    currentAgentDivision = div;
    renderDivisionFilters();
    renderAgentsList();
}

function handleAgentsFilter() {
    renderAgentsList();
}

function renderAgentsList() {
    const list = document.getElementById('agents-list');
    if (!list) return;
    
    const query = (document.getElementById('agents-search-input')?.value || '').trim().toLowerCase();
    let filtered = allAgentsData;
    
    if (currentStatusFilter !== 'all') {
        filtered = filtered.filter(a => a.status === currentStatusFilter);
    }
    if (currentAgentDivision !== 'all') {
        filtered = filtered.filter(a => a.department === currentAgentDivision);
    }
    if (query) {
        filtered = filtered.filter(a => a.name.toLowerCase().includes(query) || a.department.toLowerCase().includes(query));
    }
    
    if (filtered.length === 0) {
        list.innerHTML = '<div style="text-align:center; grid-column: 1 / -1; color: var(--text-muted); padding: 40px; font-size: 1.1rem;"><i class="fa-solid fa-filter" style="margin-bottom:10px; font-size: 2rem; display:block; opacity:0.5;"></i>لا يوجد وكلاء يطابقون خيارات التصفية الحالية.</div>';
        return;
    }
    
    list.innerHTML = '';
    filtered.forEach(agent => {
        const card = document.createElement('div');
        const status = agent.status || 'idle';
        card.className = `agent-card status-${status}`;
        card.style.cssText = 'padding: 18px 16px; border-radius: 14px; text-align: center; position: relative;';
        
        const safeName = escapeHtml(agent.name);
        const safeDept = escapeHtml(agent.department);
        
        let badgeHtml = '';
        let iconColor = 'var(--accent-blue)';
        let statusSubtext = '';

        if (status === 'working') {
            badgeHtml = `<span style="position: absolute; top: 12px; right: 12px; font-size: 0.72rem; background: rgba(46, 160, 67, 0.25); border: 1px solid #3fb950; color: #3fb950; padding: 3px 8px; border-radius: 20px; font-weight: bold; display: flex; align-items: center;"><span class="pulse-dot"></span> شغال الآن</span>`;
            iconColor = '#3fb950';
            statusSubtext = `<div style="font-size: 0.78rem; color: #7ee787; margin-top: 8px; font-weight: 600;"><i class="fa-solid fa-gear fa-spin"></i> ينفذ مهام ${safeDept}</div>`;
        } else if (status === 'completed') {
            badgeHtml = `<span style="position: absolute; top: 12px; right: 12px; font-size: 0.72rem; background: rgba(56, 139, 253, 0.2); border: 1px solid #58a6ff; color: #58a6ff; padding: 3px 8px; border-radius: 20px; font-weight: bold;"><i class="fa-solid fa-check"></i> أنجز مهمته</span>`;
            iconColor = '#58a6ff';
            statusSubtext = `<div style="font-size: 0.78rem; color: #a5d6ff; margin-top: 8px;"><i class="fa-solid fa-circle-check"></i> أتم مخرجات ${safeDept}</div>`;
        } else {
            badgeHtml = `<span style="position: absolute; top: 12px; right: 12px; font-size: 0.72rem; background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255,255,255,0.1); color: #8b949e; padding: 3px 8px; border-radius: 20px; font-weight: 500;">⚪ وضع الاستعداد</span>`;
            iconColor = 'var(--text-muted)';
            statusSubtext = `<div style="font-size: 0.78rem; color: #8b949e; margin-top: 8px;">جاهز للتكليف القادم</div>`;
        }
            
        card.innerHTML = `
            ${badgeHtml}
            <div style="font-size: 2.2rem; margin-top: 10px; margin-bottom: 12px; color: ${iconColor};">
                <i class="fa-solid fa-user-gear"></i>
            </div>
            <h4 style="margin-bottom: 6px; font-size: 1rem; color: #fff; font-weight: 700;">${safeName}</h4>
            <span style="font-size: 0.75rem; background: rgba(88,166,255,0.12); color: var(--accent-blue); padding: 3px 10px; border-radius: 10px; display: inline-block;">${safeDept}</span>
            ${statusSubtext}
        `;
        list.appendChild(card);
    });
}

// Budget & Finance Management
async function loadBudgetDetails() {
    try {
        const res = await fetch('/api/budget');
        const budget = await res.json();
        updateBudgetUI(budget);
    } catch (e) {
        console.error("Budget fetch error:", e);
    }
}

function updateBudgetUI(budget) {
    if (!budget) return;
    const tokensEl = document.getElementById('budget-tokens');
    const costEl = document.getElementById('budget-cost');
    const limitEl = document.getElementById('budget-limit');
    
    if (tokensEl) tokensEl.textContent = (budget.tokens_used || 0).toLocaleString();
    if (costEl) costEl.textContent = '$' + (budget.estimated_cost || 0).toFixed(4);
    if (limitEl) {
        const remaining = Math.max(0, (budget.budget_limit || 50.0) - (budget.estimated_cost || 0));
        limitEl.textContent = '$' + remaining.toFixed(2);
    }
    
    const tableBody = document.getElementById('budget-transactions');
    if (tableBody && budget.transactions && budget.transactions.length > 0) {
        tableBody.innerHTML = '';
        budget.transactions.forEach(tx => {
            const tr = document.createElement('tr');
            tr.style.cssText = 'border-bottom: 1px solid rgba(255,255,255,0.05);';
            const safeTime = escapeHtml(tx.time);
            const safeTask = escapeHtml(tx.task);
            const safeAgent = escapeHtml(tx.agent);
            const safeDept = escapeHtml(tx.department);
            const safeSource = escapeHtml(tx.source || 'estimated');
            
            tr.innerHTML = `
                <td style="padding: 12px 10px; font-size: 0.85rem; color: var(--text-muted);">${safeTime}</td>
                <td style="padding: 12px 10px; font-weight: 500; color: #fff;">${safeTask}</td>
                <td style="padding: 12px 10px; color: var(--accent-blue);"><i class="fa-solid fa-robot"></i> ${safeAgent}</td>
                <td style="padding: 12px 10px;"><span style="font-size: 0.8rem; background: rgba(255,255,255,0.08); padding: 3px 8px; border-radius: 6px;">${safeDept}</span></td>
                <td style="padding: 12px 10px; font-weight: bold; color: #e6edf3;">${(tx.tokens || 0).toLocaleString()} <span style="font-size: 0.7rem; color: #8b949e;">(${safeSource})</span></td>
                <td style="padding: 12px 10px; font-weight: bold; color: #3fb950;">$${(tx.cost || 0).toFixed(4)}</td>
            `;
            tableBody.appendChild(tr);
        });
    }
}
