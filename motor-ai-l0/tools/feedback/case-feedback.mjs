/**
 * 社区案例反馈报告器（case feedback reporter）
 * ---------------------------------------------------------------------------
 * 目的：让用户把自己的工况**结论**（而非设计明细）反馈给作者，形成闭环。
 *
 * 设计红线（改动前请先读 README「社区反馈」一节）：
 *  1. **本模块只导出 issue 草稿字符串**，不导出 params / matrix / 任何设计明细。
 *     脱敏不靠"过滤"，靠"不产出"——调用方在结构上就拿不到 L1 明细。
 *  2. 只回传 **L2 结论** 与 **L3 元统计**：
 *       L1 明细（stator_od=425, l0_eff=93.73）—— 高敏感，永不回传
 *       L2 结论（"OD≥900mm 区间可用率 80.4%"）—— 低敏感，回传
 *       L3 元统计（V08 failed×1760）—— 极低敏感，回传
 *  3. 草稿生成后必须过 **脱敏自检**（assertNoSensitiveData）；任一命中即抛错、
 *     不返回草稿。用户敢提交的前提是能自证清白。
 *  4. **自动提交永不实现**：只生成草稿，由人工粘贴进 issue 并署名。
 *
 * 用法：
 *   import { buildCaseFeedbackDraft } from './tools/feedback/case-feedback.mjs'
 *   const draft = buildCaseFeedbackDraft({ cases: [{ label: 'B4', spec: {...} }] })
 *   console.log(draft)   // 直接粘贴进 issue
 */

import { buildParamMatrix } from '../l0/param-matrix.mjs'
import { validateDesign } from '../../lib/design-rules.mjs'
import { RULE_CATALOG } from '../../lib/design-rules.mjs'

/** 脱敏自检规则表：任一命中即拒绝输出草稿。 */
const SENSITIVE_RULES = [
  { id: 'S01', name: '几何尺寸', re: /\b(?:stator_od|stator_id|core_length|yoke_thickness)\s*[=:]\s*[\d.]+/i },
  { id: 'S02', name: '槽数', re: /\b(?:slots?_stator|slots?_rotor|Q[se])\s*[=:]\s*\d+/i },
  { id: 'S03', name: '匝数/线规', re: /\b(?:turns?_per_coil|wire_dia|n_strands)\s*[=:]\s*[\d.]+/i },
  { id: 'S04', name: '电流/电密', re: /\b(?:peak_current|line_current|current_density| Irms)\s*[=:]\s*[\d.]+/i },
  { id: 'S05', name: '效率/损耗数值', re: /\b(?:l0_eff|l1_eff|efficiency|total_loss|copper_loss|iron_loss)\s*[=:]\s*[\d.]+/i },
  { id: 'S06', name: '客户/项目标识', re: /(?:客户|项目|单位|company|customer|project)\s*[=:：]\s*\S+/i },
  { id: 'S07', name: '本机绝对路径', re: /[A-Za-z]:[\\/](?:Users|MotorDesign|home)\S*/ },
  { id: 'S08', name: 'UNC/网络路径', re: /\\\\[\w.-]+\\[^\s]+/ },
  { id: 'S09', name: '哈希/长串 token', re: /\b[0-9a-f]{16,}\b/i },
  { id: 'S10', name: '邮箱/手机号', re: /[\w.+-]+@[\w-]+\.[\w.]+|\b1[3-9]\d{9}\b/ },
]

/**
 * 脱敏自检。命中任一规则即抛错。
 * ⚠ 前置：扫描前必须先剔除"本报告不含…"这类**自述句**，否则自检规则会与自述
 *   文本互撞（原型首版即因此误报一次）。故这里只扫正文，自述句不在正文。
 */
export function assertNoSensitiveData(draft) {
  const hits = []
  for (const r of SENSITIVE_RULES) {
    const m = draft.match(r.re)
    if (m) hits.push(`${r.id}(${r.name}): ${m[0]}`)
  }
  if (hits.length) {
    throw new Error(
      `脱敏自检未通过（${hits.length}/${SENSITIVE_RULES.length} 项命中），草稿不予输出：\n  - `
      + hits.join('\n  - '),
    )
  }
  return true
}

/** 脱敏自检的独立可测入口（供 verify.mjs 断言用）。 */
export function sensitivityRules() {
  return SENSITIVE_RULES.map((r) => ({ id: r.id, name: r.name, re: r.re }))
}

// ── 分箱（只输出分箱标签与计数，绝不输出行明细）────────────────────────

function binBy(rows, keyFn, edges) {
  const bins = edges.map((e) => ({ label: e.label, n: 0, ok: 0 }))
  for (const r of rows) {
    const v = keyFn(r)
    if (v == null || Number.isNaN(v)) continue
    const i = edges.findIndex((e) => v >= e.lo && v < e.hi)
    if (i < 0) continue
    bins[i].n += 1
    if (r.ok) bins[i].ok += 1
  }
  return bins.map((b) => ({ ...b, rate: b.n ? b.ok / b.n : null }))
}

const SPEED_EDGES = [
  { label: '≤900rpm', lo: 0, hi: 900 },
  { label: '900~1400rpm', lo: 900, hi: 1400 },
  { label: '1400~1500rpm', lo: 1400, hi: 1500 },
  { label: '1500~3000rpm', lo: 1500, hi: 3000 },
  { label: '>3000rpm', lo: 3000, hi: Infinity },
]
const OD_EDGES = [
  { label: 'OD≤300mm', lo: 0, hi: 300 },
  { label: '300~600mm', lo: 300, hi: 600 },
  { label: '600~900mm', lo: 600, hi: 900 },
  { label: 'OD≥900mm', lo: 900, hi: Infinity },
]
const POWER_EDGES = [
  { label: '≤30kW', lo: 0, hi: 30 },
  { label: '30~160kW', lo: 30, hi: 160 },
  { label: '160~400kW', lo: 160, hi: 400 },
  { label: 'P≥400kW', lo: 400, hi: Infinity },
]

/**
 * 生成社区反馈 issue 草稿。
 * @param {object} opts
 * @param {Array<{label:string, spec:object, note?:string}>} opts.cases 待反馈的工况档
 * @param {number} [opts.count=20] 每个工况生成的候选数
 * @param {string} [opts.pluginVersion] 插件版本，落款用
 * @param {boolean} [opts.dryRun=false] 只跑统计不产出草稿（自检用）
 * @returns {string} issue 草稿（Markdown，可直接粘贴）
 */
export function buildCaseFeedbackDraft(opts = {}) {
  const { cases = [], count = 20, pluginVersion = 'unknown', dryRun = false } = opts

  // ── 聚合（L3 元统计）──
  const ruleHits = new Map()          // rule -> {failed, warning}
  const rows = []                     // 聚合用中间态（不含 params，仅统计维度）
  let totalFeasible = 0
  let totalRows = 0

  for (const c of cases) {
    let matrix = []
    try {
      matrix = buildParamMatrix({ ...c.spec, count })?.matrix ?? []
    } catch (e) {
      matrix = []
    }
    for (const p of matrix) {
      let v = null
      try {
        v = validateDesign(p)
      } catch {
        continue
      }
      totalRows += 1
      // 可行口径对齐插件三步链：status 三态中只有 'failed' 才剔除，
      // 'warning' 计入可行（实测 120 行 → 剔 54 → 66 可行，即 55%）。
      // 若误用 === 'passed'，会把带提示的正常方案全算成不可行（实测 0%），断崖检测随之失效。
      const ok = v?.status !== 'failed'
      if (ok) totalFeasible += 1
      rows.push({
        ok,
        speed: Number(p.speed ?? 0),
        od: Number(p.stator_od ?? 0),
        power: Number(c.spec?.power_kw ?? 0),
      })
      for (const it of v?.issues ?? []) {
        const r = it.rule || 'UNKNOWN'
        if (!ruleHits.has(r)) ruleHits.set(r, { failed: 0, warning: 0 })
        const slot = ruleHits.get(r)
        if (it.level === 'failed') slot.failed += 1
        else if (it.level === 'warning') slot.warning += 1
      }
    }
  }

  const feasibleRate = totalRows ? totalFeasible / totalRows : null
  const ruleRows = [...ruleHits.entries()]
    .map(([rule, s]) => ({ rule, ...s, total: s.failed + s.warning }))
    .sort((a, b) => b.total - a.total)
  const speedBins = binBy(rows, (r) => r.speed, SPEED_EDGES)
  const odBins = binBy(rows, (r) => r.od, OD_EDGES)
  const powerBins = binBy(rows, (r) => r.power, POWER_EDGES)

  // ── 断崖检测（L2 结论）：相邻分箱可行率骤降即定位 ---
  // 判据用**双条件**而非单一绝对阈值：单用绝对跌幅会把小基数噪声当断崖，
  // 单用相对跌幅会把"两箱都接近 0"误判为断崖。
  // 实测：1400~1500rpm 66.7% → 1500~3000rpm 33.3%（绝对 33.3pt / 相对 50%）应判为断崖。
  const CLIFF_ABS_DROP = 0.30
  const CLIFF_REL_RATIO = 0.60
  const cliffs = []
  for (let i = 1; i < speedBins.length; i += 1) {
    const a = speedBins[i - 1]
    const b = speedBins[i]
    if (a.rate == null || b.rate == null || b.n < 3) continue
    const drop = a.rate - b.rate
    if (drop >= CLIFF_ABS_DROP && b.rate <= a.rate * CLIFF_REL_RATIO) {
      cliffs.push({ from: a.label, to: b.label, drop, fromRate: a.rate, toRate: b.rate })
    }
  }

  // ── 规则目录名（只取名字，不含参数）──
  const catalogName = (id) => RULE_CATALOG.find((r) => r.id === id)?.name ?? id

  const pct = (x) => (x == null ? '—' : `${(x * 100).toFixed(1)}%`)
  const ruleTable = [
    '| 规则 | 含义 | failed | warning |',
    '|---|---|---:|---:|',
    ...ruleRows.map(
      (r) => `| ${r.rule} | ${catalogName(r.rule)} | ${r.failed} | ${r.warning} |`,
    ),
  ].join('\n')
  const binTable = (title, bins) => [
    `| ${title} | 样本 | 可行率 |`,
    '|---|---:|---:|',
    ...bins.map((b) => `| ${b.label} | ${b.n} | ${pct(b.rate)} |`),
  ].join('\n')

  const cliffLines = cliffs.length
    ? ['', '**断崖定位**', ...cliffs.map(
      (c) =>
        `- 可行率在 ${c.from} → ${c.to} 之间从 ${(c.fromRate * 100).toFixed(1)}% 跌至 `
        + `${(c.toRate * 100).toFixed(1)}%（跌幅 ${(c.drop * 100).toFixed(1)}pt），`
        + '疑似与该区间的某条物理判据或参数分支有关，请核实是否需放宽/修正。',
    )]
    : ['', '**断崖定位**', '- 未检出相邻区间的可行率断崖。']
  // 断崖比较有前提：两侧都要有可行样本。全灭时比较无意义（0 vs 0 不构成断崖）。
  if (totalFeasible === 0) {
    cliffLines.splice(1, cliffLines.length,
      '- ⚠ 本批可行样本为 0，断崖检测不具判别力；建议补入对照工况（不同转速/功率档）后重跑。')
  }

  const draft = [
    '<!-- 本草稿由 dsh-motor-ai-l0 的案例反馈报告器生成（只含结论，不含设计明细） -->',
    '',
    '### 复现工况',
    ...cases.map((c) => `- ${c.label}（规格档位，参数由插件按用户输入生成）`),
    '',
    '### 汇总结论',
    `- 累计校验 ${totalRows} 个候选，整体可行率 **${pct(feasibleRate)}**`,
    '',
    '**规则命中画像（L3 元统计）**',
    ruleTable,
    '',
    '**转速分箱（L2 结论）**',
    binTable('转速区间', speedBins),
    '',
    '**机座分箱（L2 结论）**',
    binTable('定子外径区间', odBins),
    '',
    '**功率分箱（L2 结论）**',
    binTable('功率区间', powerBins),
    ...cliffLines,
    '',
    '### 环境',
    `- 插件版本：${pluginVersion}`,
    '',
    '### 隐私声明',
    '- 本报告**只含统计结论**，不含任何设计明细（几何/槽数/匝数/电流/效率数值均已排除）。',
    '- 报告生成器内置脱敏自检，命中任一敏感模式即拒绝输出。',
    '',
    '_由 `tools/feedback/case-feedback.mjs` 生成。请人工确认后再提交，勿直接自动上报。_',
  ].join('\n')

  if (dryRun) {
    return { totalRows, totalFeasible, feasibleRate, ruleRows, cliffs, draft }
  }

  assertNoSensitiveData(draft)
  return draft
}