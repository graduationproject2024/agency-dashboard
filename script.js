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
    // Fail-closed security policy: if DOMPurify is unavailable, escape all HTML entities
    return escapeHtml(dirtyHtml);
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

        // Update Session Persistence Bar on Overview
        if (state.persistence && state.persistence.session_id) {
            const sidEl = document.getElementById('overview-session-id');
            const sstatusEl = document.getElementById('overview-session-status');
            const scpEl = document.getElementById('overview-session-checkpoint');
            const stasksEl = document.getElementById('overview-session-tasks-count');
            
            if (sidEl) sidEl.textContent = state.persistence.session_id.substring(0, 16) + '...';
            if (sstatusEl) {
                const s = state.status || 'unknown';
                sstatusEl.textContent = s === 'running' ? 'نشطة' : (s === 'approval_needed' ? 'بانتظار الاعتماد' : (s === 'completed' ? 'مكتملة' : (s === 'failed' ? 'متعثرة' : s)));
                sstatusEl.style.background = s === 'running' ? 'rgba(46,160,67,0.2)' : (s === 'approval_needed' ? 'rgba(210,153,34,0.2)' : (s === 'completed' ? 'rgba(88,166,255,0.2)' : 'rgba(218,54,51,0.2)'));
                sstatusEl.style.color = s === 'running' ? '#3fb950' : (s === 'approval_needed' ? '#d29922' : (s === 'completed' ? '#58a6ff' : '#da3633'));
            }
            if (scpEl) {
                const cps = state.persistence.checkpoints || [];
                const lastCp = cps.length > 0 ? cps[0].step_name : (state.approval_request ? 'prd_approval_gate' : 'بدء التنفيذ');
                scpEl.textContent = lastCp;
            }
            if (stasksEl) {
                stasksEl.textContent = `${state.persistence.completed_tasks || 0} / ${state.persistence.task_count || 0}`;
            }
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

async function loadApprovalHistory() {
    const historyTbody = document.getElementById('approvals-history');
    if (!historyTbody) return;
    
    try {
        const res = await fetch('/api/approvals');
        if (res.ok) {
            const data = await res.json();
            const approvals = data.approvals || [];
            if (approvals.length > 0) {
                historyTbody.innerHTML = approvals.map(item => `
                    <tr style="border-bottom: 1px solid rgba(255,255,255,0.05);">
                        <td style="padding: 12px 10px; color: var(--text-muted); font-size: 0.9rem;">${escapeHtml(item.requested_at || item.created_at || '')}</td>
                        <td style="padding: 12px 10px; font-weight: 600; color: #fff;">${escapeHtml(item.action_type || item.policy_rule || item.approval_id)}</td>
                        <td style="padding: 12px 10px;"><span style="background: rgba(88,166,255,0.1); color: var(--accent-blue); padding: 3px 8px; border-radius: 6px; font-size: 0.85rem;">${escapeHtml(item.session_id || 'general')}</span></td>
                        <td style="padding: 12px 10px; color: ${item.status === 'approved' ? 'var(--accent-green)' : (item.status === 'pending' ? 'var(--accent-yellow)' : '#da3633')}; font-weight: 600;">
                            <i class="fa-solid ${item.status === 'approved' ? 'fa-circle-check' : (item.status === 'pending' ? 'fa-clock' : 'fa-circle-xmark')}"></i> ${escapeHtml(item.status)}
                        </td>
                    </tr>
                `).join('');
                return;
            }
        }
    } catch(e) {
        console.warn("Could not fetch server approvals, falling back to local storage:", e);
    }
    
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
    } else if (sectionId === 'orchestration') {
        loadOrchestrationData();
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
let allDivisionsData = [];
let currentAgentDivision = 'all';
let currentStatusFilter = 'all';

async function loadAgents() {
    const list = document.getElementById('agents-list');
    if (!list) return;
    list.innerHTML = '<div style="text-align:center; grid-column: 1 / -1;"><i class="fa-solid fa-spinner fa-spin"></i> جاري جلب قائمة الموظفين والأقسام...</div>';
    try {
        const [agentsRes, divisionsRes] = await Promise.all([
            fetch('/api/agents'),
            fetch('/api/divisions')
        ]);
        
        if (agentsRes.ok) {
            allAgentsData = await agentsRes.json();
        }
        if (divisionsRes.ok) {
            const divData = await divisionsRes.json();
            allDivisionsData = divData.divisions || [];
        }
        
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
        list.innerHTML = '<div style="text-align:center; grid-column: 1 / -1; color: red;">فشل في تحميل الموظفين والأقسام.</div>';
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
    
    filterContainer.innerHTML = '';
    
    const allBtn = document.createElement('button');
    allBtn.type = 'button';
    allBtn.className = `btn btn-sm ${currentAgentDivision === 'all' ? 'btn-primary' : 'btn-secondary'}`;
    allBtn.innerHTML = `<i class="fa-solid fa-layer-group"></i> كل الأقسام (${allAgentsData.length})`;
    allBtn.addEventListener('click', () => setAgentDivision('all'));
    filterContainer.appendChild(allBtn);
    
    if (allDivisionsData && allDivisionsData.length > 0) {
        allDivisionsData.forEach(d => {
            const count = d.discovered_count !== undefined ? d.discovered_count : (allAgentsData.filter(a => a.division_id === d.id || a.department === d.name).length);
            const isSelected = currentAgentDivision === d.id || currentAgentDivision === d.name;
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = `btn btn-sm ${isSelected ? 'btn-primary' : 'btn-secondary'}`;
            const safeIcon = escapeHtml(d.icon || 'fa-solid fa-folder');
            const safeName = escapeHtml(d.name);
            btn.title = d.description || `${d.name} (${d.name_ar || ''})`;
            btn.innerHTML = `<i class="${safeIcon}"></i> ${safeName} (${count})`;
            btn.addEventListener('click', () => setAgentDivision(d.id));
            filterContainer.appendChild(btn);
        });
    } else {
        const depts = Array.from(new Set(allAgentsData.map(a => a.department))).filter(Boolean);
        depts.forEach(d => {
            const count = allAgentsData.filter(a => a.department === d).length;
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = `btn btn-sm ${currentAgentDivision === d ? 'btn-primary' : 'btn-secondary'}`;
            btn.textContent = `${d} (${count})`;
            btn.addEventListener('click', () => setAgentDivision(d));
            filterContainer.appendChild(btn);
        });
    }
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
        filtered = filtered.filter(a => a.division_id === currentAgentDivision || a.division_name === currentAgentDivision || a.department === currentAgentDivision);
    }
    if (query) {
        filtered = filtered.filter(a => 
            (a.name && a.name.toLowerCase().includes(query)) ||
            (a.id && a.id.toLowerCase().includes(query)) ||
            (a.department && a.department.toLowerCase().includes(query)) ||
            (a.division_name && a.division_name.toLowerCase().includes(query)) ||
            (a.division_name_ar && a.division_name_ar.toLowerCase().includes(query)) ||
            (a.description && a.description.toLowerCase().includes(query))
        );
    }
    
    if (filtered.length === 0) {
        const selectedDiv = allDivisionsData.find(d => d.id === currentAgentDivision || d.name === currentAgentDivision);
        if (selectedDiv && !query && currentStatusFilter === 'all') {
            const safeDivIcon = escapeHtml(selectedDiv.icon || 'fa-solid fa-folder-open');
            const safeDivName = escapeHtml(selectedDiv.name);
            const safeDivDesc = escapeHtml(selectedDiv.description || 'لا يوجد وكلاء مسجلون في هذا القسم حالياً.');
            list.innerHTML = `
                <div style="text-align:center; grid-column: 1 / -1; color: var(--text-muted); padding: 50px 20px;">
                    <div style="font-size: 2.8rem; margin-bottom: 15px; opacity: 0.6; color: var(--accent-blue);">
                        <i class="${safeDivIcon}"></i>
                    </div>
                    <h3 style="color: #fff; margin-bottom: 8px;">قسم ${safeDivName} خالي من الوكلاء حالياً</h3>
                    <p style="font-size: 0.9rem; color: #8b949e; max-width: 500px; margin: 0 auto;">${safeDivDesc}</p>
                </div>
            `;
        } else {
            list.innerHTML = '<div style="text-align:center; grid-column: 1 / -1; color: var(--text-muted); padding: 40px; font-size: 1.1rem;"><i class="fa-solid fa-filter" style="margin-bottom:10px; font-size: 2rem; display:block; opacity:0.5;"></i>لا يوجد وكلاء يطابقون خيارات التصفية أو البحث الحالية.</div>';
        }
        return;
    }
    
    list.innerHTML = '';
    filtered.forEach(agent => {
        const card = document.createElement('div');
        const status = agent.status || 'idle';
        card.className = `agent-card status-${status}`;
        card.style.cssText = 'padding: 18px 16px; border-radius: 14px; text-align: center; position: relative; display: flex; flex-direction: column; justify-content: space-between;';
        
        const safeName = escapeHtml(agent.name);
        const safeDept = escapeHtml(agent.division_name || agent.department || '');
        const safeDesc = escapeHtml(agent.description || '');
        const divMeta = allDivisionsData.find(d => d.id === agent.division_id || d.name === safeDept);
        const safeIcon = escapeHtml(divMeta?.icon || 'fa-solid fa-user-gear');
        
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
        
        const descHtml = safeDesc ? `<p style="font-size: 0.78rem; color: #8b949e; margin: 8px 0 0 0; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;" title="${safeDesc}">${safeDesc}</p>` : '';
            
        card.innerHTML = `
            ${badgeHtml}
            <div>
                <div style="font-size: 2.2rem; margin-top: 10px; margin-bottom: 12px; color: ${iconColor};">
                    <i class="${safeIcon}"></i>
                </div>
                <h4 style="margin-bottom: 6px; font-size: 1rem; color: #fff; font-weight: 700;">${safeName}</h4>
                <span style="font-size: 0.75rem; background: rgba(88,166,255,0.12); color: var(--accent-blue); padding: 3px 10px; border-radius: 10px; display: inline-block;">${safeDept}</span>
                ${descHtml}
            </div>
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

// --- Phase 2.4-B Orchestration & Project Memory UI ---

let currentOrchSessionId = null;
let currentProjectMemories = [];

async function loadOrchestrationData() {
    try {
        const stateRes = await fetch('/api/state');
        if (!stateRes.ok) return;
        const state = await stateRes.json();
        
        const sessionId = state.persistence ? state.persistence.session_id : (state.session_id || null);
        currentOrchSessionId = sessionId;
        
        const sidEl = document.getElementById('orch-session-id');
        const sbadgeEl = document.getElementById('orch-session-badge');
        const sprojectEl = document.getElementById('orch-project-name');
        const sstageEl = document.getElementById('orch-current-stage');
        const scpEl = document.getElementById('orch-checkpoint');
        const resumeBtn = document.getElementById('orch-resume-btn');
        const cancelBtn = document.getElementById('orch-cancel-btn');
        
        if (sidEl) sidEl.textContent = sessionId || 'لا توجد جلسة نشطة';
        if (sprojectEl) sprojectEl.textContent = state.current_task || 'لم يبدأ بعد';
        
        let currentStage = '--';
        for (const [dept, dData] of Object.entries(state.departments || {})) {
            if (dData.status === 'active') {
                currentStage = dept;
                break;
            }
        }
        if (sstageEl) sstageEl.textContent = currentStage;
        
        if (sbadgeEl) {
            const st = state.status || 'idle';
            sbadgeEl.textContent = st === 'running' ? 'جارية' : (st === 'approval_needed' ? 'بانتظار الاعتماد' : (st === 'completed' ? 'مكتملة' : (st === 'failed' ? 'متعثرة' : st)));
            sbadgeEl.style.background = st === 'running' ? 'rgba(46,160,67,0.2)' : (st === 'approval_needed' ? 'rgba(210,153,34,0.2)' : (st === 'completed' ? 'rgba(88,166,255,0.2)' : 'rgba(218,54,51,0.2)'));
            sbadgeEl.style.color = st === 'running' ? '#3fb950' : (st === 'approval_needed' ? '#d29922' : (st === 'completed' ? '#58a6ff' : '#da3633'));
        }
        
        const checkpoints = (state.persistence && state.persistence.checkpoints) || [];
        const lastCp = checkpoints.length > 0 ? checkpoints[0].step_name : (state.approval_request ? 'prd_approval_gate' : '--');
        if (scpEl) scpEl.textContent = lastCp;
        
        if (resumeBtn) {
            resumeBtn.style.display = (sessionId && (state.status === 'approval_needed' || state.status === 'failed')) ? 'inline-flex' : 'none';
        }
        if (cancelBtn) {
            cancelBtn.style.display = (sessionId && state.status === 'running') ? 'inline-flex' : 'none';
        }
        
        // Render DAG tasks
        const tasks = (state.persistence && state.persistence.tasks) || [];
        renderTaskDAG(tasks);
        
        // Render Handoffs
        const handoffs = (state.persistence && state.persistence.handoffs) || [];
        renderHandoffs(handoffs);
        
        // Render Project Memory
        const memories = state.memory || [];
        currentProjectMemories = memories;
        renderMemory(memories);
        
        // Load sessions history
        loadSessionsHistory();
    } catch (err) {
        console.error("Error loading orchestration data:", err);
    }
}

function renderTaskDAG(tasks) {
    const container = document.getElementById('orch-tasks-container');
    if (!container) return;
    container.innerHTML = '';
    
    if (!tasks || tasks.length === 0) {
        container.innerHTML = '<div style="grid-column: 1 / -1; text-align: center; color: var(--text-muted); padding: 40px;"><i class="fa-solid fa-diagram-project" style="font-size: 2rem; margin-bottom: 10px; display: block; opacity: 0.5;"></i>لا توجد مهام مخططة للجلسة الحالية بعد. ابدأ مشروعاً جديداً لتوليد المخطط.</div>';
        return;
    }
    
    tasks.forEach(t => {
        const card = document.createElement('div');
        const st = t.status || 'pending';
        card.className = `task-dag-card status-${st}`;
        
        const safeTitle = escapeHtml(t.title);
        const safeAgent = escapeHtml(t.assigned_agent);
        const safeDiv = escapeHtml(t.division_id || 'engineering');
        const safeId = escapeHtml(t.id);
        const deps = t.dependencies || [];
        const depsHtml = deps.length > 0 
            ? deps.map(d => `<span style="font-size: 0.72rem; background: rgba(255,255,255,0.06); padding: 2px 6px; border-radius: 4px; color: #8b949e;">${escapeHtml(d)}</span>`).join(' ')
            : '<span style="font-size: 0.72rem; color: #7ee787;">لا توجد (مهمة أساسية)</span>';
            
        let statusBadge = '<span style="font-size: 0.75rem; color: #8b949e;">⚪ في الانتظار</span>';
        if (st === 'running') {
            statusBadge = '<span style="font-size: 0.75rem; color: #58a6ff; font-weight: bold;"><i class="fa-solid fa-gear fa-spin"></i> قيد التنفيذ</span>';
        } else if (st === 'completed') {
            statusBadge = '<span style="font-size: 0.75rem; color: #3fb950; font-weight: bold;"><i class="fa-solid fa-circle-check"></i> مكتملة بنجاح</span>';
        } else if (st === 'failed') {
            statusBadge = '<span style="font-size: 0.75rem; color: #da3633; font-weight: bold;"><i class="fa-solid fa-circle-xmark"></i> متعثرة</span>';
        }
        
        const hasOutput = Boolean(t.output);
        const viewOutputBtn = hasOutput 
            ? `<button class="btn btn-sm btn-secondary" onclick="viewTaskOutputModal('${safeId}', '${safeTitle}')" style="margin-top: 10px; width: 100%;"><i class="fa-solid fa-file-lines"></i> عرض مخرجات المهمة</button>`
            : '';
            
        card.innerHTML = `
            <div>
                <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px;">
                    <span style="font-size: 0.72rem; color: var(--accent-blue); font-family: monospace;">${safeId}</span>
                    ${statusBadge}
                </div>
                <h4 style="margin: 0 0 8px 0; color: #fff; font-size: 1.05rem;">${safeTitle}</h4>
                <div style="font-size: 0.82rem; color: #8b949e; margin-bottom: 6px;">
                    <span>الوكيل المكلف:</span> <strong style="color: #c9d1d9;"><i class="fa-solid fa-robot"></i> ${safeAgent}</strong>
                </div>
                <div style="font-size: 0.8rem; color: #8b949e; margin-bottom: 8px;">
                    <span>القسم:</span> <span style="background: rgba(88,166,255,0.1); color: var(--accent-blue); padding: 2px 7px; border-radius: 4px;">${safeDiv}</span>
                </div>
                <div style="font-size: 0.78rem; color: #8b949e; margin-top: 6px;">
                    <span style="display: block; margin-bottom: 4px;">المهام التي تعتمد عليها:</span>
                    <div style="display: flex; gap: 4px; flex-wrap: wrap;">${depsHtml}</div>
                </div>
            </div>
            <div>
                ${viewOutputBtn}
            </div>
        `;
        container.appendChild(card);
    });
}

function renderHandoffs(handoffs) {
    const container = document.getElementById('orch-handoffs-container');
    if (!container) return;
    container.innerHTML = '';
    
    if (!handoffs || handoffs.length === 0) {
        container.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 40px;"><i class="fa-solid fa-handshake" style="font-size: 2rem; margin-bottom: 10px; display: block; opacity: 0.5;"></i>لم يتم تسجيل تسليم مخرجات بين الوكلاء في هذه الجلسة بعد.</div>';
        return;
    }
    
    handoffs.forEach(h => {
        const item = document.createElement('div');
        item.className = 'handoff-card';
        
        const safeFrom = escapeHtml(h.from_agent);
        const safeTo = escapeHtml(h.to_agent);
        const safeContext = escapeHtml(h.context_summary || 'تسليم مخرجات مرحلية');
        const safeTime = escapeHtml(h.timestamp || '');
        const safeStatus = escapeHtml(h.status || 'delivered');
        
        item.innerHTML = `
            <div style="display: flex; align-items: center; gap: 15px; flex-wrap: wrap;">
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span style="background: rgba(88,166,255,0.15); color: #58a6ff; padding: 4px 10px; border-radius: 6px; font-weight: bold; font-size: 0.85rem;">
                        <i class="fa-solid fa-robot"></i> ${safeFrom}
                    </span>
                    <i class="fa-solid fa-arrow-left" style="color: #7ee787;"></i>
                    <span style="background: rgba(46,160,67,0.15); color: #3fb950; padding: 4px 10px; border-radius: 6px; font-weight: bold; font-size: 0.85rem;">
                        <i class="fa-solid fa-robot"></i> ${safeTo}
                    </span>
                </div>
                <div style="color: #c9d1d9; font-size: 0.9rem;">
                    ${safeContext}
                </div>
            </div>
            <div style="display: flex; align-items: center; gap: 10px;">
                <span style="font-size: 0.78rem; color: #8b949e;">${safeTime}</span>
                <span class="badge" style="background: rgba(46,160,67,0.2); color: #3fb950; font-size: 0.75rem;">${safeStatus}</span>
            </div>
        `;
        container.appendChild(item);
    });
}

function renderMemory(memories) {
    const container = document.getElementById('orch-memory-container');
    if (!container) return;
    container.innerHTML = '';
    
    if (!memories || memories.length === 0) {
        container.innerHTML = '<div style="grid-column: 1 / -1; text-align: center; color: var(--text-muted); padding: 40px;"><i class="fa-solid fa-brain" style="font-size: 2rem; margin-bottom: 10px; display: block; opacity: 0.5;"></i>لا توجد حقائق أو قرارات مسجلة في ذاكرة هذا المشروع بعد.</div>';
        return;
    }
    
    memories.forEach(m => {
        const card = document.createElement('div');
        const cat = m.category || 'fact';
        card.className = `memory-card cat-${cat}`;
        
        const safeKey = escapeHtml(m.key);
        const safeAuthor = escapeHtml(m.author_agent || 'system');
        const safeTime = escapeHtml(m.timestamp || '');
        const safeContent = escapeHtml(m.content || '');
        const isVerified = Boolean(m.is_verified);
        
        const catLabels = {
            architecture_decision: 'قرار معماري',
            requirement: 'متطلب معتمد',
            qa_finding: 'تدقيق جودة',
            decision: 'توجيه الإدارة',
            fact: 'حقيقة مؤكدة'
        };
        const catLabel = catLabels[cat] || cat;
        
        card.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <span style="font-size: 0.75rem; background: rgba(255,255,255,0.06); padding: 2px 8px; border-radius: 4px; color: #c9d1d9;">${catLabel}</span>
                ${isVerified ? '<span style="font-size: 0.75rem; color: #3fb950;"><i class="fa-solid fa-certificate"></i> موثق</span>' : ''}
            </div>
            <h4 style="margin: 0; color: #fff; font-size: 1rem;">${safeKey}</h4>
            <p style="margin: 0; font-size: 0.85rem; color: #8b949e; line-height: 1.5; max-height: 120px; overflow-y: auto; white-space: pre-wrap;">${safeContent}</p>
            <div style="display: flex; justify-content: space-between; align-items: center; font-size: 0.75rem; color: #8b949e; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 8px; margin-top: 4px;">
                <span>المصدر: <strong style="color: #58a6ff;">${safeAuthor}</strong></span>
                <span>${safeTime}</span>
            </div>
        `;
        container.appendChild(card);
    });
}

function filterMemoryCategory(category, btnElement) {
    const buttons = document.querySelectorAll('.mem-filter-btn');
    buttons.forEach(b => b.classList.remove('active'));
    if (btnElement) btnElement.classList.add('active');
    
    if (category === 'all') {
        renderMemory(currentProjectMemories);
    } else {
        const filtered = currentProjectMemories.filter(m => m.category === category);
        renderMemory(filtered);
    }
}

async function loadSessionsHistory() {
    const container = document.getElementById('orch-sessions-history-container');
    if (!container) return;
    try {
        const res = await fetch('/api/sessions');
        if (!res.ok) return;
        const data = await res.json();
        const sessions = data.sessions || [];
        
        container.innerHTML = '';
        if (sessions.length === 0) {
            container.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 30px;">لا توجد جلسات مسجلة في قاعدة البيانات بعد.</div>';
            return;
        }
        
        sessions.forEach(s => {
            const row = document.createElement('div');
            row.style.cssText = 'background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.06); padding: 14px 18px; border-radius: 10px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;';
            
            const safeId = escapeHtml(s.id);
            const safeProject = escapeHtml(s.project_id);
            const safeStatus = escapeHtml(s.status);
            const safeCp = escapeHtml(s.checkpoint_step || '--');
            const safeCreated = escapeHtml(s.created_at || '');
            
            row.innerHTML = `
                <div>
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <code style="color: var(--accent-blue); font-size: 0.85rem;">${safeId}</code>
                        <strong style="color: #fff; font-size: 0.95rem;">${safeProject}</strong>
                        <span class="badge" style="background: rgba(255,255,255,0.1); color: #c9d1d9; font-size: 0.75rem;">${safeStatus}</span>
                    </div>
                    <div style="font-size: 0.8rem; color: #8b949e; margin-top: 4px;">
                        <span>نقطة الحفظ: <strong style="color: #7ee787;">${safeCp}</strong></span> &bull;
                        <span>التاريخ: ${safeCreated}</span>
                    </div>
                </div>
                <div>
                    ${(safeStatus === 'approval_needed' || safeStatus === 'interrupted') 
                        ? `<button class="btn btn-sm btn-primary" onclick="resumeSpecificSession('${safeId}')"><i class="fa-solid fa-play"></i> استئناف</button>`
                        : ''}
                </div>
            `;
            container.appendChild(row);
        });
    } catch (e) {
        console.error("Error loading sessions history:", e);
    }
}

function switchOrchTab(tabId, btnElement) {
    const tabs = document.querySelectorAll('.orch-tab-content');
    tabs.forEach(t => t.style.display = 'none');
    
    const target = document.getElementById('orch-tab-' + tabId);
    if (target) target.style.display = 'block';
    
    const buttons = document.querySelectorAll('.orch-tab-btn');
    buttons.forEach(b => b.classList.remove('active'));
    if (btnElement) btnElement.classList.add('active');

    if (tabId === 'gates') {
        loadQualityGatesData();
    } else if (tabId === 'workforce') {
        loadWorkforceIntelligenceData();
    }
}

async function resumeCurrentSession() {
    if (!currentOrchSessionId) return;
    resumeSpecificSession(currentOrchSessionId);
}

async function resumeSpecificSession(sessionId) {
    if (!confirm('هل تريد استئناف هذه الجلسة من آخر نقطة حفظ تم التحقق منها؟')) return;
    try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/resume`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        const result = await res.json();
        if (res.ok) {
            alert('تم استئناف الجلسة بنجاح!');
            fetchState();
            loadOrchestrationData();
        } else {
            alert('تعذر استئناف الجلسة: ' + (result.error || 'خطأ غير معروف'));
        }
    } catch (e) {
        alert('فشل الاتصال بالخادم: ' + e);
    }
}

async function cancelCurrentSession() {
    if (!currentOrchSessionId) return;
    if (!confirm('هل أنت متأكد من رغبتك في إلغاء هذه الجلسة الجارية؟')) return;
    try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(currentOrchSessionId)}/cancel`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        const result = await res.json();
        if (res.ok) {
            alert('تم إلغاء الجلسة.');
            fetchState();
            loadOrchestrationData();
        } else {
            alert('تعذر إلغاء الجلسة: ' + (result.error || 'خطأ غير معروف'));
        }
    } catch (e) {
        alert('فشل الاتصال بالخادم: ' + e);
    }
}

function viewTaskOutputModal(taskId, title) {
    fetch('/api/state')
        .then(res => res.json())
        .then(state => {
            const tasks = (state.persistence && state.persistence.tasks) || [];
            const task = tasks.find(t => t.id === taskId);
            const content = task ? (task.output || 'لا توجد مخرجات مسجلة لهذه المهمة بعد.') : 'المهمة غير موجودة.';
            
            const titleEl = document.getElementById('outputModalTitle');
            const bodyEl = document.getElementById('outputModalBody');
            if (titleEl) titleEl.textContent = `مخرجات: ${title}`;
            if (bodyEl) {
                if (typeof marked !== 'undefined') {
                    bodyEl.innerHTML = sanitizeHtml(marked.parse(content));
                } else {
                    bodyEl.textContent = content;
                }
            }
            const modal = document.getElementById('outputModal');
            if (modal) modal.style.display = 'flex';
        })
        .catch(err => alert('تعذر تحميل مخرجات المهمة: ' + err));
}

// =========================================================================
// Phase 2.8 Quality Gates, Findings & Workforce Intelligence Handlers
// =========================================================================

async function loadQualityGatesData() {
    const cardEl = document.getElementById('orch-gates-status-card');
    const container = document.getElementById('orch-qa-findings-container');
    if (!cardEl || !container) return;

    if (!currentOrchSessionId) {
        cardEl.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 15px;">لا توجد جلسة نشطة حالياً لتقييم بوابات الإطلاق.</div>';
        container.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 20px;">لا توجد جلسة نشطة.</div>';
        return;
    }

    try {
        const gateRes = await fetch(`/api/sessions/${encodeURIComponent(currentOrchSessionId)}/release_gate`);
        if (gateRes.ok) {
            const gate = await gateRes.json();
            const isAllowed = Boolean(gate.allowed);
            const statusLabel = isAllowed ? 'مصرح بالنشر (Passed)' : 'محظور النشر (Blocked)';
            const statusColor = isAllowed ? '#3fb950' : '#da3633';
            const statusBg = isAllowed ? 'rgba(46, 160, 67, 0.15)' : 'rgba(218, 54, 51, 0.15)';
            const reasonsHtml = (gate.reasons && gate.reasons.length > 0)
                ? `<ul style="margin: 10px 0 0; padding-right: 20px; color: #f85149; font-size: 0.9rem;">${gate.reasons.map(r => `<li>${escapeHtml(r)}</li>`).join('')}</ul>`
                : '<p style="color: #7ee787; margin: 8px 0 0; font-size: 0.9rem;">✅ كافة معايير الإطلاق وفحوصات الجودة البرمجية مستوفاة بالكامل.</p>';

            cardEl.innerHTML = `
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <div style="width: 44px; height: 44px; border-radius: 10px; background: ${statusBg}; display: flex; align-items: center; justify-content: center; color: ${statusColor}; font-size: 1.4rem;">
                            <i class="fa-solid ${isAllowed ? 'fa-shield-check' : 'fa-shield-halved'}"></i>
                        </div>
                        <div>
                            <h3 style="margin: 0; font-size: 1.15rem; color: #fff;">حالة بوابة الإطلاق (Release Gate)</h3>
                            <div style="font-size: 0.85rem; color: #8b949e; margin-top: 3px;">
                                <span>حالة الجودة: <strong style="color: ${gate.qa_status === 'passed' ? '#7ee787' : '#f85149'};">${escapeHtml(gate.qa_status)}</strong></span> &bull;
                                <span>الموافقات: <strong style="color: ${gate.approval_status === 'approved' ? '#7ee787' : '#d29922'};">${escapeHtml(gate.approval_status)}</strong></span> &bull;
                                <span>الموانع الحرجة: <strong style="color: ${gate.blocking_findings_count > 0 ? '#f85149' : '#7ee787'};">${gate.blocking_findings_count || 0}</strong></span>
                            </div>
                        </div>
                    </div>
                    <span class="badge" style="background: ${statusBg}; color: ${statusColor}; font-size: 0.9rem; padding: 6px 14px; border-radius: 20px; font-weight: 700;">
                        ${statusLabel}
                    </span>
                </div>
                <div style="margin-top: 15px; border-top: 1px solid rgba(255,255,255,0.06); padding-top: 12px;">
                    <strong style="font-size: 0.9rem; color: #c9d1d9;">تقرير التدقيق والتحقق:</strong>
                    ${reasonsHtml}
                </div>
            `;
        }

        const findRes = await fetch(`/api/sessions/${encodeURIComponent(currentOrchSessionId)}/qa_findings`);
        if (findRes.ok) {
            const findData = await findRes.json();
            const findings = findData.findings || [];
            container.innerHTML = '';
            if (findings.length === 0) {
                container.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 30px;"><i class="fa-solid fa-circle-check" style="color: #3fb950; font-size: 1.8rem; margin-bottom: 8px; display: block;"></i>لا توجد أي ملاحظات أو أخطاء جودة مفتوحة لهذه الجلسة.</div>';
                return;
            }

            findings.forEach(f => {
                const item = document.createElement('div');
                const sev = f.severity || 'medium';
                const st = f.status || 'open';
                const sevColors = { critical: '#da3633', high: '#f85149', medium: '#d29922', low: '#58a6ff', warning: '#d29922' };
                const sevBgs = { critical: 'rgba(218,54,51,0.15)', high: 'rgba(248,81,73,0.15)', medium: 'rgba(210,153,34,0.15)', low: 'rgba(88,166,255,0.15)', warning: 'rgba(210,153,34,0.15)' };
                const color = sevColors[sev] || '#8b949e';
                const bg = sevBgs[sev] || 'rgba(255,255,255,0.05)';

                item.style.cssText = `background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.08); border-right: 4px solid ${color}; border-radius: 8px; padding: 14px 18px;`;

                item.innerHTML = `
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;">
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <span style="background: ${bg}; color: ${color}; font-size: 0.75rem; font-weight: 700; padding: 2px 8px; border-radius: 4px; text-transform: uppercase;">${escapeHtml(sev)}</span>
                            <span style="background: rgba(255,255,255,0.08); color: #c9d1d9; font-size: 0.75rem; padding: 2px 8px; border-radius: 4px;">${escapeHtml(f.category || 'general')}</span>
                            <code style="color: var(--accent-blue); font-size: 0.8rem;">${escapeHtml(f.artifact_name || '')}</code>
                        </div>
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <span class="badge" style="background: rgba(255,255,255,0.08); color: #fff; font-size: 0.8rem;">${escapeHtml(st)}</span>
                            ${st === 'open' ? `<button class="btn btn-sm btn-secondary" onclick="updateFindingStatusPrompt('${escapeHtml(f.finding_id)}')">تغيير الحالة</button>` : ''}
                        </div>
                    </div>
                    <div style="color: #fff; font-size: 0.9rem; margin-bottom: 6px;">
                        <strong>الدليل (Evidence):</strong> <span style="font-family: monospace; color: #e6edf3;">${escapeHtml(f.evidence)}</span>
                    </div>
                    ${f.remediation_recommendation ? `<div style="color: #8b949e; font-size: 0.85rem; margin-bottom: 6px;"><strong>التوصية:</strong> ${escapeHtml(f.remediation_recommendation)}</div>` : ''}
                    <div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: #6e7681; margin-top: 8px; border-top: 1px solid rgba(255,255,255,0.04); padding-top: 6px;">
                        <span>المسؤول: <strong style="color: #58a6ff;">${escapeHtml(f.responsible_agent_id || 'system')}</strong></span>
                        <span>المعرف: <code>${escapeHtml(f.finding_id)}</code></span>
                    </div>
                `;
                container.appendChild(item);
            });
        }
    } catch (err) {
        console.error("Error loading quality gates data:", err);
    }
}

async function updateFindingStatusPrompt(findingId) {
    const status = prompt("أدخل الحالة الجديدة (remediated / accepted):", "accepted");
    if (!status || !['remediated', 'accepted'].includes(status)) return;
    const note = prompt("ملاحظات / تبرير التغيير:") || "";
    try {
        const res = await fetch(`/api/qa_findings/${encodeURIComponent(findingId)}/status`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: status, note: note, agent_id: 'human_operator' })
        });
        if (res.ok) {
            alert("تم تحديث حالة الفحص بنجاح!");
            loadQualityGatesData();
        } else {
            const data = await res.json();
            alert("فشل التحديث: " + (data.error || 'خطأ غير معروف'));
        }
    } catch (e) {
        alert("فشل الاتصال: " + e);
    }
}

async function loadWorkforceIntelligenceData() {
    const container = document.getElementById('orch-workforce-container');
    if (!container) return;

    try {
        const res = await fetch('/api/workforce/intelligence');
        if (!res.ok) return;
        const data = await res.json();

        const totalAgents = data.total_registered_agents || 0;
        const executedCount = data.executed_agents_count || 0;
        const unexecutedCount = data.unexecuted_agents_count || 0;
        const totalDivs = data.total_divisions || 0;
        const totalCaps = data.total_capabilities || 0;
        const gapsCount = (data.capability_gaps || []).length;

        let divsHtml = (data.divisions || []).map(d => `
            <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 10px; padding: 14px;">
                <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <i class="fa-solid ${escapeHtml(d.icon || 'fa-folder')}" style="color: var(--accent-blue);"></i>
                        <strong style="color: #fff; font-size: 0.95rem;">${escapeHtml(d.name_ar || d.name)}</strong>
                    </div>
                    <span style="background: rgba(88,166,255,0.15); color: #58a6ff; font-size: 0.8rem; padding: 2px 8px; border-radius: 12px; font-weight: bold;">
                        ${d.agents_count} موظف
                    </span>
                </div>
                <div style="font-size: 0.8rem; color: #8b949e;">
                    <div>المجالات: <strong>${d.capabilities_count} قدرة معتمدة</strong></div>
                    <div>الأقسام الفرعية: ${d.subdepartments && d.subdepartments.length > 0 ? escapeHtml(d.subdepartments.join('، ')) : 'عام'}</div>
                </div>
            </div>
        `).join('');

        let gapsHtml = gapsCount === 0
            ? '<span style="color: #7ee787;"><i class="fa-solid fa-check"></i> تغطية كاملة لكافة القدرات المعيارية.</span>'
            : `<span style="color: #f85149;"><i class="fa-solid fa-triangle-exclamation"></i> قدرات غير مغطاة: ${(data.capability_gaps || []).map(g => `<code style="background: rgba(255,255,255,0.08); padding: 1px 6px; border-radius: 4px; margin-left: 4px;">${escapeHtml(g)}</code>`).join(' ')}</span>`;

        container.innerHTML = `
            <div class="stats-grid" style="margin-bottom: 25px;">
                <div class="stat-card">
                    <div class="icon-wrapper blue"><i class="fa-solid fa-users-gear"></i></div>
                    <div class="stat-info">
                        <h3>${totalAgents}</h3>
                        <p>وكيل معتمد بالكتالوج (282)</p>
                    </div>
                </div>
                <div class="stat-card">
                    <div class="icon-wrapper green"><i class="fa-solid fa-chart-line"></i></div>
                    <div class="stat-info">
                        <h3>${executedCount}</h3>
                        <p>وكلاء لديهم تنفيذ موثق في DB</p>
                    </div>
                </div>
                <div class="stat-card">
                    <div class="icon-wrapper yellow"><i class="fa-solid fa-clock"></i></div>
                    <div class="stat-info">
                        <h3>${unexecutedCount}</h3>
                        <p>وكلاء مهيأون بانتظار مهام</p>
                    </div>
                </div>
                <div class="stat-card">
                    <div class="icon-wrapper purple" style="color: #d2a8ff; background: rgba(138,43,226,0.1);"><i class="fa-solid fa-sitemap"></i></div>
                    <div class="stat-info">
                        <h3>${totalDivs}</h3>
                        <p>أقسام معيارية (Canonical)</p>
                    </div>
                </div>
            </div>

            <div class="glass-panel" style="margin-bottom: 20px; padding: 18px; border-radius: 10px;">
                <h3 style="margin: 0 0 12px;"><i class="fa-solid fa-layer-group"></i> تغطية القدرات التخصصية (Capability Coverage)</h3>
                <div style="font-size: 0.9rem; color: #c9d1d9; margin-bottom: 10px;">
                    إجمالي القدرات المعتمدة: <strong style="color: #58a6ff;">${totalCaps} قدرة فريدة</strong>
                </div>
                <div style="margin-bottom: 12px; display: flex; flex-wrap: wrap; gap: 6px;">
                    ${(data.capabilities_sample || []).map(c => `<span style="background: rgba(255,255,255,0.06); padding: 3px 8px; border-radius: 6px; font-size: 0.8rem; color: #a5d6ff;">#${escapeHtml(c)}</span>`).join('')}
                </div>
                <div style="border-top: 1px solid rgba(255,255,255,0.06); padding-top: 10px; font-size: 0.85rem;">
                    ${gapsHtml}
                </div>
            </div>

            <div class="glass-panel" style="padding: 18px; border-radius: 10px;">
                <h3 style="margin: 0 0 15px;"><i class="fa-solid fa-building-user"></i> توزيع الوكلاء عبر الأقسام الـ 18</h3>
                <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px;">
                    ${divsHtml}
                </div>
            </div>
        `;
    } catch (e) {
        console.error("Error loading workforce intelligence:", e);
    }
}

