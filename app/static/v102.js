/* v1.02.01 辩证制衡扩展 — 沙盘推演 + 战略雷达 + 飞轮
   依赖 app.js 的 showToast / renderMarkdown / fetch 等基础函数 */

// ============================================================
// 沙盘推演 (蒙特卡洛)
// ============================================================
async function loadSandboxCandidates() {
    try {
        const resp = await fetch('/api/results/top?n=5');
        const data = await resp.json();
        const results = data.results || [];
        if (results.length === 0) {
            document.getElementById('sandboxCandidates').style.display = 'none';
            showToast('暂无方案数据，请先求解', 'warn');
            return;
        }
        document.getElementById('sandboxCandidates').style.display = 'block';
        const t = document.getElementById('sandboxTable');
        let html = '<thead><tr><th>#</th><th>外径</th><th>极数</th><th>效率%</th><th>温升K</th><th>转矩</th></tr></thead><tbody>';
        results.forEach((r, i) => {
            html += '<tr><td>' + (i + 1) + '</td><td>' + (r.stator_od || '-') + '</td><td>' + (r.poles || '-') + '</td><td>' + (r.efficiency || 0).toFixed(1) + '</td><td>' + (r.max_temp || 0).toFixed(1) + '</td><td>' + (r.torque || 0).toFixed(1) + '</td></tr>';
        });
        html += '</tbody>';
        t.innerHTML = html;
        window._sandboxCandidates = results;
    } catch (e) {
        showToast('加载候选方案失败: ' + e.message, 'error');
    }
}

async function runSandbox() {
    const candidates = window._sandboxCandidates || [];
    if (candidates.length === 0) {
        showToast('请先加载候选方案', 'warn');
        return;
    }
    const nTrials = parseInt(document.getElementById('sandboxTrials').value);
    document.getElementById('sandboxRunBtn').disabled = true;
    document.getElementById('sandboxRunBtn').textContent = '推演中...';
    document.getElementById('sandboxResults').style.display = 'block';
    document.getElementById('sandboxSummary').innerHTML = '<div class="empty-hint">蒙特卡洛模拟运行中...</div>';

    try {
        const resp = await fetch('/api/sandbox/simulate', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ solutions: candidates, specs: {}, n_trials: nTrials })
        });
        const data = await resp.json();
        const result = data.result;
        if (result) {
            renderSandboxResults(Array.isArray(result) ? result : [result], nTrials);
        }
    } catch (e) {
        showToast('推演失败: ' + e.message, 'error');
    } finally {
        document.getElementById('sandboxRunBtn').disabled = false;
        document.getElementById('sandboxRunBtn').textContent = '开始推演';
    }
}

function renderSandboxResults(sims, nTrials) {
    const avgFail = sims.reduce((s, r) => s + (r.failure_rate || 0), 0) / sims.length;
    const worst = sims.reduce((m, r) => Math.max(m, r.failure_rate || 0), 0);
    const best = sims.reduce((m, r) => Math.min(m, r.failure_rate || 0), 0);
    const failClass = avgFail < 20 ? 'sb-fail-low' : (avgFail < 50 ? 'sb-fail-mid' : 'sb-fail-high');
    document.getElementById('sandboxSummary').innerHTML =
        '<div class="sb-card"><div class="sb-label">平均失败率</div><div class="sb-value ' + failClass + '">' + avgFail.toFixed(1) + '%</div></div>' +
        '<div class="sb-card"><div class="sb-label">最稳健方案</div><div class="sb-value sb-fail-low">' + best.toFixed(1) + '%</div></div>' +
        '<div class="sb-card"><div class="sb-label">最脆弱方案</div><div class="sb-value sb-fail-high">' + worst.toFixed(1) + '%</div></div>';

    let html = '<p style="margin-bottom:12px;">共 ' + sims.length + ' 个方案 × ' + nTrials + ' 次推演 (参数扰动 ±5%)</p>';
    sims.forEach(sim => {
        const idx = (sim.solution_index !== undefined ? sim.solution_index : 0) + 1;
        const verdict = sim.verdict || '未知';
        const modes = sim.failure_modes || {};
        const modeKeys = Object.keys(modes);
        const modesStr = modeKeys.length > 0
            ? modeKeys.map(m => m + '(' + modes[m] + ')').join(', ')
            : '无';
        const effP = (sim.percentiles && sim.percentiles.efficiency) || {};
        const tempP = (sim.percentiles && sim.percentiles.max_temp) || {};
        html += '<div class="sim-row verdict-' + verdict + '">' +
            '<div style="font-weight:500;margin-bottom:4px;">方案' + idx + ': 失败率 ' + sim.failure_rate + '% [' + verdict + ']</div>' +
            '<div>效率 P5/P50/P95 = ' + (effP.p5 || '-') + '/' + (effP.p50 || '-') + '/' + (effP.p95 || '-') + '%</div>' +
            '<div>温升 P95 = ' + (tempP.p95 || '-') + 'K</div>' +
            '<div>失败模式: ' + modesStr + '</div>' +
            '<div style="margin-top:4px;color:var(--text-secondary);">' + (sim.recommendation || '') + '</div>' +
            '</div>';
    });
    document.getElementById('sandboxDetail').innerHTML = html;
}

// ============================================================
// 战略雷达
// ============================================================
async function loadRadar() {
    try {
        const fwResp = await fetch('/api/flywheel/status');
        const evoResp = await fetch('/api/evolution/dashboard');
        const fw = await fwResp.json();
        const evo = await evoResp.json();
        document.getElementById('radarProjects').textContent = fw.total_designs || 0;
        document.getElementById('radarAdopted').textContent = fw.adopted || 0;
        document.getElementById('radarSkills').textContent = fw.skill_templates_count || 0;
        document.getElementById('radarGaps').textContent = fw.failure_patterns_count || 0;
    } catch (e) {
        showToast('加载雷达数据失败', 'warn');
    }
}

async function runRadarScan() {
    showToast('战略雷达扫描中...', 'info');
    try {
        const fwResp = await fetch('/api/flywheel/status');
        const evoResp = await fetch('/api/evolution/dashboard');
        const topResp = await fetch('/api/results/top?n=10');
        const fw = await fwResp.json();
        const evo = await evoResp.json();
        const topData = await topResp.json();
        const tops = topData.results || [];

        const opps = [];
        const adoptRate = fw.adoption_rate || 0;
        if (adoptRate > 0 && adoptRate < 50) {
            opps.push({ title: '采纳率偏低', desc: '当前采纳率 ' + adoptRate + '%，AI 生成方案质量待提升。建议聚焦高频应用场景，累积技能模板。' });
        }
        const skills = evo.skill_templates || [];
        const apps = {};
        skills.forEach(s => { apps[s.application] = (apps[s.application] || 0) + 1; });
        const coveredApps = Object.keys(apps);
        if (coveredApps.length < 3) {
            opps.push({ title: '应用场景覆盖不足', desc: '仅覆盖 ' + (coveredApps.join('、') || '无') + '。可拓展叉车/无人机/家电等场景，分散风险。' });
        }
        if (tops.length > 0) {
            const highEff = tops.filter(t => (t.efficiency || 0) > 93);
            if (highEff.length > 0) {
                opps.push({ title: '高效率方案储备', desc: '有 ' + highEff.length + ' 个效率>93%的方案。这类高性能设计可打包成技能模板，复用到相似需求。' });
            }
            const lowTemp = tops.filter(t => (t.max_temp || 999) < 70);
            if (lowTemp.length > 0) {
                opps.push({ title: '低温升设计优势', desc: lowTemp.length + ' 个方案温升<70K，热余量大。适合对可靠性要求高的客户，可作为差异化卖点。' });
            }
        }

        const gaps = [];
        const failures = fw.failure_patterns_count || 0;
        if (failures > 0) {
            gaps.push({ title: failures + ' 个失败特征待复盘', desc: '建议定期回顾被拒方案，提炼"什么不该做"的规则，喂给收敛阵营的逆向评审师。' });
        }
        const rules = evo.design_rules || [];
        if (rules.length < 5) {
            gaps.push({ title: '设计规则库薄弱', desc: '仅 ' + rules.length + ' 条规则。需更多历史项目喂养，规则越多，方案生成师越能避开已知坑。' });
        }
        if ((fw.ai_intervention_rate || 0) < 50) {
            gaps.push({ title: 'AI 接管率低', desc: '当前 ' + (fw.ai_intervention_rate || 0) + '%。决策仍高度依赖人工，建议提升技能模板覆盖率以解放决策精力。' });
        }

        const oppHtml = opps.length > 0
            ? opps.map(o => '<div class="radar-item"><div class="item-title">' + o.title + '</div><div class="item-desc">' + o.desc + '</div></div>').join('')
            : '<div class="empty-hint">暂无明显机会信号</div>';
        const gapHtml = gaps.length > 0
            ? gaps.map(g => '<div class="radar-item"><div class="item-title">' + g.title + '</div><div class="item-desc">' + g.desc + '</div></div>').join('')
            : '<div class="empty-hint">能力覆盖良好</div>';
        document.getElementById('radarOpportunities').innerHTML = oppHtml;
        document.getElementById('radarGapsList').innerHTML = gapHtml;
        document.getElementById('radarProjects').textContent = fw.total_designs || 0;
        document.getElementById('radarAdopted').textContent = fw.adopted || 0;
        document.getElementById('radarSkills').textContent = fw.skill_templates_count || 0;
        document.getElementById('radarGaps').textContent = failures;
        showToast('雷达扫描完成', 'success');
    } catch (e) {
        showToast('雷达扫描失败: ' + e.message, 'error');
    }
}
