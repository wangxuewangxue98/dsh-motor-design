/* ============================================================
   Motor-AI Web UI 前端逻辑
   ============================================================ */

// 全局状态
const state = {
    rawText: '',
    specs: null,
    solveResults: [],
    topSolutions: [],
    review: '',
    analysis: '',
    desensitized: '',
    report: '',
    reportPath: '',
    testCases: [],
    sortColumn: -1,
    sortAsc: true,
};

// v2.03.01: 全局 fetch 拦截 — 自动附加 JWT token (如果 localStorage 中有)
// 这样所有 apiFetch 调用都无需手动添加 Authorization header
const _originalFetch = window.fetch;
window.fetch = async function(...args) {
    let [url, options] = args;
    options = options || {};
    const token = localStorage.getItem('motorai_token');
    if (token) {
        if (!options.headers) {
            options.headers = {};
        }
        if (options.headers instanceof Headers) {
            if (!options.headers.has('Authorization')) {
                options.headers.set('Authorization', 'Bearer ' + token);
            }
        } else if (typeof options.headers === 'object') {
            const headers = options.headers;
            if (!headers.Authorization && !headers.authorization) {
                headers.Authorization = 'Bearer ' + token;
            }
        }
    }
    return _originalFetch(url, options);
};

// ============================================================
// 初始化
// ============================================================
function updateGpuHint() {
    const modelInput = document.getElementById('setModelName');
    const gpuCheckbox = document.getElementById('setUseGpu');
    const hint = document.getElementById('gpuHint');
    if (!modelInput || !gpuCheckbox || !hint) return;

    const val = modelInput.value.toLowerCase();
    if (val.includes('7b') || val.includes('14b')) {
        gpuCheckbox.checked = false;
        gpuCheckbox.disabled = true;
        hint.textContent = '7B+ 模型建议关闭（4GB 显存不够）';
    } else if (val.includes('4b')) {
        gpuCheckbox.disabled = false;
        hint.textContent = '4B 模型 GPU 可用但偏紧（3.4GB 显存）';
    } else if (val.includes('3b') || val.includes('1b')) {
        gpuCheckbox.disabled = false;
        hint.textContent = '3B 模型推荐开启 GPU（2.5GB 显存）';
    } else {
        gpuCheckbox.disabled = false;
        hint.textContent = '根据模型大小决定是否开启 GPU';
    }
}

document.addEventListener('DOMContentLoaded', () => {
    // 侧边栏导航
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            switchTab(item.dataset.tab);
        });
    });

    // GPU 提示
    const modelInput = document.getElementById('setModelName');
    if (modelInput) {
        modelInput.addEventListener('input', updateGpuHint);
        modelInput.addEventListener('change', updateGpuHint);
    }

    // 加载初始数据
    loadStatus();
    loadTestCases();
    loadSettings();
    loadNetworkStatus();

    // v1.03.02: 连接进度广播 (跨设备同步)
    connectProgressHub();
});

// ============================================================
// Tab 切换
// ============================================================
function switchTab(tabName) {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.classList.toggle('active', item.dataset.tab === tabName);
    });
    document.querySelectorAll('.tab-content').forEach(section => {
        section.classList.toggle('active', section.id === 'tab-' + tabName);
    });

    // 按需加载数据
    if (tabName === 'dashboard') loadStatus();
    if (tabName === 'history') loadHistory();
    if (tabName === 'compare') loadTopSolutions();
    if (tabName === 'report') loadReports();
    if (tabName === 'evolution') loadEvolutionDashboard();
    if (tabName === 'audit') loadAuditLogs();
    if (tabName === 'radar') loadRadar();
    if (tabName === 'sandbox') loadSandboxCandidates();
    if (tabName === 'tasks') loadTasks();
    if (tabName === 'points') loadPointsPanel();
    if (tabName === 'admin-users') loadAdminUsers();
}

// ============================================================
// 系统状态
// ============================================================
async function loadStatus() {
    try {
        const resp = await fetch('/api/status');
        const data = await resp.json();

        // v1.03.01: 环境标识标签
        const envBadge = document.getElementById('envBadge');
        if (envBadge && data.env) {
            envBadge.textContent = data.env.env_label;
            envBadge.style.background = data.env.env_color;
            envBadge.title = `${data.env.env_desc} | ${data.env.version_label} | DB: ${data.env.db_path ? data.env.db_path.split('\\').pop() : ''}`;
        }

        // v2.01.01: 版本号按环境动态显示
        if (data.env) {
            const vl = data.env.version_label || '';
            const vd = (data.env.version_date || '').split(' ')[0];
            const subtitle = document.getElementById('sysSubtitle');
            if (subtitle) subtitle.textContent = `电机设计AI辅助系统 v${vl}`;
            const versionText = document.getElementById('versionText');
            if (versionText) versionText.textContent = `v${vl} | ${vd}`;
            const dashboardDesc = document.getElementById('dashboardDesc');
            if (dashboardDesc) dashboardDesc.textContent = `电机设计AI辅助系统 v${vl} (${vd}) — 全局任务队列 + 跨设备同步 + 辩证制衡 + 全本地零上云`;
            const isDevEnv = data.env.env === 'dev';
            document.title = `Motor-AI ${isDevEnv ? '[DEV]' : '[TRIAL]'} 电机设计AI辅助系统`;
        }

        // v1.03.03: 调试版专属 UI
        const isDev = data.env && data.env.env === 'dev';
        const devTitleBadge = document.getElementById('devTitleBadge');
        if (devTitleBadge) {
            devTitleBadge.style.display = isDev ? 'inline-block' : 'none';
        }
        // 任务调度面板仅调试版可见
        const navTasks = document.getElementById('navTasks');
        if (navTasks) {
            navTasks.style.display = isDev ? '' : 'none';
        }
        // 调试版顶部渐变条
        if (isDev && !document.getElementById('devTopBar')) {
            const bar = document.createElement('div');
            bar.id = 'devTopBar';
            bar.className = 'dev-top-bar';
            document.querySelector('.main').insertBefore(bar, document.querySelector('.main').firstChild);
        }

        // v2.02.01: 用户信息显示 + 角色权限
        if (data.auth) {
            const user = data.auth.user;
            const userCard = document.getElementById('userCard');
            if (user && userCard) {
                userCard.style.display = 'flex';
                document.getElementById('userAvatar').textContent = (user.display_name || user.username || '?').charAt(0).toUpperCase();
                document.getElementById('userName').textContent = user.display_name || user.username;
                const roleEl = document.getElementById('userRole');
                if (roleEl) {
                    roleEl.textContent = ({admin:'管理员', user:'用户', vip:'VIP', flagship:'旗舰', guest:'访客'})[user.role] || user.role;
                    roleEl.className = 'user-role ' + (user.role || 'guest');
                }
                document.getElementById('userPoints').textContent = (user.points_balance || 0) + ' 积分';
                // 已登录: 隐藏侧边栏登录按钮
                const loginBtnSidebar = document.getElementById('loginBtnSidebar');
                if (loginBtnSidebar) loginBtnSidebar.style.display = 'none';
                // 登出按钮: dev和trial都显示 (dev登出后可恢复自动登录)
                const logoutBtn = document.getElementById('logoutBtn');
                if (logoutBtn) {
                    logoutBtn.style.display = '';
                }
                // dev 恢复自动登录按钮: 仅在 dev 自动登录时隐藏
                const devRestoreBtn = document.getElementById('devRestoreBtn');
                if (devRestoreBtn) {
                    devRestoreBtn.style.display = (data.auth.auto_login) ? 'none' : (isDev ? '' : 'none');
                }
                // 管理员面板可见性
                const navAdminUsers = document.getElementById('navAdminUsers');
                if (navAdminUsers) {
                    navAdminUsers.style.display = (user.role === 'admin') ? '' : 'none';
                }
                // 全局保存当前用户
                state.currentUser = user;
            } else if (userCard) {
                // 未登录状态: 隐藏用户卡, 显示登录入口
                userCard.style.display = 'none';
                state.currentUser = null;
                // 显示侧边栏登录按钮
                const loginBtnSidebar = document.getElementById('loginBtnSidebar');
                if (loginBtnSidebar) loginBtnSidebar.style.display = '';
                // dev 环境: 显示恢复自动登录按钮
                const devRestoreBtn = document.getElementById('devRestoreBtn');
                if (devRestoreBtn) {
                    devRestoreBtn.style.display = isDev ? '' : 'none';
                }
                // 隐藏管理员面板
                const navAdminUsers = document.getElementById('navAdminUsers');
                if (navAdminUsers) navAdminUsers.style.display = 'none';
            }
            // admin 凭据提示 (dev 或 trial 首次运行)
            const adminHint = document.getElementById('adminHint');
            if (adminHint) {
                adminHint.style.display = data.auth.admin_hint ? '' : 'none';
            }
        }

        // Ollama
        const ollamaEl = document.getElementById('ollamaStatus');
        const ollamaDetail = document.getElementById('ollamaDetail');
        const sidebarStatus = document.getElementById('sidebarStatus');
        const sidebarText = document.getElementById('sidebarStatusText');

        if (data.ollama.running) {
            ollamaEl.textContent = '运行中';
            ollamaEl.style.color = 'var(--success)';
            ollamaDetail.textContent = `${data.ollama.model} | ${data.ollama.gpu_mode ? 'GPU' : 'CPU'} 模式`;
            sidebarStatus.querySelector('.status-dot').className = 'status-dot status-dot--ok';
            sidebarText.textContent = 'AI 引擎运行中';
        } else {
            ollamaEl.textContent = '未运行';
            ollamaEl.style.color = 'var(--error)';
            ollamaDetail.textContent = '请启动 Ollama 服务';
            sidebarStatus.querySelector('.status-dot').className = 'status-dot status-dot--error';
            sidebarText.textContent = 'AI 引擎未运行';
        }

        // 云端
        const cloudEl = document.getElementById('cloudStatus');
        const cloudDetail = document.getElementById('cloudDetail');
        if (data.cloud.allowed === false) {
            cloudEl.textContent = '已禁用';
            cloudEl.style.color = 'var(--text-tertiary)';
            cloudDetail.textContent = '试用版安全限制：云端 API 已关闭';
        } else if (data.cloud.configured) {
            cloudEl.textContent = '已配置';
            cloudEl.style.color = 'var(--success)';
            cloudDetail.textContent = data.cloud.model;
        } else {
            cloudEl.textContent = '未配置';
            cloudEl.style.color = 'var(--warning)';
            cloudDetail.textContent = '在设置中配置 DeepSeek API Key';
        }

        // 数据库
        const dbEl = document.getElementById('dbStatus');
        const dbDetail = document.getElementById('dbDetail');
        dbEl.textContent = `${data.db.total} 组`;
        dbDetail.textContent = data.db.total > 0
            ? `最高效率 ${data.db.max_efficiency}% | 平均 ${data.db.avg_efficiency}%`
            : '暂无数据';

        // Motor-CAD
        const motorEl = document.getElementById('motorStatus');
        const motorDetail = document.getElementById('motorDetail');
        if (data.motorcad.is_demo) {
            motorEl.textContent = 'Demo 版';
            motorEl.style.color = 'var(--error)';
            motorDetail.textContent = `v${data.motorcad.version || '11.1.5'} | COM 自动化被禁用`;
        } else if (data.motorcad.available) {
            motorEl.textContent = '已连接';
            motorEl.style.color = 'var(--success)';
            motorDetail.textContent = `v${data.motorcad.version || '11.1.5'} | COM 已连接`;
        } else if (data.motorcad.installed) {
            motorEl.textContent = '已安装';
            motorEl.style.color = 'var(--warning)';
            motorDetail.textContent = `v${data.motorcad.version || '11.1.5'} | COM 未连接 (启动较慢，约 1 分钟)`;
        } else {
            motorEl.textContent = '未安装';
            motorEl.style.color = 'var(--error)';
            motorDetail.textContent = '模拟模式';
        }

        // 更新全局 Motor-CAD 提示/手动导入面板
        updateMotorcadNotice(data.motorcad);
    } catch (e) {
        console.error('loadStatus error:', e);
    }
}


function updateMotorcadNotice(motorcad) {
    const notice = document.getElementById('solveMotorcadNotice');
    const noticeText = document.getElementById('solveMotorcadNoticeText');
    const workflowPanel = document.getElementById('manualWorkflowPanel');
    const demoNotice = document.getElementById('motorcadDemoNotice');

    if (!notice) return;

    // COM 不可用时始终显示手动工作流面板
    const comUnavailable = !motorcad.available || motorcad.is_demo || motorcad.com_skip;

    if (motorcad.is_demo) {
        notice.style.display = 'block';
        noticeText.textContent = '检测到 Motor-CAD Demo 版本，COM 自动化被禁用。请使用手动求解工作流：导出参数 → Motor-CAD GUI 求解 → 导入结果。';
        if (workflowPanel) workflowPanel.style.display = 'block';
        if (demoNotice) demoNotice.style.display = 'block';
    } else if (comUnavailable || motorcad.use_mode === 'manual') {
        notice.style.display = 'block';
        const reason = motorcad.last_fallback_reason || motorcad.licence_note || 'COM 自动化不可用 (32/64位兼容性问题)';
        noticeText.textContent = `Motor-CAD COM 自动化不可用。原因：${reason}。请使用手动求解工作流：导出参数 → Motor-CAD GUI 求解 → 导入结果。`;
        if (workflowPanel) workflowPanel.style.display = 'block';
        if (demoNotice) demoNotice.style.display = 'none';
    } else {
        notice.style.display = 'none';
        if (workflowPanel) workflowPanel.style.display = 'none';
        if (demoNotice) demoNotice.style.display = 'none';
    }
}

// ============================================================
// 测试用例
// ============================================================
async function loadTestCases() {
    try {
        const resp = await fetch('/api/test-cases');
        const data = await resp.json();
        state.testCases = data.cases || [];
    } catch (e) {
        console.error('loadTestCases error:', e);
    }
}

function loadTestCase(index) {
    if (state.testCases[index]) {
        document.getElementById('parseInput').value = state.testCases[index];
    }
}

// ============================================================
// 需求解析
// ============================================================
async function parseRequirements() {
    const text = document.getElementById('parseInput').value.trim();
    if (!text) {
        showToast('请输入客户需求描述', 'warning');
        return;
    }

    state.rawText = text;
    const btn = document.getElementById('parseBtn');
    btn.disabled = true;
    btn.textContent = 'AI 解析中...';

    try {
        const resp = await fetch('/api/parse', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
        });
        const data = await resp.json();

        if (data.error) {
            showToast('解析失败: ' + data.error, 'error');
            return;
        }

        state.specs = data.specs;
        renderSpecsForm(data.specs);
        document.getElementById('parseNextBtn').style.display = 'inline-flex';
        showToast('解析成功！请确认参数后进入下一步', 'success');
    } catch (e) {
        showToast('解析失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'AI 解析需求';
    }
}

function renderSpecsForm(specs) {
    const form = document.getElementById('specsForm');
    const fields = [
        { key: 'power_kw', label: '额定功率 (kW)', type: 'number' },
        { key: 'voltage_v', label: '电压 (V)', type: 'number' },
        { key: 'speed_rpm', label: '转速 (rpm)', type: 'number' },
        { key: 'cooling', label: '冷却方式', type: 'select', options: ['自然风冷', '水冷', '油冷', '未知'] },
        { key: 'efficiency_target', label: '效率目标 (%)', type: 'number' },
        { key: 'stator_od_limit', label: '外径限制 (mm)', type: 'number' },
    ];

    let html = '';
    fields.forEach(f => {
        const val = specs[f.key];
        const displayVal = (val === null || val === undefined) ? '' : val;
        if (f.type === 'select') {
            const opts = f.options.map(o =>
                `<option value="${o}" ${o === displayVal ? 'selected' : ''}>${o}</option>`
            ).join('');
            html += `
                <div class="spec-field">
                    <label>${f.label}</label>
                    <select data-key="${f.key}">${opts}</select>
                </div>`;
        } else {
            html += `
                <div class="spec-field">
                    <label>${f.label}</label>
                    <input type="${f.type}" data-key="${f.key}" value="${displayVal}" placeholder="未指定">
                </div>`;
        }
    });

    form.innerHTML = html;
}

function getSpecsFromForm() {
    const specs = {};
    document.querySelectorAll('#specsForm [data-key]').forEach(el => {
        const key = el.dataset.key;
        const val = el.value.trim();
        if (val === '') {
            specs[key] = null;
        } else if (el.type === 'number') {
            specs[key] = parseFloat(val);
        } else {
            specs[key] = val;
        }
    });
    return specs;
}

function goToSolve() {
    state.specs = getSpecsFromForm();
    renderSolveSpecsBar();
    switchTab('solve');
}

function renderSolveSpecsBar() {
    const bar = document.getElementById('solveSpecsBar');
    if (!state.specs) {
        bar.innerHTML = '<span class="empty-hint">请先完成需求解析</span>';
        return;
    }

    const s = state.specs;
    const tags = [];
    if (s.power_kw) tags.push(`<span class="spec-tag">功率: ${s.power_kw} kW</span>`);
    if (s.voltage_v) tags.push(`<span class="spec-tag">电压: ${s.voltage_v} V</span>`);
    if (s.speed_rpm) tags.push(`<span class="spec-tag">转速: ${s.speed_rpm} rpm</span>`);
    if (s.cooling) tags.push(`<span class="spec-tag">冷却: ${s.cooling}</span>`);
    if (s.efficiency_target) tags.push(`<span class="spec-tag">效率目标: ${s.efficiency_target}%</span>`);
    if (s.stator_od_limit) tags.push(`<span class="spec-tag">外径限制: ${s.stator_od_limit} mm</span>`);

    bar.innerHTML = `<strong>当前需求规格:</strong> ${tags.join('')}`;
}

// ============================================================
// 批量求解
// ============================================================
async function previewParams() {
    if (!state.specs) {
        showToast('请先完成需求解析', 'warning');
        return;
    }

    const count = parseInt(document.getElementById('solveCount').value);
    showLoading('生成参数矩阵...');

    try {
        const resp = await fetch('/api/preview-params', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ specs: state.specs, count }),
        });
        const data = await resp.json();

        if (data.error) {
            showToast('预览失败: ' + data.error, 'error');
            return;
        }

        renderPreviewTable(data.params);
        document.getElementById('previewSection').style.display = 'block';
        showToast(`已生成 ${data.count} 组参数`, 'success');
    } catch (e) {
        showToast('预览失败: ' + e.message, 'error');
    } finally {
        hideLoading();
    }
}

function renderPreviewTable(params) {
    const table = document.getElementById('previewTable');
    table.innerHTML = `
        <thead>
            <tr>
                <th>#</th>
                <th>外径 (mm)</th>
                <th>极数</th>
                <th>电压 (V)</th>
                <th>转速 (rpm)</th>
            </tr>
        </thead>
        <tbody>
            ${params.map((p, i) => `
                <tr>
                    <td>${i + 1}</td>
                    <td>${p.stator_od}</td>
                    <td>${p.poles}</td>
                    <td>${p.voltage}</td>
                    <td>${p.speed}</td>
                </tr>
            `).join('')}
        </tbody>
    `;
}

async function startSolve() {
    if (!state.specs) {
        showToast('请先完成需求解析', 'warning');
        return;
    }

    const count = parseInt(document.getElementById('solveCount').value);
    const btn = document.getElementById('solveStartBtn');
    btn.disabled = true;
    btn.textContent = '求解中...';
    _myWorkflowActive = true;  // v1.03.02: 标记自己发起的求解

    // 显示进度区域
    const progressEl = document.getElementById('solveProgress');
    const resultsEl = document.getElementById('solveResultsSection');
    progressEl.style.display = 'block';
    resultsEl.style.display = 'none';
    document.getElementById('solveLog').innerHTML = '';
    updateProgress(0, count, '开始求解...');

    state.solveResults = [];

    try {
        const resp = await fetch('/api/solve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                specs: state.specs,
                count: count,
                raw_text: state.rawText,
            }),
        });

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    try {
                        const data = JSON.parse(line.slice(6));
                        handleSolveEvent(data, count);
                    } catch (e) {
                        // 忽略解析错误
                    }
                }
            }
        }
    } catch (e) {
        showToast('求解失败: ' + e.message, 'error');
        appendLog('求解失败: ' + e.message, 'err');
    } finally {
        _myWorkflowActive = false;  // v1.03.02: 求解结束
        btn.disabled = false;
        btn.textContent = '开始求解';
        hideRemoteBanner();
    }
}

let _connectingTimer = null;
let _connectingStart = 0;

function handleSolveEvent(data, totalCount) {
    if (data.type === 'connecting') {
        appendLog('⏳ ' + (data.message || '正在连接 Motor-CAD...'), 'info');
        updateProgress(0, totalCount, '连接 Motor-CAD 中 (约 1-2 分钟)...');

        // 启动连接计时器, 每 5 秒更新进度文字
        _connectingStart = Date.now();
        if (_connectingTimer) clearInterval(_connectingTimer);
        _connectingTimer = setInterval(() => {
            const elapsed = Math.floor((Date.now() - _connectingStart) / 1000);
            updateProgress(0, totalCount, `连接 Motor-CAD 中... ${elapsed}s (Motor-CAD 正在启动, 请耐心等待)`);
        }, 5000);

    } else if (data.type === 'start') {
        // 清除连接计时器
        if (_connectingTimer) {
            clearInterval(_connectingTimer);
            _connectingTimer = null;
            const elapsed = Math.floor((Date.now() - _connectingStart) / 1000);
            if (elapsed > 5) {
                appendLog(`✓ Motor-CAD 连接成功 (${elapsed}s)`, 'ok');
            }
        }
        appendLog(`开始批量求解，共 ${data.total} 组参数`, 'info');
        if (data.motorcad_available === false && data.warning) {
            appendLog('⚠️ ' + data.warning, 'warn');
        }
        updateProgress(0, data.total, '求解中...');
    } else if (data.type === 'progress') {
        const r = data.result;
        const p = r.params;
        const modeTag = r.solve_mode === 'motorcad' ? '[MC] ' : (r.solve_mode === 'pending' ? '[PEND] ' : '[SIM] ');
        appendLog(
            `${modeTag}[${data.index}/${data.total}] 外径=${p.stator_od}mm, 极数=${p.poles}, ` +
            `效率=${r.efficiency}%, 温升=${r.max_temp}K, 转矩=${r.torque}N·m` +
            (r.fallback_reason ? ` (降级原因: ${r.fallback_reason.slice(0, 60)})` : ''),
            r.solve_mode === 'motorcad' ? 'ok' : (r.solve_mode === 'pending' ? 'info' : 'warn')
        );
        state.solveResults.push(r);
        updateProgress(data.index, data.total, `已完成 ${data.index}/${data.total}`);
    } else if (data.type === 'error') {
        appendLog(`[${data.index}/${data.total}] 失败: ${data.error}`, 'err');
        updateProgress(data.index, data.total, `已完成 ${data.index}/${data.total}`);
    } else if (data.type === 'done') {
        if (_connectingTimer) { clearInterval(_connectingTimer); _connectingTimer = null; }
        appendLog(`求解完成！成功 ${data.count}/${data.total} 组`, 'info');

        // 显示求解摘要
        if (data.summary) {
            const s = data.summary;
            if (s.use_mode === 'manual') {
                appendLog(`📋 全部 ${s.simulation_count} 组方案已生成占位数据，请通过手动工作流导入 Motor-CAD 求解结果`, 'info');
                showSolveWarning('等待手动导入 Motor-CAD 结果', `${s.fallback_reason || 'MOTORCAD_USE_MODE=manual'}。占位数据已生成，请导出参数 CSV → Motor-CAD GUI 求解 → 导入结果 CSV`);
            } else if (s.all_simulation) {
                appendLog(`⚠️ 全部 ${s.simulation_count} 组使用模拟数据 | 原因: ${s.fallback_reason}`, 'warn');
                showSolveWarning('全部方案使用模拟数据', s.fallback_reason);
            } else if (s.simulation_count > 0) {
                appendLog(`⚠️ Motor-CAD: ${s.motorcad_count} 组, 模拟: ${s.simulation_count} 组`, 'warn');
                showSolveWarning('部分方案使用模拟数据', `${s.motorcad_count} 组 Motor-CAD / ${s.simulation_count} 组模拟`);
            } else {
                appendLog(`全部 ${s.motorcad_count} 组由 Motor-CAD 求解`, 'ok');
            }
        }

        updateProgress(data.total, data.total, '全部完成');
        renderSolveResultsTable(state.solveResults);
        document.getElementById('solveResultsSection').style.display = 'block';
        showToast(`求解完成！${data.count} 组成功`, 'success');
    } else if (data.type === 'fatal') {
        if (_connectingTimer) { clearInterval(_connectingTimer); _connectingTimer = null; }
        appendLog(`致命错误: ${data.error}`, 'err');
        showToast('求解失败: ' + data.error, 'error');
    }
}

function showSolveWarning(title, detail) {
    let banner = document.getElementById('solveWarningBanner');
    if (!banner) {
        banner = document.createElement('div');
        banner.id = 'solveWarningBanner';
        banner.className = 'solve-warning-banner';
        const resultsSection = document.getElementById('solveResultsSection');
        resultsSection.parentNode.insertBefore(banner, resultsSection);
    }
    banner.innerHTML = `
        <div class="warning-icon">⚠️</div>
        <div class="warning-content">
            <div class="warning-title">${title}</div>
            <div class="warning-detail">${detail}</div>
            <div class="warning-hint">${title.includes('手动导入') ? '使用上方"手动求解工作流"面板：导出参数 → Motor-CAD 求解 → 导入结果' : '请确保 Motor-CAD 已启动并加载了 .mot 模板文件，COM 自动化才能正常工作'}</div>
        </div>
    `;
    banner.style.display = 'flex';
}

function updateProgress(current, total, text) {
    const pct = total > 0 ? Math.round((current / total) * 100) : 0;
    document.getElementById('progressBarFill').style.width = pct + '%';
    document.getElementById('solveProgressText').textContent = text;
    document.getElementById('solveProgressCount').textContent = `${current} / ${total}`;
}

function appendLog(text, type) {
    const log = document.getElementById('solveLog');
    const line = document.createElement('div');
    line.className = `log-line log-line--${type || 'info'}`;
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

function renderSolveResultsTable(results) {
    const table = document.getElementById('solveResultsTable');
    const sorted = [...results].sort((a, b) => b.efficiency - a.efficiency);
    table.innerHTML = `
        <thead>
            <tr>
                <th>排名</th>
                <th>模式</th>
                <th>外径 (mm)</th>
                <th>内径 (mm)</th>
                <th>转子外径 (mm)</th>
                <th>铁心长 (mm)</th>
                <th>气隙 (mm)</th>
                <th>极数</th>
                <th>定子槽</th>
                <th>转子槽</th>
                <th>匝数</th>
                <th>效率 (%)</th>
                <th>温升 (K)</th>
                <th>转矩 (N·m)</th>
                <th>功率 (kW)</th>
                <th>齿磁密 (T)</th>
                <th>气隙磁密 (T)</th>
                <th>电密 (A/mm²)</th>
                <th>铜耗 (W)</th>
                <th>铁耗 (W)</th>
            </tr>
        </thead>
        <tbody>
            ${sorted.map((r, i) => `
                <tr>
                    <td>${i + 1}</td>
                    <td>${r.solve_mode === 'motorcad'
                        ? '<span class="mode-badge mode-badge--mc">MC</span>'
                        : (r.solve_mode === 'pending'
                            ? '<span class="mode-badge mode-badge--pend">PEND</span>'
                            : '<span class="mode-badge mode-badge--sim">SIM</span>')}</td>
                    <td>${r.params.stator_od}</td>
                    <td>${r.stator_id || r.params.stator_id || '-'}</td>
                    <td>${r.rotor_od || r.params.rotor_od || '-'}</td>
                    <td>${r.core_length || r.params.core_length || '-'}</td>
                    <td>${r.air_gap || r.params.air_gap || '-'}</td>
                    <td>${r.params.poles}</td>
                    <td>${r.slots_stator || r.params.slots_stator || '-'}</td>
                    <td>${r.slots_rotor || r.params.slots_rotor || '-'}</td>
                    <td>${r.turns_per_coil || r.params.turns_per_coil || '-'}</td>
                    <td>${effBadge(r.efficiency)}</td>
                    <td>${r.max_temp}</td>
                    <td>${r.torque}</td>
                    <td>${r.power}</td>
                    <td>${r.stator_tooth_flux_density || '-'}</td>
                    <td>${r.air_gap_flux_density || '-'}</td>
                    <td>${r.current_density || '-'}</td>
                    <td>${r.copper_loss || '-'}</td>
                    <td>${r.iron_loss || '-'}</td>
                </tr>
            `).join('')}
        </tbody>
    `;
}

function goToCompare() {
    switchTab('compare');
    loadTopSolutions();
}

// ============================================================
// 方案对比
// ============================================================
async function loadTopSolutions() {
    const n = parseInt(document.getElementById('compareTopN').value);
    const minEff = parseFloat(document.getElementById('compareMinEff').value) || 0;

    try {
        const resp = await fetch(`/api/results/top?n=${n}&min_eff=${minEff}`);
        const data = await resp.json();

        if (data.error) {
            showToast('加载失败: ' + data.error, 'error');
            return;
        }

        state.topSolutions = data.results || [];
        renderCompareTable(state.topSolutions);
    } catch (e) {
        showToast('加载失败: ' + e.message, 'error');
    }
}

function renderCompareTable(results) {
    const table = document.getElementById('compareTable');
    if (!results || results.length === 0) {
        table.innerHTML = '';
        document.getElementById('compareTableWrap').innerHTML =
            '<div class="empty-hint">暂无数据，请先完成批量求解</div>';
        return;
    }

    table.innerHTML = `
        <thead>
            <tr>
                <th rowspan="2">排名</th>
                <th rowspan="2">模式</th>
                <th colspan="9">结构参数</th>
                <th colspan="4">性能参数</th>
                <th colspan="4">电磁参数</th>
                <th colspan="3">损耗 (W)</th>
                <th rowspan="2">时间</th>
                <th rowspan="2">操作</th>
            </tr>
            <tr>
                <th>外径(mm)</th>
                <th>内径(mm)</th>
                <th>转子外径(mm)</th>
                <th>铁心长(mm)</th>
                <th>气隙(mm)</th>
                <th>极数</th>
                <th>定子槽</th>
                <th>转子槽</th>
                <th>匝数</th>
                <th>效率(%)</th>
                <th>温升(K)</th>
                <th>转矩(N·m)</th>
                <th>功率(kW)</th>
                <th>齿磁密(T)</th>
                <th>气隙磁密(T)</th>
                <th>电密(A/mm²)</th>
                <th>功率因数</th>
                <th>铜耗</th>
                <th>铁耗</th>
                <th>总损耗</th>
            </tr>
        </thead>
        <tbody>
            ${results.map((r, i) => `
                <tr>
                    <td>${i + 1}</td>
                    <td>${r.solve_mode === 'motorcad'
                        ? '<span class="mode-badge mode-badge--mc">MC</span>'
                        : (r.solve_mode === 'pending'
                            ? '<span class="mode-badge mode-badge--pend">PEND</span>'
                            : '<span class="mode-badge mode-badge--sim">SIM</span>')}</td>
                    <td>${r.stator_od || '-'}</td>
                    <td>${r.stator_id || '-'}</td>
                    <td>${r.rotor_od || '-'}</td>
                    <td>${r.core_length || '-'}</td>
                    <td>${r.air_gap || '-'}</td>
                    <td>${r.poles || '-'}</td>
                    <td>${r.slots_stator || '-'}</td>
                    <td>${r.slots_rotor || '-'}</td>
                    <td>${r.turns_per_coil || '-'}</td>
                    <td>${effBadge(r.efficiency)}</td>
                    <td>${r.max_temp || '-'}</td>
                    <td>${r.torque || '-'}</td>
                    <td>${r.power || '-'}</td>
                    <td>${r.stator_tooth_flux_density || '-'}</td>
                    <td>${r.air_gap_flux_density || '-'}</td>
                    <td>${r.current_density || '-'}</td>
                    <td>${r.power_factor || '-'}</td>
                    <td>${r.copper_loss || '-'}</td>
                    <td>${r.iron_loss || '-'}</td>
                    <td>${r.total_loss || '-'}</td>
                    <td>${r.created_time || '-'}</td>
                    <td><button class="btn btn-sm btn-secondary" onclick="selectSolutionForFeedback(${r.id}, ${JSON.stringify(r).replace(/"/g, '&quot;')})">反馈</button></td>
                </tr>
            `).join('')}
        </tbody>
    `;
}

async function doReview() {
    if (state.topSolutions.length === 0) {
        showToast('请先加载方案数据', 'warning');
        return;
    }

    const btn = document.getElementById('reviewBtn');
    btn.disabled = true;
    btn.textContent = 'AI 点评中...';
    document.getElementById('reviewDisplay').innerHTML =
        '<div class="empty-hint">本地 AI 正在分析方案...</div>';

    try {
        const resp = await fetch('/api/review', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ solutions: state.topSolutions.slice(0, 5) }),
        });
        const data = await resp.json();

        if (data.error) {
            showToast('点评失败: ' + data.error, 'error');
            document.getElementById('reviewDisplay').innerHTML =
                '<div class="empty-hint">点评失败: ' + data.error + '</div>';
            return;
        }

        state.review = data.review;
        document.getElementById('reviewDisplay').textContent = data.review;
        showToast('本地点评完成', 'success');
    } catch (e) {
        showToast('点评失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = '生成本地点评';
    }
}

async function doCloudAnalysis() {
    if (state.topSolutions.length === 0) {
        showToast('请先加载方案数据', 'warning');
        return;
    }

    // 构建脱敏对比文本（包含详细参数）
    const lines = state.topSolutions.slice(0, 5).map((s, i) => {
        let line = `方案${i + 1}: 外径${s.stator_od}mm, 极数${s.poles}`;
        if (s.slots_stator) line += `, 定子槽${s.slots_stator}/转子槽${s.slots_rotor || '?'}`;
        if (s.turns_per_coil) line += `, 匝数${s.turns_per_coil}`;
        line += `, 效率${s.efficiency}%, 温升${s.max_temp}K, 转矩${s.torque}N·m`;
        if (s.stator_tooth_flux_density) line += `, 齿磁密${s.stator_tooth_flux_density}T, 气隙磁密${s.air_gap_flux_density}T`;
        if (s.current_density) line += `, 电密${s.current_density}A/mm²`;
        if (s.copper_loss) line += `, 铜耗${s.copper_loss}W, 铁耗${s.iron_loss}W`;
        if (s.thermal_factor) line += `, 发热因数${s.thermal_factor}`;
        return line;
    });
    const comparisonText = lines.join('\n');

    const btn = document.getElementById('cloudBtn');
    btn.disabled = true;
    btn.textContent = '云端分析中...';
    document.getElementById('cloudDisplay').innerHTML =
        '<div class="empty-hint">云端 AI 正在深度分析（仅发送脱敏性能数据）...</div>';

    try {
        const resp = await fetch('/api/cloud-analysis', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: comparisonText }),
        });
        const data = await resp.json();

        if (data.error) {
            showToast('分析失败: ' + data.error, 'error');
            document.getElementById('cloudDisplay').innerHTML =
                '<div class="empty-hint">' + data.error + '</div>';
            return;
        }

        state.analysis = data.analysis;
        state.desensitized = data.desensitized;
        document.getElementById('cloudDisplay').textContent = data.analysis;

        // 脱敏信息
        const info = document.getElementById('desensitizeInfo');
        info.style.display = 'block';
        if (data.was_desensitized) {
            info.className = 'desensitize-info desensitize-info--warn';
            info.textContent = '⚠️ 检测到敏感信息，已自动脱敏后发送';
        } else {
            info.className = 'desensitize-info desensitize-info--ok';
            info.textContent = '✅ 脱敏检查通过，仅含性能指标';
        }

        showToast('云端分析完成', 'success');
    } catch (e) {
        showToast('分析失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = '云端深度分析';
    }
}

function goToReport() {
    switchTab('report');
}

// ============================================================
// 报告中心
// ============================================================
async function generateReport() {
    if (state.topSolutions.length === 0) {
        showToast('请先在"方案对比"中加载方案', 'warning');
        return;
    }

    const btn = document.getElementById('reportGenBtn');
    btn.disabled = true;
    btn.textContent = '生成中...';
    document.getElementById('reportPreview').innerHTML =
        '<div class="empty-hint">正在生成报告，请稍候...</div>';

    try {
        const resp = await fetch('/api/report', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                specs: state.specs || {},
                solutions: state.topSolutions.slice(0, 5),
                review: state.review,
                analysis: state.analysis,
                raw_text: state.rawText,
            }),
        });
        const data = await resp.json();

        if (data.error) {
            showToast('生成失败: ' + data.error, 'error');
            document.getElementById('reportPreview').innerHTML =
                '<div class="empty-hint">生成失败: ' + data.error + '</div>';
            return;
        }

        state.report = data.report;
        state.reportPath = data.path;
        document.getElementById('reportPreview').innerHTML = renderMarkdown(data.report);
        showToast('报告已生成: ' + data.path, 'success');
        loadReports();
    } catch (e) {
        showToast('生成失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = '生成报告';
    }
}

async function loadReports() {
    try {
        const resp = await fetch('/api/reports');
        const data = await resp.json();
        const list = document.getElementById('reportList');

        if (!data.reports || data.reports.length === 0) {
            list.innerHTML = '<div class="empty-hint">暂无报告</div>';
            return;
        }

        list.innerHTML = data.reports.map(r => `
            <div class="report-item" onclick="readReport('${r.path.replace(/\\/g, '\\\\')}')">
                <div class="report-item-name">${r.filename}</div>
                <div class="report-item-meta">${r.modified} | ${formatSize(r.size)}</div>
            </div>
        `).join('');
    } catch (e) {
        console.error('loadReports error:', e);
    }
}

async function readReport(path) {
    try {
        const resp = await fetch(`/api/report/read?path=${encodeURIComponent(path)}`);
        const data = await resp.json();

        if (data.error) {
            showToast('读取失败: ' + data.error, 'error');
            return;
        }

        document.getElementById('reportPreview').innerHTML = renderMarkdown(data.content);
        state.report = data.content;
        state.reportPath = path;
    } catch (e) {
        showToast('读取失败: ' + e.message, 'error');
    }
}

// ============================================================
// 历史记录
// ============================================================
async function loadHistory() {
    try {
        const resp = await fetch('/api/results');
        const data = await resp.json();

        if (data.error) {
            showToast('加载失败: ' + data.error, 'error');
            return;
        }

        // 统计
        const stats = data.stats || {};
        document.getElementById('histTotal').textContent = stats.total || 0;
        document.getElementById('histMaxEff').textContent = stats.max_efficiency ? stats.max_efficiency + '%' : '-';
        document.getElementById('histAvgEff').textContent = stats.avg_efficiency ? stats.avg_efficiency + '%' : '-';

        // 表格
        const table = document.getElementById('historyTable');
        const results = data.results || [];
        if (results.length === 0) {
            table.innerHTML = '';
            document.getElementById('historyTable').parentElement.innerHTML =
                '<div class="empty-hint">暂无历史数据</div>';
            return;
        }

        table.innerHTML = `
            <thead>
                <tr>
                    <th>ID</th>
                    <th>外径 (mm)</th>
                    <th>内径 (mm)</th>
                    <th>转子外径 (mm)</th>
                    <th>铁心长 (mm)</th>
                    <th>极数</th>
                    <th>电压 (V)</th>
                    <th>转速 (rpm)</th>
                    <th>效率 (%)</th>
                    <th>温升 (K)</th>
                    <th>转矩 (N·m)</th>
                    <th>功率 (kW)</th>
                    <th>时间</th>
                </tr>
            </thead>
            <tbody>
                ${results.map(r => `
                    <tr>
                        <td>${r.id}</td>
                        <td>${r.stator_od || '-'}</td>
                        <td>${r.stator_id || '-'}</td>
                        <td>${r.rotor_od || '-'}</td>
                        <td>${r.core_length || '-'}</td>
                        <td>${r.poles || '-'}</td>
                        <td>${r.voltage_v || '-'}</td>
                        <td>${r.speed_rpm || '-'}</td>
                        <td>${effBadge(r.efficiency)}</td>
                        <td>${r.max_temp || '-'}</td>
                        <td>${r.torque || '-'}</td>
                        <td>${r.power || '-'}</td>
                        <td>${r.created_time || '-'}</td>
                    </tr>
                `).join('')}
            </tbody>
        `;
    } catch (e) {
        showToast('加载失败: ' + e.message, 'error');
    }
}

async function exportCSV() {
    try {
        const resp = await fetch('/api/export-csv');
        const data = await resp.json();

        if (data.error) {
            showToast('导出失败: ' + data.error, 'error');
            return;
        }

        showToast('CSV 已导出: ' + data.path, 'success');
    } catch (e) {
        showToast('导出失败: ' + e.message, 'error');
    }
}

// ============================================================
// 设置
// ============================================================
async function loadSettings() {
    try {
        const resp = await fetch('/api/settings');
        const data = await resp.json();

        document.getElementById('setModelName').value = data.local_model_name || 'qwen2.5:7b';
        document.getElementById('setUseGpu').checked = data.use_gpu || false;
        document.getElementById('setApiKey').value = data.deepseek_api_key || '';
        document.getElementById('setCloudModel').value = data.cloud_model_name || 'deepseek-chat';
        document.getElementById('setMotorcadPath').value = data.motorcad_exe_path || '';
        if (document.getElementById('setMotorcadProgId')) {
            document.getElementById('setMotorcadProgId').value = data.motorcad_progid || 'MotorCAD.MotorCAD';
        }
        if (document.getElementById('setMotorcadUseMode')) {
            document.getElementById('setMotorcadUseMode').value = data.motorcad_use_mode || 'auto';
        }
        if (document.getElementById('motorcadConnStatus')) {
            const statusEl = document.getElementById('motorcadConnStatus');
            try {
                const statusResp = await fetch('/api/status');
                const statusData = await statusResp.json();
                if (statusData.motorcad && statusData.motorcad.is_demo) {
                    statusEl.textContent = 'Demo 版：COM 自动化被禁用，建议使用模拟/手动导入模式';
                    statusEl.style.color = 'var(--error)';
                } else if (statusData.motorcad && statusData.motorcad.available) {
                    statusEl.textContent = 'COM 已连接 (Motor-CAD 运行中)';
                    statusEl.style.color = 'var(--success)';
                } else if (statusData.motorcad && statusData.motorcad.installed) {
                    statusEl.textContent = '已安装，COM 未连接 (Motor-CAD 启动约需 1 分钟，求解时会等待最长 2 分钟)';
                    statusEl.style.color = 'var(--warning)';
                } else {
                    statusEl.textContent = '未检测到 Motor-CAD';
                    statusEl.style.color = 'var(--error)';
                }
            } catch (e) {
                statusEl.textContent = '状态检查失败';
            }
        }

        // 更新 GPU 提示
        updateGpuHint();
    } catch (e) {
        console.error('loadSettings error:', e);
    }
}

async function saveSettings() {
    const data = {
        local_model_name: document.getElementById('setModelName').value,
        use_gpu: document.getElementById('setUseGpu').checked,
        deepseek_api_key: document.getElementById('setApiKey').value,
        cloud_model_name: document.getElementById('setCloudModel').value,
    };

    if (document.getElementById('setMotorcadUseMode')) {
        data.motorcad_use_mode = document.getElementById('setMotorcadUseMode').value;
    }

    try {
        const resp = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        const result = await resp.json();

        if (result.status === 'ok') {
            showToast('设置已保存并即时生效', 'success');
            loadStatus();
        } else {
            showToast('保存失败: ' + (result.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('保存失败: ' + e.message, 'error');
    }
}

// ============================================================
// 网络访问管理
// ============================================================
async function loadNetworkStatus() {
    try {
        const resp = await fetch('/api/network/status');
        const data = await resp.json();

        // 绑定地址下拉框
        const select = document.getElementById('setBindHost');
        if (select) {
            // 动态添加局域网 IP 选项
            const currentVal = data.bind_host || '127.0.0.1';
            // 清除旧的动态选项（保留前两个固定选项）
            while (select.options.length > 2) select.remove(2);
            // 添加局域网 IP
            (data.lan_ips || []).forEach(ip => {
                const opt = document.createElement('option');
                opt.value = ip;
                opt.textContent = `${ip} — 绑定到此网卡`;
                select.appendChild(opt);
            });
            // 添加 Tailscale IP
            if (data.tailscale && data.tailscale.ip) {
                const opt = document.createElement('option');
                opt.value = data.tailscale.ip;
                opt.textContent = `${data.tailscale.ip} — Tailscale`;
                select.appendChild(opt);
            }
            select.value = currentVal;
        }

        // 状态文本
        const statusEl = document.getElementById('netStatusText');
        if (statusEl) {
            const lanStr = (data.lan_ips || []).join(', ') || '未检测到';
            let html = `绑定: <strong>${data.bind_host}</strong> ｜ 端口: ${data.port} ｜ 监听: ${data.port_listening ? '✅' : '❌'}<br>`;
            html += `局域网 IP: ${lanStr}`;
            if (data.tailscale && data.tailscale.ip) {
                html += `<br>Tailscale IP: ${data.tailscale.ip}`;
            }
            statusEl.innerHTML = html;
        }

        // Tailscale 状态
        const tsEl = document.getElementById('tailscaleStatus');
        if (tsEl) {
            const ts = data.tailscale || {};
            if (!ts.installed) {
                tsEl.innerHTML = '⚪ <span style="color:var(--text-light);">未安装</span>';
            } else if (!ts.running) {
                tsEl.innerHTML = '🟡 <span style="color:var(--warning);">已安装，未运行</span>';
            } else {
                tsEl.innerHTML = `🟢 <span style="color:var(--success);">运行中</span> — IP: <strong>${ts.ip}</strong>，主机名: ${ts.hostname || '-'}`;
            }
        }

        // 局域网 URL 显示
        const lanUrlEl = document.getElementById('netLanUrl');
        if (lanUrlEl && data.lan_ips && data.lan_ips.length > 0) {
            lanUrlEl.textContent = `http://${data.lan_ips[0]}:${data.port}`;
        }
    } catch (e) {
        console.error('loadNetworkStatus error:', e);
        const statusEl = document.getElementById('netStatusText');
        if (statusEl) statusEl.textContent = '检测失败: ' + e.message;
    }
}

async function saveNetworkConfig() {
    const select = document.getElementById('setBindHost');
    if (!select) return;
    const bindHost = select.value;

    try {
        const resp = await fetch('/api/network/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ bind_host: bindHost }),
        });
        const result = await resp.json();
        if (result.status === 'ok') {
            showToast(result.message, 'success');
            if (bindHost !== '127.0.0.1') {
                setTimeout(() => {
                    showToast('⚠️ 请重启 Web UI (start_ui.bat) 使绑定地址生效', 'info');
                }, 1000);
            }
        } else {
            showToast('保存失败: ' + (result.message || result.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('保存失败: ' + e.message, 'error');
    }
}

async function showNetworkQRCode() {
    try {
        const resp = await fetch('/api/network/qrcode');
        const data = await resp.json();
        if (data.error) {
            showToast(data.error, 'error');
            return;
        }

        const lan = data.lan || data;
        const ts = data.tailscale;

        // 创建模态弹窗
        let modal = document.getElementById('qrModal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'qrModal';
            modal.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999;';
            modal.addEventListener('click', (e) => {
                if (e.target === modal) modal.style.display = 'none';
            });
            document.body.appendChild(modal);
        }

        let qrHTML = `
            <div style="flex:1;text-align:center;">
                <h4 style="margin:0 0 4px;">局域网</h4>
                <p style="color:var(--text-secondary);font-size:11px;margin:0 0 8px;">手机和电脑须同一 Wi-Fi</p>
                <img src="${lan.qr_code}" style="width:200px;height:200px;border:1px solid var(--border);border-radius:8px;" />
                <p style="margin:8px 0 0;font-size:12px;font-family:monospace;color:var(--primary);">${lan.url}</p>
            </div>
        `;

        if (ts) {
            qrHTML += `
                <div style="flex:1;text-align:center;">
                    <h4 style="margin:0 0 4px;color:var(--success,#27ae60);">Tailscale</h4>
                    <p style="color:var(--text-secondary);font-size:11px;margin:0 0 8px;">手机装 Tailscale 后扫码</p>
                    <img src="${ts.qr_code}" style="width:200px;height:200px;border:1px solid var(--border);border-radius:8px;" />
                    <p style="margin:8px 0 0;font-size:12px;font-family:monospace;color:var(--success,#27ae60);">${ts.url}</p>
                </div>
            `;
        }

        let warning = '';
        if (!ts) {
            warning = '<p style="color:var(--text-light);font-size:11px;margin:8px 0 0;">提示：安装 Tailscale 可实现外网访问，不受 Wi-Fi 网络类型限制</p>';
        } else {
            warning = '<p style="color:var(--text-light);font-size:11px;margin:8px 0 0;">局域网二维码如无法访问，请用 Tailscale 二维码（更稳定）</p>';
        }

        modal.innerHTML = `
            <div style="background:var(--surface);border-radius:var(--radius);padding:32px;text-align:center;max-width:520px;box-shadow:0 8px 32px rgba(0,0,0,0.2);">
                <h3 style="margin:0 0 8px;">扫码访问 Motor-AI</h3>
                <p style="color:var(--text-secondary);font-size:13px;margin:0 0 20px;">手机浏览器扫描下方二维码直接打开</p>
                <div style="display:flex;gap:24px;align-items:flex-start;">
                    ${qrHTML}
                </div>
                ${warning}
                <p style="color:var(--text-light);font-size:11px;margin:8px 0 16px;">需先在"绑定地址"中选择 0.0.0.0 并重启</p>
                <button class="btn btn-secondary" style="margin-top:8px;" onclick="document.getElementById('qrModal').style.display='none'">关闭</button>
            </div>
        `;
        modal.style.display = 'flex';
    } catch (e) {
        showToast('生成二维码失败: ' + e.message, 'error');
    }
}

async function checkNetworkAccess(target) {
    const resultEl = document.getElementById('netCheckResult');
    if (resultEl) {
        resultEl.textContent = '检测中...';
        resultEl.style.color = 'var(--text-secondary)';
    }

    try {
        const resp = await fetch('/api/network/check-access', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target: target }),
        });
        const data = await resp.json();

        if (resultEl) {
            if (data.reachable) {
                resultEl.innerHTML = data.message;
                resultEl.style.color = 'var(--success)';
            } else {
                resultEl.innerHTML = data.error || '不可达';
                resultEl.style.color = 'var(--error)';
            }
        }
    } catch (e) {
        if (resultEl) {
            resultEl.textContent = '检测失败: ' + e.message;
            resultEl.style.color = 'var(--error)';
        }
    }
}
function showToast(message, type) {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast toast--${type || 'info'}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.animation = 'slideIn 0.3s ease reverse';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

function showLoading(text) {
    document.getElementById('loadingText').textContent = text || '处理中...';
    document.getElementById('loadingOverlay').style.display = 'flex';
}

function hideLoading() {
    document.getElementById('loadingOverlay').style.display = 'none';
}

function effBadge(eff) {
    if (!eff && eff !== 0) return '-';
    let cls = 'eff-badge--low';
    if (eff >= 93) cls = 'eff-badge--high';
    else if (eff >= 85) cls = 'eff-badge--mid';
    return `<span class="eff-badge ${cls}">${eff}%</span>`;
}

function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

function renderMarkdown(text) {
    if (!text) return '<div class="empty-hint">无内容</div>';

    // 转义 HTML
    let html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    // 代码块
    html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
        return `<pre>${code.trim()}</pre>`;
    });

    // 行内代码
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

    // Markdown 表格 (在标题之前处理)
    html = html.replace(/^(\|.+\|)\n(\|[-| :]+\|)\n((?:\|.+\|\n?)*)/gm, (match, header, sep, body) => {
        const headers = header.split('|').filter(h => h.trim()).map(h => h.trim());
        const rows = body.trim().split('\n').filter(r => r.trim() && r.startsWith('|'));
        let tableHtml = '<table class="md-table"><thead><tr>';
        headers.forEach(h => { tableHtml += `<th>${h}</th>`; });
        tableHtml += '</tr></thead><tbody>';
        rows.forEach(row => {
            const cells = row.split('|').filter(c => c.trim()).map(c => c.trim());
            tableHtml += '<tr>';
            cells.forEach(c => { tableHtml += `<td>${c}</td>`; });
            tableHtml += '</tr>';
        });
        tableHtml += '</tbody></table>';
        return tableHtml;
    });

    // 标题
    html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

    // 粗体
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

    // 无序列表
    html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>[\s\S]*?<\/li>)(?=\n[^-]|\n$|$)/g, '<ul>$1</ul>');

    // 有序列表
    html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');

    // 引用块
    html = html.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');

    // 段落（连续空行分段）
    html = html.replace(/\n{2,}/g, '</p><p>');
    html = '<p>' + html + '</p>';

    // 清理：移除标题、列表、pre、table、blockquote 标签外的多余 p 标签
    html = html.replace(/<p><(h[1-3]|ul|pre|table|blockquote)/g, '<$1');
    html = html.replace(/<\/(h[1-3]|ul|pre|table|blockquote)><\/p>/g, '</$1>');

    return html;
}

// ============================================================
// AI 团队（多智能体工作流）
// ============================================================
async function runAgentWorkflow() {
    const text = document.getElementById('agentInputText').value.trim();
    if (!text) { showToast('请输入客户需求', 'warn'); return; }

    const solveCount = parseInt(document.getElementById('agentSolveCount').value);
    const useCloudReview = document.getElementById('agentUseCloudReview').checked;
    const useCloudReport = document.getElementById('agentUseCloudReport').checked;

    // 重置 pipeline (双轨)
    ['parse_requirement', 'generate_designs', 'review_solutions',
     'convergence_review', 'decision_gate', 'generate_report'].forEach(s => {
        updatePipelineStep(s, 'pending', '等待中');
    });
    document.getElementById('agentResults').style.display = 'none';
    document.getElementById('decisionGate').style.display = 'none';

    // 清空消息栏
    clearMsgBar();
    addMsgBar('info', 'AI 团队启动中...');
    document.getElementById('gateReadPro').checked = false;
    document.getElementById('gateReadCon').checked = false;
    document.getElementById('agentRunBtn').disabled = true;
    document.getElementById('agentRunBtn').textContent = '运行中...';
    _myWorkflowActive = true;  // v1.03.02: 标记自己发起的工作流

    try {
        const resp = await fetch('/api/workflow/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                raw_text: text, solve_count: solveCount,
                use_cloud_review: useCloudReview, use_cloud_report: useCloudReport
            })
        });

        if (!resp.ok) {
            const errData = await resp.json().catch(() => ({}));
            showToast('AI 团队启动失败: ' + (errData.error || resp.statusText), 'error');
            return;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                try {
                    const data = JSON.parse(line.slice(6));
                    handleWorkflowEvent(data);
                } catch (e) { /* skip */ }
            }
        }
    } catch (e) {
        showToast('工作流错误: ' + e.message, 'error');
    } finally {
        _myWorkflowActive = false;  // v1.03.02: 工作流结束
        document.getElementById('agentRunBtn').disabled = false;
        document.getElementById('agentRunBtn').textContent = '启动 AI 团队';
        hideRemoteBanner();
    }
}

function handleWorkflowEvent(data) {
    if (data.type === 'start') {
        showToast('AI 团队已启动 (辩证双轨)', 'info');
        addMsgBar('info', '工作流已启动 (辩证双轨制衡)');
        if (data.steps && data.steps.length) {
            addMsgBar('info', '预期步骤: ' + data.steps.join(' → '));
        }
    } else if (data.type === 'step_start') {
        var startMap = {
            'parse_requirement': '需求分析师',
            'generate_designs': '方案生成师',
            'review_solutions': '评审推荐师',
            'convergence_review': '收敛阵营',
            'decision_gate': '决策门',
            'generate_report': '报告撰写员',
        };
        var sName = startMap[data.step] || data.step;
        updatePipelineStep(data.step, 'running', '执行中');
        addMsgBar('step', '[' + sName + '] ' + (data.message || '开始执行...'));
    } else if (data.type === 'step_done') {
        const stepMap = {
            'parse_requirement': { name: '需求解析完成', next: 'generate_designs' },
            'generate_designs': { name: '方案生成完成', next: 'review_solutions' },
            'review_solutions': { name: '支持报告完成', next: 'decision_gate' },
            'convergence_review': { name: '反对报告完成', next: 'decision_gate' },
            'decision_gate': { name: '决策门就绪', next: 'generate_report' },
            'generate_report': { name: '报告生成完成', next: null }
        };
        const info = stepMap[data.step];
        if (info) {
            updatePipelineStep(data.step, 'done', info.name);
            addMsgBar('step', '[' + info.name + ']');
            // 双轨: generate_designs 完成后同时启动 review 和 convergence
            if (data.step === 'generate_designs' && info.next) {
                updatePipelineStep('review_solutions', 'running', '执行中');
                updatePipelineStep('convergence_review', 'running', '收敛阵营执行中');
                addMsgBar('info', '双轨并行启动: 放大轨(评审推荐师) + 收敛轨(四剑客)');
                addMsgBar('info', '收敛阵营执行中, 含4个agent串行调用, 预计需要1-3分钟...');
            } else if (info.next && !(data.step === 'review_solutions' || data.step === 'convergence_review')) {
                updatePipelineStep(info.next, 'running', '执行中');
            }
        }

        // 额外进度信息
        if (data.step === 'parse_requirement' && data.specs) {
            document.getElementById('agentResults').style.display = 'block';
            document.getElementById('agentSpecsDisplay').textContent = JSON.stringify(data.specs, null, 2);
            addMsgBar('info', '需求解析: ' + JSON.stringify(data.specs).slice(0, 120) + '...');
        }
        if (data.step === 'generate_designs') {
            addMsgBar('info', '方案生成: ' + (data.results_count || 0) + ' 个方案');
        }
        if (data.step === 'review_solutions' && data.review) {
            document.getElementById('agentReviewDisplay').innerHTML = renderMarkdown(data.review);
            addMsgBar('success', '支持报告已生成 (放大轨)');
        }
        if (data.step === 'convergence_review' && data.con_report) {
            document.getElementById('agentConReportDisplay').innerHTML = renderMarkdown(data.con_report);
            addMsgBar('success', '反对报告已生成 (收敛轨)');
        }
        if (data.step === 'decision_gate') {
            document.getElementById('decisionGate').style.display = 'block';
            const ready = data.ready_for_confirm;
            const gs = document.getElementById('gateStatus');
            if (ready) {
                gs.textContent = '双报告已齐备，请阅读后确认';
                gs.className = 'gate-status gate-ready';
                addMsgBar('success', '决策门: 双报告齐备, 等待用户确认');
            } else {
                gs.textContent = '报告未齐备';
                gs.className = 'gate-status gate-pending';
                addMsgBar('warn', '决策门: 报告未齐备');
            }
        }
        if (data.step === 'generate_report' && data.report) {
            document.getElementById('agentReportDisplay').innerHTML = renderMarkdown(data.report);
            if (data.report_filepath) {
                const btn = document.getElementById('agentReportBtn');
                btn.style.display = 'inline-block';
                btn.dataset.filepath = data.report_filepath;
            }
            addMsgBar('success', '正式报告已生成');
        }
    } else if (data.type === 'step_error') {
        updatePipelineStep(data.step, 'error', '错误: ' + (data.error || '').slice(0, 50));
        showToast('步骤错误: ' + data.step, 'warn');
        addMsgBar('error', '步骤[' + data.step + ']错误: ' + (data.error || '未知错误'));
    } else if (data.type === 'progress') {
        var level = data.level || 'info';
        var ts = data.timestamp ? data.timestamp + ' ' : '';
        addMsgBar(level, ts + (data.message || ''));
    } else if (data.type === 'done') {
        showToast('辩证制衡工作流完成', 'success');
        addMsgBar('success', '======= 工作流全部完成 =======');
    } else if (data.type === 'fatal') {
        showToast('致命错误: ' + data.error, 'error');
        addMsgBar('error', '致命错误: ' + (data.error || '未知'));
    }
}

// ==================== 消息栏 ====================
function addMsgBar(type, text) {
    const body = document.getElementById('msgBarBody');
    if (!body) return;
    // 移除空提示
    const empty = body.querySelector('.msg-empty');
    if (empty) empty.remove();

    const now = new Date();
    const ts = String(now.getHours()).padStart(2, '0') + ':' +
               String(now.getMinutes()).padStart(2, '0') + ':' +
               String(now.getSeconds()).padStart(2, '0');

    const icons = { info: '\u2139', success: '\u2714', warn: '\u26a0', error: '\u2716', step: '\u25b6' };
    const icon = icons[type] || icons.info;

    const line = document.createElement('div');
    line.className = 'msg-line msg-' + type;
    line.innerHTML = '<span class="msg-time">' + ts + '</span>' +
                     '<span class="msg-icon">' + icon + '</span>' +
                     '<span class="msg-text"></span>';
    line.querySelector('.msg-text').textContent = text;

    body.appendChild(line);
    body.scrollTop = body.scrollHeight;
}

function clearMsgBar() {
    const body = document.getElementById('msgBarBody');
    if (!body) return;
    body.innerHTML = '<div class="msg-empty">点击「启动 AI 团队」后，此处将显示实时运行日志</div>';
}

// 决策门: 检查是否双报告都已阅读
function checkGateReady() {
    const readPro = document.getElementById('gateReadPro').checked;
    const readCon = document.getElementById('gateReadCon').checked;
    const ready = readPro && readCon;
    document.getElementById('gateAdoptBtn').disabled = !ready;
    document.getElementById('gateRejectBtn').disabled = !ready;
}

// 决策门: 采纳
async function gateAdopt() {
    const specsText = document.getElementById('agentSpecsDisplay').textContent;
    let specs = {};
    try { specs = JSON.parse(specsText); } catch (e) {}
    // 取 Top1 方案 id (从工作流结果中, 简化用最近一条)
    showToast('采纳决策已记录，飞轮正在沉淀技能模板...', 'info');
    try {
        const resp = await fetch('/api/results/top?n=1');
        const data = await resp.json();
        const top = (data.results || [])[0];
        if (top) {
            const fb = await fetch('/api/feedback', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ design_id: top.id, action: 'adopt', specs: specs, reason: '决策门采纳' })
            });
            const result = await fb.json();
            if (result.skill_template) {
                showToast('技能模板已生成: ' + (result.template_name || result.skill_template), 'success');
            } else {
                showToast('采纳已记录', 'success');
            }
        }
    } catch (e) {
        showToast('采纳记录失败: ' + e.message, 'error');
    }
}

// 决策门: 拒绝
async function gateReject() {
    const reason = prompt('请简要说明拒绝原因 (供飞轮学习):');
    if (reason === null) return;
    const specsText = document.getElementById('agentSpecsDisplay').textContent;
    let specs = {};
    try { specs = JSON.parse(specsText); } catch (e) {}
    try {
        const resp = await fetch('/api/results/top?n=1');
        const data = await resp.json();
        const top = (data.results || [])[0];
        if (top) {
            await fetch('/api/feedback', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ design_id: top.id, action: 'reject', specs: specs, reason: reason })
            });
            showToast('拒绝已记录，失败特征已沉淀', 'success');
        }
    } catch (e) {
        showToast('拒绝记录失败: ' + e.message, 'error');
    }
}

function updatePipelineStep(step, status, desc) {
    const el = document.getElementById('step-' + step);
    if (el) el.dataset.status = status;
    const descEl = document.getElementById('step-desc-' + step);
    if (descEl) descEl.textContent = desc;
}

function downloadAgentReport() {
    const btn = document.getElementById('agentReportBtn');
    if (btn && btn.dataset.filepath) {
        showToast('报告已保存: ' + btn.dataset.filepath, 'success');
    }
}

// ============================================================
// 进化仪表盘
// ============================================================
async function loadEvolutionDashboard() {
    try {
        const resp = await fetch('/api/evolution/dashboard');
        const data = await resp.json();

        // 指标
        const m = data.metrics || {};
        document.getElementById('evoTotalDesigns').textContent = m.total_designs || 0;
        document.getElementById('evoAdopted').textContent = m.adopted_designs || 0;
        document.getElementById('evoAdoptionRate').textContent = (m.adoption_rate || 0) + '%';
        document.getElementById('evoSkills').textContent = m.skill_templates_count || 0;
        document.getElementById('evoRules').textContent = m.rules_count || 0;

        // 飞轮指标 (AI接管率 + 失败特征)
        try {
            const fwResp = await fetch('/api/flywheel/status');
            const fw = await fwResp.json();
            document.getElementById('evoAiRate').textContent = (fw.ai_intervention_rate || 0) + '%';
            document.getElementById('evoFailures').textContent = fw.failure_patterns_count || 0;
        } catch (e) { /* 飞轮不可用时保持默认 */ }

        // 技能模板
        const skillList = document.getElementById('skillTemplatesList');
        const templates = data.skill_templates || [];
        if (templates.length === 0) {
            skillList.innerHTML = '<div class="empty-hint">暂无技能模板，采纳方案后将自动生成</div>';
        } else {
            skillList.innerHTML = templates.map(t => `
                <div class="skill-item">
                    <div>
                        <div class="skill-name">${t.name || 'Unnamed'}</div>
                        <div class="skill-meta">${t.application || ''} | 效率: ${t.performance?.efficiency || '?'}% | 使用: ${t.usage_count || 0}次</div>
                    </div>
                    <span class="rule-confidence">${t.success_rate ? (t.success_rate * 100).toFixed(0) + '%' : '100%'}</span>
                </div>
            `).join('');
        }

        // 设计规则
        const rulesList = document.getElementById('designRulesList');
        const rules = data.design_rules || [];
        if (rules.length === 0) {
            rulesList.innerHTML = '<div class="empty-hint">暂无设计规则</div>';
        } else {
            rulesList.innerHTML = rules.map(r => `
                <div class="rule-item">
                    <div class="rule-condition">${r.condition}<span class="rule-confidence">${(r.confidence * 100).toFixed(0)}%</span></div>
                    <div class="rule-constraint">${r.constraint}</div>
                </div>
            `).join('');
        }

        // 进化历史
        const historyList = document.getElementById('evolutionHistoryList');
        const history = data.evolution_history || [];
        if (history.length === 0) {
            historyList.innerHTML = '<div class="empty-hint">暂无进化记录</div>';
        } else {
            historyList.innerHTML = history.slice().reverse().map(h => `
                <div class="history-item">
                    <span class="history-date">${h.date}</span> (v${h.version})
                    <div class="history-change">${h.change} | 规则: ${h.rules_count} | 模板: ${h.templates_count}</div>
                </div>
            `).join('');
        }
    } catch (e) {
        showToast('加载进化仪表盘失败: ' + e.message, 'error');
    }
}

async function mineRule() {
    try {
        showToast('正在挖掘规则...', 'info');
        const resp = await fetch('/api/evolution/mine-rule', { method: 'POST' });
        const data = await resp.json();
        if (data.rule) {
            showToast('新规则已挖掘: ' + data.rule.constraint, 'success');
        } else {
            showToast(data.message || '数据不足', 'warn');
        }
        loadEvolutionDashboard();
    } catch (e) {
        showToast('挖掘失败: ' + e.message, 'error');
    }
}

// ============================================================
// 审计日志
// ============================================================
async function loadAuditLogs() {
    try {
        const [logsResp, statsResp] = await Promise.all([
            fetch('/api/audit/logs?limit=50'),
            fetch('/api/audit/stats')
        ]);
        const logsData = await logsResp.json();
        const statsData = await statsResp.json();

        // 统计
        document.getElementById('auditTotal').textContent = statsData.total_logs || 0;
        document.getElementById('auditToday').textContent = statsData.today_logs || 0;
        document.getElementById('auditAgents').textContent = statsData.active_agents || 0;

        // 日志表格
        const tbody = document.getElementById('auditTableBody');
        const logs = logsData.logs || [];
        if (logs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-secondary)">暂无审计日志</td></tr>';
        } else {
            tbody.innerHTML = logs.map(log => {
                const ts = log.timestamp ? log.timestamp.slice(11, 19) : '-';
                const params = log.parameters ? log.parameters.slice(0, 80) : '';
                const statusClass = log.status === 'success' ? 'audit-status-success' : 'audit-status-error';
                return `<tr>
                    <td>${ts}</td>
                    <td><span class="audit-agent-badge">${log.agent_name}</span></td>
                    <td>${log.action}</td>
                    <td class="${statusClass}">${log.status}</td>
                    <td>${log.duration_ms || '-'}</td>
                    <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis">${params}</td>
                </tr>`;
            }).join('');
        }
    } catch (e) {
        showToast('加载审计日志失败: ' + e.message, 'error');
    }
}

// ============================================================
// 方案反馈（采纳/拒绝）
// ============================================================
let _selectedSolutionId = null;
let _currentSpecs = {};

function selectSolutionForFeedback(id, solutionData) {
    _selectedSolutionId = id;
    const section = document.getElementById('feedbackSection');
    section.style.display = 'block';
    const info = document.getElementById('feedbackSolutionInfo');
    if (solutionData) {
        info.textContent = `方案 #${id}: 外径${solutionData.stator_od || '?'}mm, 极数${solutionData.poles || '?'}, 效率${solutionData.efficiency?.toFixed(1) || '?'}%`;
    }
    // 滚动到反馈区域
    section.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function submitFeedback(action) {
    if (!_selectedSolutionId) {
        showToast('请先在表格中选择一个方案', 'warn');
        return;
    }

    const reason = document.getElementById('feedbackReason').value.trim();

    try {
        const resp = await fetch('/api/feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                design_id: _selectedSolutionId,
                action: action,
                reason: reason,
                specs: _currentSpecs
            })
        });
        const data = await resp.json();

        if (data.success) {
            if (action === 'adopt') {
                showToast('方案已采纳! 技能模板已生成', 'success');
            } else {
                showToast('方案已拒绝', 'info');
            }
            document.getElementById('feedbackSection').style.display = 'none';
            document.getElementById('feedbackReason').value = '';
        } else {
            showToast('操作失败: ' + (data.error || ''), 'error');
        }
    } catch (e) {
        showToast('反馈提交失败: ' + e.message, 'error');
    }
}


// ============================================================
// Motor-CAD 手动导入 (Demo/授权受限时的替代方案)
// ============================================================
async function importMotorcadJson() {
    const textarea = document.getElementById('importJsonData');
    if (!textarea) return;
    const raw = textarea.value.trim();
    if (!raw) {
        showToast('请输入 JSON 数据', 'warn');
        return;
    }

    try {
        const payload = JSON.parse(raw);
        if (!payload.params || !payload.result) {
            showToast('JSON 需包含 params 和 result 字段', 'warn');
            return;
        }

        showLoading('正在导入 Motor-CAD 结果...');
        const resp = await fetch('/api/motorcad/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                params: payload.params,
                result: payload.result,
                specs: state.specs || {}
            })
        });
        const data = await resp.json();
        hideLoading();

        if (data.success) {
            showToast(`导入成功，已保存到数据库 (ID: ${data.saved_id})`, 'success');
            textarea.value = '';
            loadStatus();
        } else {
            showToast('导入失败: ' + (data.error || ''), 'error');
        }
    } catch (e) {
        hideLoading();
        showToast('JSON 解析失败: ' + e.message, 'error');
    }
}

async function importMotorcadCsv() {
    const input = document.getElementById('importCsvPath');
    if (!input) return;
    const path = input.value.trim();
    if (!path) {
        showToast('请输入 CSV 文件路径', 'warn');
        return;
    }

    showLoading('正在导入 CSV...');
    try {
        const resp = await fetch('/api/motorcad/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                csv_path: path,
                specs: state.specs || {}
            })
        });
        const data = await resp.json();
        hideLoading();

        if (data.success) {
            showToast(`导入成功 ${data.imported_count} 组结果`, 'success');
            input.value = '';
            loadStatus();
        } else {
            showToast('导入失败: ' + (data.error || ''), 'error');
        }
    } catch (e) {
        hideLoading();
        showToast('导入失败: ' + e.message, 'error');
    }
}

// ============================================================
// 手动求解工作流: 导出参数 / 操作指南 / 文件上传
// ============================================================
async function exportParamsCsv() {
    const specs = state.specs || {};
    const countSelect = document.getElementById('solveCount');
    const count = countSelect ? parseInt(countSelect.value) : 30;

    showLoading('正在生成参数矩阵...');
    try {
        const resp = await fetch('/api/motorcad/download-params', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ specs, count })
        });

        if (resp.ok) {
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `motorcad_params_${new Date().toISOString().slice(0,10)}.csv`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            hideLoading();
            showToast('参数 CSV 已下载，请在 Motor-CAD 中按此表设置参数', 'success');
        } else {
            const data = await resp.json();
            hideLoading();
            showToast('导出失败: ' + (data.error || ''), 'error');
        }
    } catch (e) {
        hideLoading();
        showToast('导出失败: ' + e.message, 'error');
    }
}

async function showMotorcadGuide() {
    try {
        const resp = await fetch('/api/motorcad/guide');
        const guide = await resp.json();

        if (guide.error) {
            showToast('获取指南失败: ' + guide.error, 'error');
            return;
        }

        let html = `<div style="max-width:700px;line-height:1.8;">
            <h3 style="margin-bottom:12px;">${guide.title}</h3>
            <p style="color:var(--text-secondary);margin-bottom:16px;">${guide.com_status}</p>
            <p style="margin-bottom:16px;"><strong>模板文件:</strong> ${guide.template_file}</p>`;

        if (guide.steps) {
            html += '<ol style="margin-bottom:16px;">';
            for (const step of guide.steps) {
                html += `<li style="margin-bottom:12px;"><strong>${step.title}</strong><br><span style="font-size:13px;color:var(--text-secondary);">${step.description}</span>`;
                if (step.variables) {
                    html += '<ul style="margin-top:4px;font-size:13px;">';
                    for (const v of step.variables) {
                        html += `<li><code>${v.name}</code> - ${v.description}</li>`;
                    }
                    html += '</ul>';
                }
                if (step.result_variables) {
                    html += '<ul style="margin-top:4px;font-size:13px;">';
                    for (const rv of step.result_variables) {
                        html += `<li>${rv}</li>`;
                    }
                    html += '</ul>';
                }
                html += '</li>';
            }
            html += '</ol>';
        }

        html += '</div>';

        // 使用简单的 modal 显示
        showModal('Motor-CAD 手动操作指南', html);
    } catch (e) {
        showToast('获取指南失败: ' + e.message, 'error');
    }
}

function showModal(title, contentHtml) {
    // 创建或复用 modal
    let modal = document.getElementById('dynamicModal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'dynamicModal';
        modal.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;';
        modal.onclick = function(e) { if (e.target === modal) modal.style.display = 'none'; };
        document.body.appendChild(modal);
    }
    modal.innerHTML = `
        <div style="background:var(--bg-card);border-radius:12px;max-width:800px;max-height:80vh;overflow-y:auto;padding:24px;box-shadow:0 8px 32px rgba(0,0,0,0.3);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
                <h2 style="margin:0;">${title}</h2>
                <button onclick="document.getElementById('dynamicModal').style.display='none'" style="background:none;border:none;font-size:24px;cursor:pointer;color:var(--text-secondary);">×</button>
            </div>
            ${contentHtml}
        </div>
    `;
    modal.style.display = 'flex';
}

async function uploadResultCsv() {
    const fileInput = document.getElementById('resultCsvFile');
    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        showToast('请选择 CSV 文件', 'warn');
        return;
    }

    const file = fileInput.files[0];
    const formData = new FormData();
    formData.append('file', file);

    showLoading('正在导入 CSV 结果...');
    try {
        const resp = await fetch('/api/motorcad/upload-csv', {
            method: 'POST',
            body: formData
        });
        const data = await resp.json();
        hideLoading();

        if (data.success) {
            showToast(data.message || `导入成功 ${data.imported_count} 组结果`, 'success');
            fileInput.value = '';
            loadStatus();
            // 刷新结果列表
            if (typeof loadResults === 'function') loadResults();
        } else {
            showToast('导入失败: ' + (data.error || ''), 'error');
        }
    } catch (e) {
        hideLoading();
        showToast('导入失败: ' + e.message, 'error');
    }
}

// ============================================================
// 半自动求解工作流
// ============================================================
let _saPollTimer = null;

async function startSemiAutoSolve() {
    const params = {
        speed: parseFloat(document.getElementById('saSpeed').value) || 3000,
        copper_loss: parseFloat(document.getElementById('saCopperLoss').value) || 400,
        wj_inlet_temp: parseFloat(document.getElementById('saWjTemp').value) || 40,
        wj_flow_rate: (parseFloat(document.getElementById('saWjFlow').value) || 5) * 1.67e-5,
    };

    try {
        const resp = await fetch('/api/semi-auto/solve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ params }),
        });
        const data = await resp.json();
        if (resp.ok && data.status === 'started') {
            showToast('半自动求解已启动!', 'success');
            document.getElementById('saStartBtn').style.display = 'none';
            document.getElementById('saCancelBtn').style.display = '';
            document.getElementById('saProgressBar').style.display = '';
            document.getElementById('saResultArea').style.display = 'none';
            _saPollTimer = setInterval(pollSemiAutoStatus, 2000);
        } else {
            showToast('启动失败: ' + (data.error || ''), 'error');
        }
    } catch (e) {
        showToast('启动失败: ' + e.message, 'error');
    }
}

async function cancelSemiAutoSolve() {
    try {
        await fetch('/api/semi-auto/cancel', { method: 'POST' });
        if (_saPollTimer) { clearInterval(_saPollTimer); _saPollTimer = null; }
        document.getElementById('saStartBtn').style.display = '';
        document.getElementById('saCancelBtn').style.display = 'none';
        document.getElementById('saStatusText').textContent = '已取消';
        showToast('已取消', 'info');
    } catch (e) {
        showToast('取消失败: ' + e.message, 'error');
    }
}

async function pollSemiAutoStatus() {
    try {
        const resp = await fetch('/api/semi-auto/status');
        const st = await resp.json();
        const phaseMap = {
            'idle': 0, 'modifying': 1, 'starting': 2, 'waiting': 3,
            'monitoring': 4, 'reading': 5, 'done': 5, 'error': 0, 'cancelled': 0
        };
        const stepIdx = phaseMap[st.phase] || 0;
        const stepColors = ['#4CAF50', '#2196F3', '#FF9800', '#9C27B0', '#F44336', '#4CAF50'];
        for (let i = 1; i <= 5; i++) {
            const el = document.getElementById('saStep' + i);
            if (i <= stepIdx) {
                el.style.background = stepColors[i-1];
                el.style.color = '#fff';
            } else if (i === stepIdx + 1 && st.running) {
                el.style.background = '#FF9800';
                el.style.color = '#fff';
            } else {
                el.style.background = 'var(--border)';
                el.style.color = '';
            }
        }
        document.getElementById('saDetailStatus').textContent = st.message || st.phase;
        document.getElementById('saStatusText').textContent = st.phase;
        if (st.phase === 'done' || st.phase === 'error' || st.phase === 'cancelled') {
            if (_saPollTimer) { clearInterval(_saPollTimer); _saPollTimer = null; }
            document.getElementById('saStartBtn').style.display = '';
            document.getElementById('saCancelBtn').style.display = 'none';
            if (st.phase === 'done' && st.result) {
                showSemiAutoResult(st.result);
                showToast('半自动求解完成!', 'success');
            } else if (st.phase === 'error') {
                showToast('错误: ' + (st.error || ''), 'error');
            }
        }
    } catch (e) {
        console.error('Status poll error:', e);
    }
}

function showSemiAutoResult(result) {
    const area = document.getElementById('saResultArea');
    const content = document.getElementById('saResultContent');
    area.style.display = '';
    let html = '';
    html += `<p><strong>计算完成:</strong> ${result.calculation_completed ? '✅ 是' : '❌ 否'}</p>`;
    html += `<p><strong>.mot 文件:</strong> ${result.mot_file || 'N/A'}</p>`;
    if (result.new_files && result.new_files.length > 0) {
        html += `<p><strong>新文件:</strong> ${result.new_files.length} 个</p>`;
        html += '<details><summary>查看文件列表</summary><ul>';
        result.new_files.forEach(f => { html += `<li style="font-size:12px;">${f}</li>`; });
        html += '</ul></details>';
    }
    if (result.modified_files && result.modified_files.length > 0) {
        html += `<p><strong>修改文件:</strong> ${result.modified_files.length} 个</p>`;
    }
    if (result.result_texts && result.result_texts.length > 0) {
        html += `<p><strong>读取到的结果文本:</strong></p><ul>`;
        result.result_texts.forEach(t => { html += `<li style="font-size:12px;">${t}</li>`; });
        html += '</ul>';
    }
    if (result.manual_input_needed) {
        html += `<div style="margin-top:8px;padding:8px;background:rgba(255,152,0,0.1);border-radius:4px;">`;
        html += `<p style="margin:0;color:#FF9800;">⚠️ 无法自动读取温度结果。请在 Motor-CAD 中查看"输出数据 > Temperatures"标签页的温度值，然后手动输入。</p>`;
        html += `</div>`;
    }
    html += `<p style="margin-top:8px;font-size:11px;color:var(--text-tertiary);">${result.note || ''}</p>`;
    content.innerHTML = html;
}

// ============================================================
// v1.03.02: 跨设备进度同步 + 任务终止
// ============================================================
var _myWorkflowActive = false;  // 当前客户端是否在运行自己的工作流
var _progressEventSource = null;

function connectProgressHub() {
    try {
        _progressEventSource = new EventSource('/api/progress/stream');

        _progressEventSource.onmessage = function(e) {
            try {
                var data = JSON.parse(e.data);
                handleProgressHubEvent(data);
            } catch(err) {
                // ignore parse errors
            }
        };

        _progressEventSource.onerror = function() {
            // 断线重连 (5秒后)
            if (_progressEventSource) {
                _progressEventSource.close();
                _progressEventSource = null;
            }
            setTimeout(function() {
                if (!_progressEventSource) connectProgressHub();
            }, 5000);
        };
    } catch(err) {
        console.error('ProgressHub connect error:', err);
    }
}

function handleProgressHubEvent(data) {
    // 心跳: 忽略
    if (data.type === 'heartbeat') return;

    // 快照: 页面加载时获取当前状态
    if (data.type === 'hub_snapshot') {
        var st = data.state;
        if (st && st.active) {
            showRemoteBanner(st);
            // 如果有历史消息且不是自己发起的, 回放最近的几条
            if (!_myWorkflowActive && st.messages && st.messages.length > 0) {
                var recent = st.messages.slice(-5);
                recent.forEach(function(msg) {
                    if (msg.message) {
                        addMsgBar(msg.type || 'info', '[' + msg.time + '] ' + msg.message);
                    }
                });
            }
        }
        return;
    }

    // 会话开始
    if (data.type === 'hub_start') {
        if (!_myWorkflowActive) {
            showRemoteBanner(data);
            addMsgBar('info', '远程会话已启动 (' + (data.type === 'workflow' ? 'AI团队' : '批量求解') + ')');
        }
        return;
    }

    // 会话结束
    if (data.type === 'hub_end') {
        hideRemoteBanner();
        if (!_myWorkflowActive) {
            addMsgBar('info', '远程会话已结束');
        }
        return;
    }

    // 取消请求
    if (data.type === 'cancel_requested') {
        addMsgBar('warn', '收到终止请求, 正在停止...');
        return;
    }

    // 事件 (包含原始事件 + 状态快照)
    if (data.type === 'hub_event') {
        var event = data.event;
        var state = data.state;

        // 更新横幅
        if (state && state.active) {
            updateRemoteBanner(state);
        } else {
            hideRemoteBanner();
        }

        // 如果是自己发起的工作流, 跳过 (fetch reader 已处理)
        if (_myWorkflowActive) return;

        // 镜像远程事件到消息栏
        if (event) {
            if (event.type === 'step_start') {
                var stepNames = {
                    'parse_requirement': '需求分析师',
                    'generate_designs': '方案生成师',
                    'review_solutions': '评审推荐师',
                    'convergence_review': '收敛阵营',
                    'decision_gate': '决策门',
                    'generate_report': '报告撰写员'
                };
                var sName = stepNames[event.step] || event.step;
                addMsgBar('step', '[' + sName + '] ' + (event.message || ''));
                updatePipelineStep(event.step, 'running', '远程执行中');
            } else if (event.type === 'progress') {
                if (event.message) {
                    addMsgBar('info', event.message);
                }
            } else if (event.type === 'step_done') {
                var doneNames = {
                    'parse_requirement': '需求解析完成',
                    'generate_designs': '方案生成完成',
                    'review_solutions': '支持报告完成',
                    'convergence_review': '反对报告完成',
                    'decision_gate': '决策门就绪',
                    'generate_report': '报告生成完成'
                };
                var dName = doneNames[event.step] || event.step;
                if (dName) {
                    updatePipelineStep(event.step, 'done', dName);
                    addMsgBar('step', '[' + dName + ']');
                }
                // 显示结果摘要
                if (event.step === 'generate_designs' && event.results_count) {
                    addMsgBar('info', '远程: 生成 ' + event.results_count + ' 个方案');
                }
                if (event.step === 'generate_report' && event.report) {
                    addMsgBar('success', '远程: 正式报告已生成');
                    document.getElementById('agentReportDisplay').innerHTML = renderMarkdown(event.report);
                    document.getElementById('agentReportBtn').style.display = '';
                    _lastReportText = event.report;
                    _lastReportFilename = event.report_filename || '';
                }
            } else if (event.type === 'done') {
                addMsgBar('success', '远程会话完成');
            } else if (event.type === 'fatal') {
                addMsgBar('error', '远程错误: ' + (event.error || ''));
            } else if (event.type === 'cancelled') {
                addMsgBar('warn', '远程任务已终止: ' + (event.message || ''));
            }
        }
    }
}

function showRemoteBanner(state) {
    var banner = document.getElementById('remoteSessionBanner');
    if (!banner) return;
    banner.style.display = 'flex';
    updateRemoteBanner(state);
}

function updateRemoteBanner(state) {
    var text = document.getElementById('remoteSessionText');
    var step = document.getElementById('remoteSessionStep');
    var time = document.getElementById('remoteSessionTime');
    if (!text) return;

    var typeLabel = state.type === 'workflow' ? 'AI团队工作流' : '批量求解';
    text.textContent = typeLabel + ' 进行中';

    if (state.current_step) {
        var stepNames = {
            'parse_requirement': '需求分析师',
            'generate_designs': '方案生成师',
            'review_solutions': '评审推荐师',
            'convergence_review': '收敛阵营',
            'decision_gate': '决策门',
            'generate_report': '报告撰写员'
        };
        step.textContent = stepNames[state.current_step] || state.current_step;
    } else {
        step.textContent = '';
    }

    if (state.total && state.completed) {
        time.textContent = state.completed + '/' + state.total + ' (' + state.elapsed + 's)';
    } else {
        time.textContent = state.elapsed + 's';
    }
}

function hideRemoteBanner() {
    var banner = document.getElementById('remoteSessionBanner');
    if (banner) banner.style.display = 'none';
}

async function cancelRemoteTask() {
    if (!confirm('确定要终止当前正在运行的任务吗？\n(正在进行的 Motor-CAD 解算将完成当前方案后停止)')) return;

    try {
        var resp = await fetch('/api/progress/cancel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: 'user_clicked_cancel' })
        });
        var data = await resp.json();
        if (data.status === 'cancel_requested') {
            showToast('终止信号已发送', 'warn');
            addMsgBar('warn', '已发送终止信号, 正在停止任务...');
        } else {
            showToast('终止失败: ' + (data.error || ''), 'error');
        }
    } catch(e) {
        showToast('终止请求失败: ' + e.message, 'error');
    }
}

// ============================================================
// v1.03.03: 任务调度面板
// ============================================================
let _taskFilter = 'all';
let _taskPollTimer = null;

function filterTasks(status) {
    _taskFilter = status;
    loadTasks();
}

async function loadTasks() {
    try {
        const url = _taskFilter === 'all'
            ? '/api/tasks?limit=50'
            : `/api/tasks?status=${_taskFilter}&limit=50`;
        const resp = await fetch(url);
        const data = await resp.json();
        renderTaskList(data.tasks || []);

        // 更新统计信息
        const activeResp = await fetch('/api/tasks/active');
        const activeData = await activeResp.json();
        const activeCount = (activeData.tasks || []).length;
        document.getElementById('taskWorkerCount').textContent = activeCount;
        document.getElementById('taskTotalCount').textContent = data.total || 0;
        document.getElementById('taskMcadStatus').textContent = activeCount > 0 ? '忙碌' : '空闲';

        // 启动自动刷新
        if (_taskPollTimer) clearInterval(_taskPollTimer);
        const pollStatus = document.getElementById('taskPollStatus');
        if (activeCount > 0) {
            _taskPollTimer = setInterval(loadTasks, 2000);
            if (pollStatus) pollStatus.textContent = '自动刷新中 (2s)...';
        } else {
            if (pollStatus) pollStatus.textContent = '无活跃任务';
        }
    } catch(e) {
        console.error('loadTasks error:', e);
        const container = document.getElementById('taskListContainer');
        if (container) container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--error);">加载失败</div>';
    }
}

function renderTaskList(tasks) {
    const container = document.getElementById('taskListContainer');
    if (!container) return;

    if (!tasks || tasks.length === 0) {
        container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-tertiary);">暂无任务记录</div>';
        return;
    }

    let html = '';
    for (const t of tasks) {
        const statusText = {
            'queued': '排队中', 'running': '运行中', 'completed': '已完成',
            'cancelled': '已取消', 'failed': '失败'
        }[t.status] || t.status;
        const typeText = { 'solve': '求解', 'workflow': '工作流' }[t.type] || t.type;
        const progressPct = Math.round((t.progress || 0) * 100);
        const isRunning = t.status === 'running' || t.status === 'queued';
        const createdAt = t.created_at ? t.created_at.substring(11, 19) : '';
        const sourceBadge = t.source === 'trial'
            ? '<span style="color:#3b82f6;font-size:10px;">TRIAL</span>'
            : '<span style="color:#f59e0b;font-size:10px;">DEV</span>';

        // 最新日志
        const lastLog = (t.log && t.log.length > 0) ? t.log[t.log.length - 1] : null;
        const lastLogText = lastLog ? `${lastLog.time} ${lastLog.msg}` : '';

        html += `<div class="task-card task-${t.status}">
            <div class="task-card-header">
                <span class="task-card-id">#${t.id}</span>
                ${sourceBadge}
                <span class="task-card-type">${typeText}</span>
                <span class="task-card-status status-${t.status}">${statusText}</span>
                <span style="font-size:11px;color:var(--text-tertiary);margin-left:auto;">${createdAt}</span>
            </div>
            <div class="task-progress-bar">
                <div class="task-progress-fill ${t.status === 'completed' ? 'fill-completed' : ''} ${t.status === 'cancelled' ? 'fill-cancelled' : ''}"
                     style="width:${progressPct}%"></div>
            </div>
            <div class="task-card-info">
                <span>进度: ${progressPct}%</span>
                <span>${t.current_step || ''}</span>
                ${t.result_summary ? `<span>摘要: ${t.result_summary}</span>` : ''}
            </div>
            ${lastLogText ? `<div class="task-card-step">${lastLogText}</div>` : ''}
            ${isRunning ? `<div style="margin-top:8px;">
                <button class="btn btn-danger btn-sm" onclick="cancelTask('${t.id}')">终止任务</button>
            </div>` : ''}
        </div>`;
    }
    container.innerHTML = html;
}

async function cancelTask(taskId) {
    if (!confirm(`确定要终止任务 #${taskId} 吗？`)) return;
    try {
        const resp = await fetch(`/api/task/${taskId}/cancel`, { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            showToast(`任务 #${taskId} 已发送终止信号`, 'success');
            loadTasks();
        } else {
            showToast('终止失败: ' + (data.error || ''), 'error');
        }
    } catch(e) {
        showToast('终止请求失败: ' + e.message, 'error');
    }
}

// ============================================================
// v2.02.01: 认证 — 登录 / 注册 / 登出
// ============================================================
function showAuthModal(mode) {
    const modal = document.getElementById('authModal');
    if (!modal) return;
    modal.style.display = 'flex';
    if (mode === 'register') showRegisterForm();
    else showLoginForm();
}

function showLoginForm() {
    document.getElementById('loginForm').style.display = '';
    document.getElementById('registerForm').style.display = 'none';
    document.getElementById('authModalTitle').textContent = '登录';
}

function showRegisterForm() {
    document.getElementById('loginForm').style.display = 'none';
    document.getElementById('registerForm').style.display = '';
    document.getElementById('authModalTitle').textContent = '注册';
}

function closeAuthModal() {
    document.getElementById('authModal').style.display = 'none';
}

async function doLogin() {
    const username = document.getElementById('loginUsername').value.trim();
    const password = document.getElementById('loginPassword').value;
    if (!username || !password) { showToast('请填写用户名和密码', 'error'); return; }
    try {
        const resp = await fetch('/api/auth/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username, password}),
        });
        const data = await resp.json();
        if (resp.ok && data.status === 'ok') {
            if (data.token) localStorage.setItem('motorai_token', data.token);
            closeAuthModal();
            showToast('登录成功: ' + (data.user?.display_name || data.user?.username), 'success');
            loadStatus();
        } else {
            showToast('登录失败: ' + (data.error || ''), 'error');
        }
    } catch(e) {
        showToast('请求失败: ' + e.message, 'error');
    }
}

async function doRegister() {
    const username = document.getElementById('regUsername').value.trim();
    const email = document.getElementById('regEmail').value.trim();
    const password = document.getElementById('regPassword').value;
    const display_name = document.getElementById('regDisplayName').value.trim();
    if (!username || !password) { showToast('用户名和密码不能为空', 'error'); return; }
    if (password.length < 6) { showToast('密码至少 6 位', 'error'); return; }
    try {
        const resp = await fetch('/api/auth/register', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username, email, password, display_name}),
        });
        const data = await resp.json();
        if (resp.ok && data.status === 'ok') {
            if (data.token) localStorage.setItem('motorai_token', data.token);
            closeAuthModal();
            showToast('注册成功! 赠送 100 积分', 'success');
            loadStatus();
        } else {
            showToast('注册失败: ' + (data.error || ''), 'error');
        }
    } catch(e) {
        showToast('请求失败: ' + e.message, 'error');
    }
}

function doLogout() {
    localStorage.removeItem('motorai_token');
    fetch('/api/auth/logout', {method: 'POST'});
    showToast('已登出', 'info');
    setTimeout(() => location.reload(), 500);
}

async function doDevRestore() {
    try {
        await fetch('/api/auth/dev-restore', {method: 'POST'});
        showToast('已恢复自动登录', 'success');
        setTimeout(() => location.reload(), 500);
    } catch(e) {
        showToast('请求失败: ' + e.message, 'error');
    }
}

// ============================================================
// v2.02.01: 积分面板
// ============================================================
async function loadPointsPanel() {
    try {
        const [balResp, memResp, histResp] = await Promise.all([
            fetch('/api/points/balance'),
            fetch('/api/points/membership'),
            fetch('/api/points/history'),
        ]);
        const bal = await balResp.json();
        const mem = await memResp.json();
        const hist = await histResp.json();

        document.getElementById('pointsBalance').textContent = (bal.balance || 0) + ' 分';
        const memInfo = mem.membership || {};
        document.getElementById('pointsLevel').textContent = memInfo.name || '试用用户';

        // 签到状态
        const checkinBtn = document.getElementById('checkinBtn');
        if (mem.balance !== undefined) {
            const today = new Date().toISOString().slice(0, 10);
            const alreadyChecked = (hist.history || []).some(h => (h.reason || '').includes(today));
            document.getElementById('pointsCheckinStatus').textContent = alreadyChecked ? '已签到' : '未签到';
            if (checkinBtn) checkinBtn.disabled = alreadyChecked;
        }

        // 渲染历史记录
        const container = document.getElementById('pointsHistoryContainer');
        if (container) {
            const records = hist.history || [];
            if (records.length === 0) {
                container.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-tertiary);">暂无记录</div>';
            } else {
                container.innerHTML = records.map(r => {
                    const isEarn = (r.type || 'earn') === 'earn';
                    const amount = r.points > 0 ? '+' + r.points : r.points;
                    return '<div class="points-row ' + (isEarn ? 'earn' : 'spend') + '">' +
                        '<span style="font-size:12px;color:var(--text-secondary);">' + (r.reason || '') + '</span>' +
                        '<span style="font-size:11px;color:var(--text-tertiary);">' + (r.created_time || '').slice(0,16) + '</span>' +
                        '<span class="pr-amount ' + (isEarn ? 'earn' : 'spend') + '">' + amount + '</span>' +
                    '</div>';
                }).join('');
            }
        }
    } catch(e) {
        console.error('loadPointsPanel error:', e);
    }
}

async function doCheckin() {
    try {
        const resp = await fetch('/api/points/checkin', { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            showToast('签到成功! +' + data.points + ' 积分 (余额: ' + data.balance + ')', 'success');
            loadPointsPanel();
            loadStatus();
        } else {
            showToast(data.error || '签到失败', 'error');
        }
    } catch(e) {
        showToast('请求失败: ' + e.message, 'error');
    }
}

// ============================================================
// v2.02.01: 用户管理 (admin)
// ============================================================
async function loadAdminUsers() {
    try {
        const resp = await fetch('/api/admin/users');
        const data = await resp.json();
        if (!resp.ok) {
            showToast('无权访问: ' + (data.error || ''), 'error');
            return;
        }
        const users = data.users || [];
        document.getElementById('adminUserTotal').textContent = users.length;
        document.getElementById('adminUserVip').textContent = users.filter(u => ['vip','flagship'].includes(u.membership_level)).length;
        document.getElementById('adminUserActive').textContent = users.filter(u => u.status === 'active').length;

        const container = document.getElementById('adminUsersContainer');
        if (container) {
            if (users.length === 0) {
                container.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-tertiary);">暂无用户</div>';
            } else {
                container.innerHTML = users.map(u => {
                    const initials = (u.display_name || u.username || '?').charAt(0).toUpperCase();
                    var roleMap = {admin:'管理员',user:'用户',vip:'VIP',flagship:'旗舰',guest:'访客'};
                    var roleBadge = '<span class="user-role ' + (u.role || 'guest') + '" style="font-size:9px;">' + (roleMap[u.role] || u.role) + '</span>';
                    var levelBadge = '<span class="user-role" style="font-size:9px;background:#3b82f6;">' + (u.membership_level || 'trial') + '</span>';
                    return '<div class="admin-user-card">' +
                        '<div class="au-avatar">' + initials + '</div>' +
                        '<div class="au-info">' +
                            '<div class="au-name">' + (u.display_name || u.username) + ' ' + roleBadge + ' ' + levelBadge + '</div>' +
                            '<div class="au-meta">ID: ' + u.id + ' | ' + (u.email || '无邮箱') + ' | 积分: ' + (u.points_balance || 0) + ' | 注册: ' + (u.created_time || '').slice(0,10) + ' | 状态: ' + (u.status || 'active') + '</div>' +
                        '</div>' +
                        '<div class="au-actions">' +
                            '<select onchange="updateUserRole(' + u.id + ', this.value)" style="font-size:11px;padding:2px 4px;border-radius:4px;background:var(--bg-primary);color:var(--text-primary);border:1px solid var(--border);">' +
                                '<option value="user" ' + (u.role==='user'?'selected':'') + '>用户</option>' +
                                '<option value="vip" ' + (u.role==='vip'?'selected':'') + '>VIP</option>' +
                                '<option value="flagship" ' + (u.role==='flagship'?'selected':'') + '>旗舰</option>' +
                                '<option value="admin" ' + (u.role==='admin'?'selected':'') + '>管理员</option>' +
                            '</select>' +
                            '<button class="btn btn-sm" onclick="adjustPoints(' + u.id + ')" title="调整积分">+/-</button>' +
                        '</div>' +
                    '</div>';
                }).join('');
            }
        }
    } catch(e) {
        console.error('loadAdminUsers error:', e);
    }
}

async function updateUserRole(userId, newRole) {
    try {
        const resp = await fetch('/api/admin/user/' + userId, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({role: newRole}),
        });
        const data = await resp.json();
        if (resp.ok) {
            showToast('用户 #' + userId + ' 角色已更新为 ' + newRole, 'success');
        } else {
            showToast('更新失败: ' + (data.error || ''), 'error');
        }
    } catch(e) {
        showToast('请求失败: ' + e.message, 'error');
    }
}

async function adjustPoints(userId) {
    var amount = prompt('为用户 #' + userId + ' 调整积分\n正数=增加, 负数=减少');
    if (amount === null) return;
    var n = parseInt(amount);
    if (isNaN(n)) { showToast('请输入数字', 'error'); return; }
    var reason = prompt('调整原因:', '管理员调整') || '管理员调整';
    try {
        const resp = await fetch('/api/admin/user/' + userId + '/points', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({amount: n, reason: reason}),
        });
        const data = await resp.json();
        if (resp.ok) {
            showToast('积分调整成功, 新余额: ' + data.new_balance, 'success');
            loadAdminUsers();
        } else {
            showToast('调整失败: ' + (data.error || ''), 'error');
        }
    } catch(e) {
        showToast('请求失败: ' + e.message, 'error');
    }
}
