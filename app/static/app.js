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
}

// ============================================================
// 系统状态
// ============================================================
async function loadStatus() {
    try {
        const resp = await fetch('/api/status');
        const data = await resp.json();

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
        if (data.cloud.configured) {
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
        btn.disabled = false;
        btn.textContent = '开始求解';
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
                <th colspan="5">结构参数</th>
                <th colspan="4">性能参数</th>
                <th colspan="4">电磁参数</th>
                <th colspan="3">损耗 (W)</th>
                <th rowspan="2">时间</th>
                <th rowspan="2">操作</th>
            </tr>
            <tr>
                <th>外径(mm)</th>
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
// 工具函数
// ============================================================
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
    document.getElementById('gateReadPro').checked = false;
    document.getElementById('gateReadCon').checked = false;
    document.getElementById('agentRunBtn').disabled = true;
    document.getElementById('agentRunBtn').textContent = '运行中...';

    try {
        const resp = await fetch('/api/workflow/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                raw_text: text, solve_count: solveCount,
                use_cloud_review: useCloudReview, use_cloud_report: useCloudReport
            })
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
        document.getElementById('agentRunBtn').disabled = false;
        document.getElementById('agentRunBtn').textContent = '启动 AI 团队';
    }
}

function handleWorkflowEvent(data) {
    if (data.type === 'start') {
        showToast('AI 团队已启动 (辩证双轨)', 'info');
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
            // 双轨: generate_designs 完成后同时启动 review 和 convergence
            if (data.step === 'generate_designs' && info.next) {
                updatePipelineStep('review_solutions', 'running', '执行中');
                updatePipelineStep('convergence_review', 'running', '收敛阵营执行中');
            } else if (info.next && !(data.step === 'review_solutions' || data.step === 'convergence_review')) {
                updatePipelineStep(info.next, 'running', '执行中');
            }
        }

        if (data.step === 'parse_requirement' && data.specs) {
            document.getElementById('agentResults').style.display = 'block';
            document.getElementById('agentSpecsDisplay').textContent = JSON.stringify(data.specs, null, 2);
        }
        if (data.step === 'review_solutions' && data.review) {
            document.getElementById('agentReviewDisplay').innerHTML = renderMarkdown(data.review);
        }
        if (data.step === 'convergence_review' && data.con_report) {
            document.getElementById('agentConReportDisplay').innerHTML = renderMarkdown(data.con_report);
        }
        if (data.step === 'decision_gate') {
            document.getElementById('decisionGate').style.display = 'block';
            const ready = data.ready_for_confirm;
            const gs = document.getElementById('gateStatus');
            if (ready) {
                gs.textContent = '双报告已齐备，请阅读后确认';
                gs.className = 'gate-status gate-ready';
            } else {
                gs.textContent = '报告未齐备';
                gs.className = 'gate-status gate-pending';
            }
        }
        if (data.step === 'generate_report' && data.report) {
            document.getElementById('agentReportDisplay').innerHTML = renderMarkdown(data.report);
            if (data.report_filepath) {
                const btn = document.getElementById('agentReportBtn');
                btn.style.display = 'inline-block';
                btn.dataset.filepath = data.report_filepath;
            }
        }
    } else if (data.type === 'step_error') {
        updatePipelineStep(data.step, 'error', '错误: ' + (data.error || '').slice(0, 50));
        showToast('步骤错误: ' + data.step, 'warn');
    } else if (data.type === 'done') {
        showToast('辩证制衡工作流完成', 'success');
    } else if (data.type === 'fatal') {
        showToast('致命错误: ' + data.error, 'error');
    }
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
