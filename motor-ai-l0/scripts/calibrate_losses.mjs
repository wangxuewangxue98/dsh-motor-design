/**
 * 损耗模型标定（铁耗 / 铜耗 / 机械耗）
 * ---------------------------------------------------------------------------
 * 数据来源：MotorDesign `output/loss_calib/calib_dataset.json`
 *   —— 由 `Scripts/l0_training/export_loss_calib_dataset.py` 从生产库 `l0_residuals`
 *      导出的 RMxprt 批次真值（source_path LIKE 'cal_%'）。
 *
 * 口径纪律（务必先读，改动前看 MEMORY.md「伪真值」条目）：
 *   · 真值只用 total_loss / copper_loss / iron_loss。
 *   · mechanical_loss 与 l1_temp/max_temp **不可用**（前者恒为 total×0.15 的合成值，
 *     后者是含 random.uniform(-3,3) 的估算式）。真实机械耗 = total − cu − fe（自洽推导）。
 *   · 铁耗占比 > 50% 的行已在导出阶段剔除（高速多极 f² 项爆表，属工况不可行非模型误差）。
 *
 * 标定手法：
 *   铁耗 —— 对 [f·B^α, f²·B²] 两个基做**无截距最小二乘**，解出 kh / ke（物理结构不变）。
 *   铜耗 —— 乘性系数 k_cu = argmin Σ(k·P_L0 − P_true)²（一维闭式解）。
 *   机械耗 —— 乘性系数 k_mech 同上。
 *
 * 用法：
 *   node scripts/calibrate_losses.mjs                       # 标定并打印报告
 *   node scripts/calibrate_losses.mjs --apply               # 标定并写回 motor-constants.mjs
 *   node scripts/calibrate_losses.mjs --dataset <path>      # 指定数据集
 */

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  estimateCoreMassKg, estimateIronLoss, estimateCopperLoss,
  estimateMechanicalLoss, deriveElectricalClosure,
} from '../lib/formula-engine.mjs'
import { STEINMETZ, AIR_GAP_FLUX_DEFAULT } from '../lib/motor-constants.mjs'

/** 气隙缺省，与 quickL0Estimate 的 `params.air_gap ?? 0.6` 保持同源 */
const AIR_GAP_DEFAULT = 0.6

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const DEFAULT_DATASET = process.env.L0B_CALIB_DATASET || null   // 必须由 --dataset 或环境变量显式指定

const TARGETS = { copper: 0.85, iron: 0.80, total: 0.85 }   // 规格 §验收阈值

/** 复刻 quickL0Estimate 的输入准备顺序，保证标定口径与生产完全一致 */
function predictLosses(c) {
  const statorId = c.stator_id
  const rotorOdMm = Math.round((statorId - 2 * (c.air_gap ?? 0.6)) * 10) / 10
  const coreMassKg = estimateCoreMassKg(c.stator_od, statorId, c.core_length)
  const closure = deriveElectricalClosure({
    voltage: c.voltage_v,
    speed: c.speed_rpm,
    poles: c.poles,
    statorId,
    coreLength: c.core_length,
    airGap: c.air_gap ?? 0.6,
    slotsStator: c.slots_stator,
    parallelCircuits: c.parallel_circuits,
    airGapFlux: AIR_GAP_FLUX_DEFAULT,
    powerKw: c.power_kw,
  })
  const effTurns = closure?.turns_per_coil ?? c.turns_per_coil
  const effCurrent = closure?.peak_current ?? null
  const pIron = estimateIronLoss({
    speedRpm: c.speed_rpm,
    poles: c.poles,
    coreMassKg,
    fluxDensity: STEINMETZ.fluxDensity,
  })
  const cu = estimateCopperLoss({
    statorOd: c.stator_od,
    statorId,
    coreLength: c.core_length,
    slotsStator: c.slots_stator,
    poles: c.poles,
    peakCurrent: effCurrent,
    turnsPerCoil: effTurns,
    parallelCircuits: c.parallel_circuits,
    airGap: c.air_gap ?? 0.6,
  })
  const pMech = estimateMechanicalLoss({ speedRpm: c.speed_rpm, rotorOdMm })
  return { pIron, pCopper: cu.copper_loss, pMech, coreMassKg, rotorOdMm }
}

/** 无截距一元最小二乘：y ≈ k·x ⇒ k = Σxy / Σx² */
function fitScale(xs, ys) {
  let sxy = 0
  let sxx = 0
  for (let i = 0; i < xs.length; i += 1) {
    sxy += xs[i] * ys[i]
    sxx += xs[i] * xs[i]
  }
  return sxx > 0 ? sxy / sxx : 1
}

/** 无截距二元最小二乘（正规方程 2x2） */
function fitTwoBases(b1, b2, y) {
  let a11 = 0, a12 = 0, a22 = 0, b1v = 0, b2v = 0
  for (let i = 0; i < y.length; i += 1) {
    a11 += b1[i] * b1[i]; a12 += b1[i] * b2[i]; a22 += b2[i] * b2[i]
    b1v += b1[i] * y[i]; b2v += b2[i] * y[i]
  }
  const det = a11 * a22 - a12 * a12
  if (Math.abs(det) < 1e-12) return { k1: 1, k2: 0, ok: false }
  return { k1: (b1v * a22 - a12 * b2v) / det, k2: (a11 * b2v - a12 * b1v) / det, ok: true }
}

/** 判定系数（R²，无截距口径） */
function r2(xs, ys) {
  const n = ys.length
  const k = fitScale(xs, ys)
  const sse = ys.reduce((s, y, i) => s + (y - k * xs[i]) ** 2, 0)
  const sst = ys.reduce((s, y) => s + y ** 2, 0)
  return { r2: sst > 0 ? 1 - sse / sst : 0, k }
}

function stats(pred, truth) {
  const n = truth.length
  if (!n) return { r2: 0, mae: 0, mape: 0 }
  const k = fitScale(pred, truth)
  let sse = 0, sae = 0, sape = 0
  for (let i = 0; i < n; i += 1) {
    const p = k * pred[i]
    sse += (truth[i] - p) ** 2
    sae += Math.abs(truth[i] - p)
    sape += Math.abs(truth[i] - p) / Math.max(1, truth[i])
  }
  const sst = truth.reduce((s, y) => s + y ** 2, 0)
  return { k, r2: sst > 0 ? 1 - sse / sst : 0, mae: sae / n, mape: sape / n }
}

const argv = process.argv.slice(2)
const APPLY = argv.includes('--apply')
const dsIdx = argv.indexOf('--dataset')
const DATASET = dsIdx >= 0 ? argv[dsIdx + 1] : DEFAULT_DATASET

if (!DATASET) {
  console.error('❌ 缺少数据集。请用 --dataset <path> 或环境变量 L0B_CALIB_DATASET 指定。')
  console.error('   生成方：MotorDesign Scripts/l0_training/export_loss_calib_dataset.py')
  process.exit(1)
}
if (!fs.existsSync(DATASET)) {
  console.error(`❌ 数据集不存在: ${DATASET}`)
  console.error('   先运行：python Scripts/l0_training/export_loss_calib_dataset.py')
  process.exit(1)
}

const ds = JSON.parse(fs.readFileSync(DATASET, 'utf8'))
const cases = ds.cases
console.log(`数据��� ${ds.meta.kept} 行（读取 ${cases.length}），铁损占比上限 ${ds.meta.fe_share_max}`)
console.log(`真源: ${DATASET}\n`)

const pred = { iron: [], copper: [], mech: [] }
const truth = { iron: [], copper: [], mech: [] }
const B1 = []   // f·B^α
const B2 = []   // f²·B²
const MASS = [] // m_core

for (const c of cases) {
  let p
  try {
    p = predictLosses(c)
  } catch (e) {
    continue
  }
  const f = (c.speed_rpm * c.poles) / 120
  const B = STEINMETZ.fluxDensity
  B1.push(f * B ** STEINMETZ.alpha)
  B2.push(f * f * B * B)
  MASS.push(p.coreMassKg)
  pred.iron.push(p.pIron); truth.iron.push(c.truth.iron_loss)
  pred.copper.push(p.pCopper); truth.copper.push(c.truth.copper_loss)
  pred.mech.push(p.pMech); truth.mech.push(c.truth.mech_loss_derived)
}

const n = truth.iron.length
if (!n) {
  console.error('❌ 无有效样本')
  process.exit(1)
}

// ── 铁耗：解 kh / ke（物理基函数不变，只换系数）──
const yPerMass = truth.iron.map((v, i) => v / Math.max(1e-9, MASS[i]))
const { k1: kh, k2: ke, ok: ironOk } = fitTwoBases(B1, B2, yPerMass)

// ── 铜耗 / 机械耗：乘性系数 ──
const cu = stats(pred.copper, truth.copper)
const mech = stats(pred.mech, truth.mech)
const ironBase = r2(pred.iron, truth.iron)   // 标定前基线（当前系数）

// ── 总损（按标定后三项重算）──
const predTotal = []
const truthTotal = []
for (let i = 0; i < n; i += 1) {
  const ironNew = (kh * B1[i] + ke * B2[i]) * MASS[i]
  predTotal.push(ironNew + cu.k * pred.copper[i] + mech.k * pred.mech[i])
  truthTotal.push(truth.iron[i] + truth.copper[i] + truth.mech[i])
}
const total = stats(predTotal, truthTotal)

const R = (x, d = 4) => Number(x).toFixed(d)
console.log('══ 标定结果 ══════════════════════════════════════════════════════')
console.log(`样本 ${n} 行`)
console.log('')
console.log('【铁耗】P_fe = (kh·f·B^α + ke·f²·B²)·m_core')
console.log(`  kh: ${R(STEINMETZ.kh, 5)} → ${R(kh, 5)}   (×${R(kh / STEINMETZ.kh, 2)})`)
console.log(`  ke: ${R(STEINMETZ.ke, 7)} → ${R(ke, 7)}   (×${R(ke / STEINMETZ.ke, 2)})`)
console.log(`  线性解有效: ${ironOk ? '是' : '否（基函数共线，退回单基）'}`)
console.log(`  R²: ${R(ironBase.r2)} → ${R(r2(pred.iron.map(() => 0), []).r2 || ironBase.r2, 3)}`
  + `   标定后 R²: ${R(fitIronR2(kh, ke, B1, B2, MASS, truth.iron))}`)
console.log('')
console.log('【铜耗】乘性系数（电流闭环结构不变）')
console.log(`  k_cu: 1.0000 → ${R(cu.k)}   (×${R(cu.k, 2)})`)
console.log(`  R²: ${R(cu.r2)}   MAE ${R(cu.mae, 1)}W   MAPE ${R(cu.mape * 100, 1)}%`)
console.log('')
console.log('【机械耗】乘性系数')
console.log(`  k_mech: 1.0000 → ${R(mech.k)}   (×${R(mech.k, 2)})`)
console.log(`  R²: ${R(mech.r2)}   MAE ${R(mech.mae, 1)}W   MAPE ${R(mech.mape * 100, 1)}%`)
console.log('')
console.log('【总损耗】（标定后三项重算）')
console.log(`  R²: ${R(total.r2)}   MAE ${R(total.mae, 1)}W   MAPE ${R(total.mape * 100, 1)}%`)

console.log('\n══ 验收（规格阈值）═══════════════════════════════════════════════')
const ironR2 = fitIronR2(kh, ke, B1, B2, MASS, truth.iron)
const rows = [
  ['铜损', cu.r2, TARGETS.copper, cu.mape],
  ['铁损', ironR2, TARGETS.iron, null],
  ['总损', total.r2, TARGETS.total, total.mape],
]
let allPass = true
for (const [name, r, tgt, mape] of rows) {
  const pass = r >= tgt
  if (!pass) allPass = false
  console.log(`  ${pass ? '✅' : '❌'} ${name} R²=${R(r)}  阈值 ${tgt}`
    + (mape != null ? `  MAPE=${R(mape * 100, 1)}%` : ''))
}
if (!allPass) {
  console.log('\n⚠ 未达全部阈值 —— 不建议写回常量，避免把拟合噪声固化进物理公式。')
}

if (APPLY) {
  if (!allPass) {
    console.log('\n❌ --apply 已指定，但验收未全绿，拒绝写回。')
    process.exit(2)
  }
  const f = path.join(__dirname, '..', 'lib', 'motor-constants.mjs')
  let src = fs.readFileSync(f, 'utf8')
  src = src.replace(
    /export const STEINMETZ = \{[^}]*\}/,
    `export const STEINMETZ = { kh: ${R(kh, 6)}, ke: ${R(ke, 8)}, `
    + `alpha: ${STEINMETZ.alpha}, fluxDensity: ${STEINMETZ.fluxDensity} }`,
  )
  fs.writeFileSync(f, src, 'utf8')
  console.log(`\n✅ 已写回 lib/motor-constants.mjs：STEINMETZ.kh / ke`)
  console.log(`   铜耗 ×${R(cu.k, 4)}、机械耗 ×${R(mech.k, 4)} 仍需在 estimateCopperLoss /`)
  console.log('   estimateMechanicalLoss 内以显式校准常量落地（本次仅标出数值，未改公式）。')
}

function fitIronR2(kh2, ke2, b1, b2, mass, truthArr) {
  const xs = b1.map((v, i) => (kh2 * v + ke2 * b2[i]) * mass[i])
  return r2(xs, truthArr).r2
}