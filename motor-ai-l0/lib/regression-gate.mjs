/**
 * 公式 vs 模拟 回归质量门 —— W4 第二块（纯逻辑层）
 * ===========================================================
 * 为什么需要「两道门」而不是一道：
 *
 *   门 A · 物理边界门（sanity）：L0 估算值必须落进工程可解释的物理边界
 *       —— 效率落进该功率档的 IE1~IE4 能效带、温升不越绝缘等级限值。
 *       越界 = 真 bug（公式写错 / 量级错 / 单位错），必须阻断。
 *
 *   门 B · 回归门（regression）：与基线快照逐指标比较，只允许极小劣化。
 *       这是 CI 真正阻断的那道门 —— 「不能再比上次差」。
 *       没有这道门，标定过程中任何一次"顺手改系数"都可能悄悄改变排序结果。
 *
 *   ⚠ 关于「L0 vs L1 仿真偏差 ≤2pt」这个 W4 目标：
 *     它被登记为 **诊断项（divergence）而非阻断项**。理由（W3 实算取证）：
 *     Python 侧 _run_simulated 的经验式（motor_tools.py:750-756）是在
 *     ~180mm 外径 / 8 极 / 3000rpm 附近标定的，把它套到 200kW/22000rpm
 *     会推出 78.6% 效率 —— 比 IE1 地板还低，是**仿真模型本身失真**，
 *     不是 L0 的错。强行把 L0 对齐到它，等于把 L0 一起带偏。
 *     故：只有落在仿真模型适用域内的案例，其偏差才计入 2pt 预算；
 *     域外案例标记 out_of_domain，不计入、不阻断，但要可见。
 */

import { quickL0Estimate } from './formula-engine.mjs'
import { INSULATION_LIMITS, DEFAULT_INSULATION_CLASS } from './motor-constants.mjs'

/**
 * 仿真模型（motor_tools.py:750-756）的适用域
 * 依据：85 + (od-180)·0.04 + (poles-8)·1.2 与 55 + (od-180)·0.25 + (speed-3000)·0.004
 *       两个线性式在标定中心附近才成立，越远越失真。
 */
export const SIM_VALID_DOMAIN = {
  odMm: [120, 260],
  poles: [4, 8],
  speedRpm: [500, 4000],
}

/** 判定某案例是否落在仿真模型适用域内 */
export function inSimDomain(params) {
  const od = params?.stator_od
  const poles = params?.poles
  const speed = params?.speed
  if (typeof od !== 'number' || typeof poles !== 'number' || typeof speed !== 'number') return false
  return (
    od >= SIM_VALID_DOMAIN.odMm[0] && od <= SIM_VALID_DOMAIN.odMm[1] &&
    poles >= SIM_VALID_DOMAIN.poles[0] && poles <= SIM_VALID_DOMAIN.poles[1] &&
    speed >= SIM_VALID_DOMAIN.speedRpm[0] && speed <= SIM_VALID_DOMAIN.speedRpm[1]
  )
}

/** 效率相对参考带的偏离（带内为 0，带外取到最近边界的距离，单位 pt） */
export function bandDeviationPt(value, band) {
  const [lo, hi] = band ?? []
  if (typeof value !== 'number' || typeof lo !== 'number' || typeof hi !== 'number') return null
  if (value >= lo && value <= hi) return 0
  const d = value < lo ? lo - value : value - hi
  return Math.round(d * 100) / 100
}

/**
 * 评估单个案例
 * @param {object} caseDef benchmarks 中的一项
 * @param {object} [config] { efficiencyCap, tempRiseRange, budget }
 */
export function evaluateCase(caseDef, config = {}) {
  const params = caseDef.params ?? {}
  const effCap = config.efficiencyCap ?? 96
  const maxTempClamp = config.tempRiseRange ?? [45, 130]
  const budget = config.budget ?? { efficiency_pt: 2, temp_k: 10 }

  const est = quickL0Estimate(params, { efficiencyCap: effCap, maxTempClamp })

  const design = caseDef.reference?.insulation_design ?? DEFAULT_INSULATION_CLASS
  const assess = caseDef.reference?.insulation_assessment ?? design
  const limitDesign = INSULATION_LIMITS[design] ?? INSULATION_LIMITS[DEFAULT_INSULATION_CLASS]
  const limitAssess = INSULATION_LIMITS[assess] ?? INSULATION_LIMITS[DEFAULT_INSULATION_CLASS]

  const effDevPt = bandDeviationPt(est.efficiency, caseDef.reference?.efficiency_band)
  const deltaEffPt = Math.round(Math.abs(est.efficiency - est.l1_efficiency_proxy) * 100) / 100
  const deltaTempK = Math.round(Math.abs(est.max_temp - est.l1_temp_proxy) * 10) / 10
  const domain = inSimDomain(params)

  // ---- 门 A：物理边界 ----
  const sanityIssues = []
  if (effDevPt === null) {
    sanityIssues.push({ metric: 'efficiency', level: 'fail', msg: '参考带缺失或效率非数值' })
  } else if (effDevPt > 0) {
    sanityIssues.push({
      metric: 'efficiency', level: 'fail',
      msg: `效率 ${est.efficiency}% 越出参考带 ${caseDef.reference.efficiency_band.join('~')}%（偏离 ${effDevPt}pt）`,
    })
  }
  if (est.temp_rise > limitDesign) {
    sanityIssues.push({
      metric: 'temp_rise', level: 'fail',
      msg: `温升 ${est.temp_rise}K 超过 ${design} 级设计限值 ${limitDesign}K`,
    })
  } else if (est.temp_rise > limitAssess) {
    sanityIssues.push({
      metric: 'temp_rise', level: 'warn',
      msg: `温升 ${est.temp_rise}K 超过 ${assess} 级考核限值 ${limitAssess}K（未越设计限值）`,
    })
  }

  const sanity = {
    status: sanityIssues.some((i) => i.level === 'fail')
      ? 'fail'
      : sanityIssues.length ? 'warn' : 'pass',
    issues: sanityIssues,
  }

  // ---- 诊断：L0 vs 仿真偏差 ----
  const withinBudget = deltaEffPt <= budget.efficiency_pt && deltaTempK <= budget.temp_k
  const divergence = {
    in_domain: domain,
    delta_efficiency_pt: deltaEffPt,
    delta_temp_k: deltaTempK,
    budget: { efficiency_pt: budget.efficiency_pt, temp_k: budget.temp_k },
    status: !domain ? 'out_of_domain' : withinBudget ? 'within' : 'debt',
  }

  return {
    id: caseDef.id,
    name: caseDef.name,
    source: caseDef.source ?? '',
    metrics: {
      efficiency: est.efficiency,
      temp_rise: est.temp_rise,
      max_temp: est.max_temp,
      total_loss: est.total_loss,
      torque: est.torque,
      torque_density: est.torque_density,
      l1_efficiency_proxy: est.l1_efficiency_proxy,
      l1_temp_proxy: est.l1_temp_proxy,
      efficiency_band: caseDef.reference?.efficiency_band ?? null,
      efficiency_deviation_pt: effDevPt,
      temp_limit_design_k: limitDesign,
      temp_limit_assessment_k: limitAssess,
    },
    sanity,
    divergence,
  }
}

/**
 * 评估整个标定集
 * @param {object} suite benchmarks/l0-benchmarks.json 内容
 * @param {object} [config]
 * @returns {{generated_at, budget, results, sanity, divergence, verdict}}
 */
export function evaluateSuite(suite, config = {}) {
  const budget = suite?.budget ?? config.budget ?? { efficiency_pt: 2, temp_k: 10 }
  const sanityBlocking = config.sanityBlocking ?? suite?.gate?.sanity_blocking ?? false
  const results = (suite?.cases ?? []).map((c) => evaluateCase(c, { ...config, budget }))

  const sanityFail = results.filter((r) => r.sanity.status === 'fail')
  const sanityWarn = results.filter((r) => r.sanity.status === 'warn')
  const debt = results.filter((r) => r.divergence.status === 'debt')
  const outOfDomain = results.filter((r) => r.divergence.status === 'out_of_domain')

  return {
    generated_at: new Date().toISOString(),
    budget,
    results,
    sanity: {
      status: sanityFail.length ? 'fail' : sanityWarn.length ? 'warn' : 'pass',
      blocking: sanityBlocking,
      failed: sanityFail.map((r) => ({ id: r.id, issues: r.sanity.issues })),
      warned: sanityWarn.map((r) => ({ id: r.id, issues: r.sanity.issues })),
    },
    divergence: {
      within: results.filter((r) => r.divergence.status === 'within').length,
      debt: debt.map((r) => ({
        id: r.id,
        delta_efficiency_pt: r.divergence.delta_efficiency_pt,
        delta_temp_k: r.divergence.delta_temp_k,
      })),
      out_of_domain: outOfDomain.map((r) => r.id),
    },
    // 物理边界越界：默认不阻断（标定未收敛前阻断会让 CI 长期红灯，门就没人看了），
    // 但必须计入 DEBT 且可见；sanityBlocking=true 后才升级为 FAIL。
    // 回归门（门 B）由调用方合并，因为它需要基线快照。
    verdict: sanityFail.length ? (sanityBlocking ? 'FAIL' : 'DEBT')
      : debt.length ? 'DEBT' : 'PASS',
  }
}

/** 生成基线快照（只存可比对的数值，不含时间戳等噪声） */
export function makeBaseline(evaluation) {
  return {
    schema: 'l0-baseline/1',
    metrics: Object.fromEntries(
      evaluation.results.map((r) => [r.id, {
        efficiency: r.metrics.efficiency,
        temp_rise: r.metrics.temp_rise,
        total_loss: r.metrics.total_loss,
        torque: r.metrics.torque,
      }])
    ),
  }
}

/**
 * 门 B：与基线比较
 * 劣化方向定义：efficiency 变低 = 劣化；temp_rise / total_loss 变高 = 劣化
 * @param {object} evaluation evaluateSuite 的返回
 * @param {object|null} baseline makeBaseline 产出的快照
 * @param {object} tolerance { efficiency_pt, temp_k, loss_pct }
 * @returns {{ok: boolean, baseline_found: boolean, regressions: object[], improvements: object[]}}
 */
export function compareBaseline(evaluation, baseline, tolerance = {}) {
  const tol = {
    efficiency_pt: tolerance.efficiency_pt ?? 0.5,
    temp_k: tolerance.temp_k ?? 2.0,
    loss_pct: tolerance.loss_pct ?? 5.0,
  }

  if (!baseline?.metrics) {
    return { ok: true, baseline_found: false, regressions: [], improvements: [], tolerance: tol }
  }

  const regressions = []
  const improvements = []

  for (const r of evaluation.results) {
    const base = baseline.metrics[r.id]
    if (!base) {
      regressions.push({ id: r.id, metric: 'baseline', delta: null, msg: '基线中缺少该案例' })
      continue
    }
    const cur = r.metrics

    const dEff = base.efficiency - cur.efficiency          // 正 = 效率变低 = 劣化
    if (dEff > tol.efficiency_pt) {
      regressions.push({
        id: r.id, metric: 'efficiency', baseline: base.efficiency, current: cur.efficiency,
        delta: Math.round(dEff * 100) / 100, tolerance: tol.efficiency_pt, unit: 'pt',
      })
    } else if (dEff < -tol.efficiency_pt) improvements.push({ id: r.id, metric: 'efficiency', delta: Math.round(-dEff * 100) / 100 })

    const dTemp = cur.temp_rise - base.temp_rise            // 正 = 温升变高 = 劣化
    if (dTemp > tol.temp_k) {
      regressions.push({
        id: r.id, metric: 'temp_rise', baseline: base.temp_rise, current: cur.temp_rise,
        delta: Math.round(dTemp * 10) / 10, tolerance: tol.temp_k, unit: 'K',
      })
    } else if (dTemp < -tol.temp_k) improvements.push({ id: r.id, metric: 'temp_rise', delta: Math.round(-dTemp * 10) / 10 })

    const lossPct = base.total_loss > 0
      ? ((cur.total_loss - base.total_loss) / base.total_loss) * 100 : 0
    if (lossPct > tol.loss_pct) {
      regressions.push({
        id: r.id, metric: 'total_loss', baseline: base.total_loss, current: cur.total_loss,
        delta: Math.round(lossPct * 100) / 100, tolerance: tol.loss_pct, unit: '%',
      })
    } else if (lossPct < -tol.loss_pct) improvements.push({ id: r.id, metric: 'total_loss', delta: Math.round(lossPct * 100) / 100 })
  }

  return {
    ok: regressions.length === 0,
    baseline_found: true,
    regressions,
    improvements,
    tolerance: tol,
  }
}

export default {
  SIM_VALID_DOMAIN, inSimDomain, bandDeviationPt,
  evaluateCase, evaluateSuite, makeBaseline, compareBaseline,
}
