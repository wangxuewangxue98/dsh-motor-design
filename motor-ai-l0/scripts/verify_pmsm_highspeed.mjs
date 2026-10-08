#!/usr/bin/env node
/**
 * PMSM 高速工况核验脚本（v0.2.5）—— 替代 v0.2.4 时期的 diagnose_pmsm_stall.mjs
 * ============================================================================
 * 背景
 *   v0.2.4 之前，200kW/22000rpm/380V/PMSM 工况下 L0 三步链 120/120 候选全 failed、
 *   recommended=null。当时定位出 5 条根因并给出改进方案 P0~P5，v0.2.5 已全部落地：
 *     P5 能力门 / P0 轭厚按磁密反解 / P1 匝数量化语义拆分（V14+V17）/ P2 磁密口径自洽
 *     P3「并联支路方向反了」经数值验证为**错误结论**，已撤回（详见 CHANGELOG 0.2.5）
 *
 * 本脚本的定位因此从「诊断为什么坏」转为「核验修好了没有」：
 *   1) 端到端跑三步链，看可行数与阻断规则
 *   2) 验证 P0：轭厚是否已按磁密配厚、V08 是否清零
 *   3) 验证 P1：量化职责是否已拆分（V14 只抓填错、V17 判整匝无解）
 *   4) 验证 P2：磁密口径是否随工况变化（不再是恒 0.80T）
 *   5) 验证 P5：能力门是否给出明确路由，而非死胡同
 *
 * 只读：只 import 源码做调用与数值观察，不写盘、不改配置。
 * 用法：node scripts/verify_pmsm_highspeed.mjs
 */

/** @typedef {'ok'|'warn'|'bad'} Level */

const line = (ch = '─') => console.log(ch.repeat(78))
const head = (t) => { console.log(''); line('═'); console.log(t); line('═') }

import { buildParamMatrix } from '../tools/l0/param-matrix.mjs'
import { runL0Estimate } from '../tools/l0/l0-estimate.mjs'
import { runDesignValidate } from '../tools/l0/design-validate.mjs'
import { yokeFluxDensity, airGapFluxActual } from '../lib/design-rules.mjs'
import { solveYokeAndFrame, STACKING_FACTOR } from '../lib/motor-constants.mjs'
import { assessApplicability } from '../lib/applicability-gate.mjs'

const SPEC = {
  power_kw: 200, speed_rpm: 22000, voltage_v: 380,
  motor_type: 'pmsm', cooling: 'liquid_jacket', insulation_class: 'H', count: 120,
}

head('【1】端到端三步链（200kW / 22000rpm / 380V / PMSM —— v0.2.4 全灭工况）')

const built = buildParamMatrix(SPEC, {})
const rows = built.matrix
console.log(`① 矩阵生成：${rows.length} 行，od_limit=${built.od_limit.value}mm（source=${built.od_limit.source}）`)

const grown = rows.filter((r) => r._physics?.od_grown_for_yoke).length
const capped = rows.filter((r) => r._physics?.yoke_limited_by_od_cap).length
console.log(`   P0 轭厚放大行=${grown}  被 od_limit 截断行=${capped}`)

const v = runDesignValidate({ params_list: rows })
console.log(`② 物理校验 summary = ${JSON.stringify(v.summary)}`)
console.log(`   规则命中: ${(v.rule_hits ?? []).map((h) => `${h.rule}×${h.count}`).join('  ')}`)

const kept = rows.filter((_, i) => !(v.failed_indices ?? []).includes(i))
console.log(`   剔除 failed 后剩余 ${kept.length} 行（v0.2.4 为 0 行）`)

const est = runL0Estimate({ params_list: kept, sort_by: 'efficiency_raw' }, {})
console.log(`③ L0 估算：applicable=${est.applicability}  feasible_count=${est.feasible_count}  returned=${est.returned}`)
if (est.recommended) {
  const r = est.recommended
  console.log(`   recommended = OD${r.stator_od}mm / ${r.poles}极 / η=${r.efficiency}% (raw ${r.efficiency_raw}%)`)
}
if (est.results[0]) {
  const t = est.results[0]
  console.log(`   Top1 诊断: By=${t.yoke_flux_density}T  Bg=${t.air_gap_flux_density}T  `
    + `匝/线圈=${t.turns_per_coil}  槽满率=${t.slot_fill_ratio}  verdict=${t.verdict}`)
}

head('【2】P0 验证 —— 轭厚是否已按磁密需求配厚')
{
  const r = rows[0]
  const frame = solveYokeAndFrame({ statorOd: r.stator_od, statorId: r.stator_id, poles: r.poles })
  const by = yokeFluxDensity({ statorId: r.stator_id, yokeThickness: r.yoke_thickness, poles: r.poles, airGapFlux: 0.8 })
  console.log(`样例行: OD${r.stator_od}/Dsi${r.stator_id}/${r.poles}极`)
  console.log(`  比例式轭厚 = ${frame.yoke_by_ratio}mm`)
  console.log(`  磁密需求   = ${frame.yoke_by_flux}mm（By ≤ 1.5T 反解）`)
  console.log(`  实际配给   = ${r.yoke_thickness}mm   ⇒ By = ${by}T`)
  console.log(`  判定: ${by <= 1.5 ? '✅ 轭厚已满足磁密要求' : '❌ 仍不满足'}`)
  const v08failed = (v.rule_hits ?? []).find((h) => h.rule === 'V08:failed')
  console.log(`  全批 V08 failed = ${v08failed ? v08failed.count : 0} 行（v0.2.4 为 120 行全灭）`)
}

head('【3】P1 验证 —— 量化职责拆分（V14 只抓填错/ V17 判整匝无解）')
{
  const rep = runDesignValidate({ params_list: rows, include_reports: true })
  const issues = (rep.reports ?? []).flatMap((r) => r.issues ?? [])
  const v14 = issues.filter((i) => i.rule === 'V14')
  const v17f = issues.filter((i) => i.rule === 'V17' && i.level === 'failed')
  const v17w = issues.filter((i) => i.rule === 'V17' && i.level === 'warning')
  console.log(`V14 命中 = ${v14.length}（理想匝<1 时不重复归因，应为 0）`)
  console.log(`V17 failed = ${v17f.length}（圆线整匝绕组物理无解）`)
  console.log(`V17 warning = ${v17w.length}（可整除但量化偏差偏大）`)
  if (v17f[0]) console.log(`\nV17 样例报错（必含可执行出路）:\n  ${String(v17f[0].msg).split('；').join('；\n  ')}`)

  // 反向守卫：常规工况手填离谱匝数，V14 必须仍然拦截
  const okM = buildParamMatrix({ power_kw: 15, speed_rpm: 1460, voltage_v: 380, count: 20 }, {}).matrix
  const bad = { ...okM[0], turns_per_coil: okM[0].turns_per_coil * 10, _physics: { ...okM[0]._physics } }
  const badRep = runDesignValidate({ params_list: [bad], include_reports: true })
  const caught = (badRep.reports?.[0]?.issues ?? []).some((i) => i.rule === 'V14' && i.level === 'failed')
  console.log(`\n反向守卫（15kW 常规工况手填 10× 匝数）: ${caught ? '✅ V14 仍拦截' : '❌ V14 守卫失效'}`)
}

head('【4】P2 验证 —— 磁密口径是否已随工况自洽')
{
  console.log('同族不同转速的实际气隙磁密（由实际整数匝数反解）：')
  console.log('  转速      频率    匝/线圈   实际Bg    固定口径By')
  for (const n of [1000, 8000, 22000]) {
    const m = buildParamMatrix({ power_kw: 200, speed_rpm: n, voltage_v: 380, motor_type: 'pmsm', count: 8 }, {}).matrix
    const r = m.find((x) => x.turns_per_coil > 0) ?? m[0]
    const bg = airGapFluxActual({
      voltageV: r.voltage, speedRpm: r.speed, poles: r.poles,
      statorId: r.stator_id, coreLength: r.core_length,
      slotsStator: r.slots_stator, parallelCircuits: r.parallel_circuits,
      turnsPerCoil: r.turns_per_coil,
    })
    const byFixed = (0.8 * r.stator_id) / (r.poles * r.yoke_thickness * STACKING_FACTOR)
    console.log(`  ${String(n).padStart(6)}rpm ${String((n * r.poles / 120).toFixed(0)).padStart(6)}Hz`
      + ` ${String(r.turns_per_coil).padStart(8)} ${bg.toFixed(3).padStart(8)}T`
      + ` ${byFixed.toFixed(2).padStart(10)}T`)
  }
  console.log('⇒ 实际 Bg 随工况变化（v0.2.4 恒为 0.80T）；可用 bg_caliber=intent 回退旧口径')
}

head('【5】P5 验证 —— 能力门是否给出明确出路（而非死胡同）')
{
  const g = assessApplicability({
    powerKw: SPEC.power_kw, speedRpm: SPEC.speed_rpm,
    voltageV: SPEC.voltage_v, poles: rows[0]?.poles, motorType: 'pmsm',
  })
  console.log(`verdict = ${g.verdict}   routing = ${g.routing}`)
  for (const r of g.reasons) console.log(`  [${r.level}] ${r.code}: ${r.msg}`)
  console.log(`\n路由建议:\n${String(g.advice).split('\n').map((s) => '  ' + s).join('\n')}`)
}

head('【6】结论')
{
  const ok = est.feasible_count > 0 && kept.length > 0
  console.log(ok
    ? `✅ 该工况已不再空转：${kept.length}/${rows.length} 行通过校验，${est.feasible_count} 行可行。`
    : '❌ 仍全灭。')
  console.log(`   但请注意能力门判定 = ${est.applicability}：`)
  console.log('   高速 PMSM 仍超出 L0 标定域（无 dq/弱磁/退磁模型、无转子强度校核），')
  console.log('   故上述数字仅可用于「电/磁侧初筛与横向比较」，最终结论必须由 L1/L2 给出。')
  console.log(`   L0 内部仍有 ${est.results.filter((r) => r.verdict !== 'feasible').length} 行因温升模型未标定被判不可行，`)
  console.log('   这是已知的标定欠账（质量门 DEBT），与本次修复无关。')
}

console.log('')