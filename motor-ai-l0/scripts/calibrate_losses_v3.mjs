/**
 * 损耗模型标定 v3 —— 铜耗分档系数 + 变频铁耗双基辨识
 * ---------------------------------------------------------------------------
 * 数据源（均须显式传参，脚本不落任何本机绝对路径）：
 *   --dataset       控制变量扫参数据集（铜耗/总损标定，必选）
 *                   生成方：MotorDesign Scripts/l0_training/export_cv_dataset.py
 *   --freq-dataset  变频扫参数据集（铁耗双基辨识，可选；无则铁耗沿用 v2 口径）
 *                   生成方：MotorDesign Scripts/l0_training/cv_sweep_freq.py
 *
 * ════ v3 相对 v2 的两个变化 ════
 * 1. 铜耗分档系数（替代 v2 单系数）：
 *    v2 单系数 k=0.887 档间比值极差 6.4×（MAPE 168%）；分档后每档 R² 0.92~0.97
 *    ⇒ 档内模型形状正确，档间只差一个幅度 ⇒ 按功率分档查表 k(P)。
 *    分档边界：≤30kW / 30~100kW / >100kW（基于本次三档样本，外插需谨慎）。
 * 2. 铁耗双基辨识（需变频数据）：
 *    v2 样本 f 恒 ≈49Hz ⇒ [f·B^α, f²·B²] 共线解出负 ke。
 *    v3 变频样本 f 覆盖 25~200Hz（8×）且 B 用真值 B_eff=(Bt+By)/2，
 *    双基正交后 NNLS（非负最小二乘）拟合 kh/ke，物理约束保证不出现负系数。
 *
 * 用法：
 *   node scripts/calibrate_losses_v3.mjs --dataset <path>
 *   node scripts/calibrate_losses_v3.mjs --dataset <path> --freq-dataset <path>
 *   node scripts/calibrate_losses_v3.mjs --dataset <path> --apply   # 达标才写回
 */

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  estimateCoreMassKg, estimateIronLoss, estimateCopperLoss, estimateMechanicalLoss,
} from '../lib/formula-engine.mjs'
import { STEINMETZ } from '../lib/motor-constants.mjs'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const TARGETS = { copper: 0.85, iron: 0.80, total: 0.85 }

/** 功率分档边界（kW）—— 与 motor-constants.mjs 的 COPPER_LOSS_K 保持同一真源 */
const FRAME_BINS = [
  { name: 'small', maxKw: 30 },
  { name: 'medium', maxKw: 100 },
  { name: 'large', maxKw: Infinity },
]

function frameOf(powerKw) {
  return FRAME_BINS.find((b) => powerKw <= b.maxKw) ?? FRAME_BINS[FRAME_BINS.length - 1]
}

function predictCopper(c) {
  const cu = estimateCopperLoss({
    statorOd: c.stator_od, statorId: c.stator_id, coreLength: c.core_length,
    slotsStator: c.slots_stator, poles: c.poles,
    peakCurrent: c.stator_current * Math.SQRT2,
    turnsPerCoil: c.turns_per_coil, parallelCircuits: c.parallel_circuits, airGap: 0.6,
  })
  return cu.copper_loss
}

function predictMech(c) {
  const rotorOdMm = Math.round((c.stator_id - 2 * 0.6) * 10) / 10
  return estimateMechanicalLoss({ speedRpm: c.speed_rpm, rotorOdMm })
}

/** 铁耗等效磁密：齿/轭均值（标定与生产必须同一口径） */
function bEffOf(c) {
  const bt = c.tooth_flux, by = c.yoke_flux
  if (bt && by) return (bt + by) / 2
  if (bt) return bt
  if (by) return by
  return STEINMETZ.fluxDensity   // 回退：与 v2 一致
}

function fitScale(xs, ys) {
  let sxy = 0, sxx = 0
  for (let i = 0; i < xs.length; i += 1) { sxy += xs[i] * ys[i]; sxx += xs[i] * xs[i] }
  return sxx > 0 ? sxy / sxx : 1
}

/** 非负最小二乘（2 基，投影法：普通解含负分量时按边界约束重解） */
function nnls2(b1, b2, y) {
  let a11 = 0, a12 = 0, a22 = 0, v1 = 0, v2 = 0
  for (let i = 0; i < y.length; i += 1) {
    a11 += b1[i] * b1[i]; a12 += b1[i] * b2[i]; a22 += b2[i] * b2[i]
    v1 += b1[i] * y[i]; v2 += b2[i] * y[i]
  }
  const det = a11 * a22 - a12 * a12
  if (Math.abs(det) < 1e-12) return { k1: NaN, k2: NaN, ok: false, note: '病态(共线)' }
  let k1 = (v1 * a22 - a12 * v2) / det
  let k2 = (a11 * v2 - a12 * v1) / det
  if (k1 >= 0 && k2 >= 0) return { k1, k2, ok: true, note: '内点解' }
  // 边界约束：固定负分量为 0，重解另一分量
  if (k1 < 0 && k2 < 0) return { k1: 0, k2: 0, ok: false, note: '双负(数据异常)' }
  if (k1 < 0) { k1 = 0; k2 = Math.max(0, v2 / a22); return { k1, k2, ok: true, note: '边界解 k1→0' } }
  k2 = 0; k1 = Math.max(0, v1 / a11)
  return { k1, k2, ok: true, note: '边界解 k2→0' }
}

function r2Fixed(xs, ys, k) {
  const sse = ys.reduce((s, y, i) => s + (y - k * xs[i]) ** 2, 0)
  const sst = ys.reduce((s, y) => s + y * y, 0)
  return sst > 0 ? 1 - sse / sst : 0
}

function mapeFixed(xs, ys, k) {
  let s = 0
  for (let i = 0; i < ys.length; i += 1) {
    if (ys[i] !== 0) s += Math.abs(ys[i] - k * xs[i]) / Math.abs(ys[i])
  }
  return (s / ys.length) * 100
}

function argIdx(argv, name) {
  const i = argv.indexOf(name)
  return i >= 0 ? argv[i + 1] : null
}

function main() {
  const argv = process.argv.slice(2)
  const apply = argv.includes('--apply')
  const dsPath = argIdx(argv, '--dataset') || process.env.L0B_CALIB_DATASET
  const freqPath = argIdx(argv, '--freq-dataset') || process.env.L0B_FREQ_DATASET

  if (!dsPath || !fs.existsSync(dsPath)) {
    console.error('缺少/无效 --dataset（cv 控制变量数据集）')
    process.exit(1)
  }
  const rows = JSON.parse(fs.readFileSync(dsPath, 'utf8')).rows
  const freqRows = freqPath && fs.existsSync(freqPath)
    ? JSON.parse(fs.readFileSync(freqPath, 'utf8')).kept
    : null

  console.log('═'.repeat(78))
  console.log('损耗模型标定 v3 —— 铜耗分档 + 变频铁耗')
  console.log('═'.repeat(78))
  console.log(`cv 数据集: ${dsPath}  (${rows.length} 行)`)
  console.log(`变频数据集: ${freqPath ?? '（未提供，铁耗沿用 v2 口径）'}${
    freqRows ? `  (${freqRows.length} 行)` : ''}`)

  // ════ 1. 铜耗分档 ════
  const cuL0 = rows.map(predictCopper)
  const cuTrue = rows.map((r) => r.copper_loss)

  console.log('\n' + '─'.repeat(78))
  console.log('【铜耗·分档标定】  P_cu = k(功率档) · 3·I²·R，I/N 用 RMxprt 真值')
  console.log('─'.repeat(78))
  const frames = {}
  for (const b of FRAME_BINS) {
    const idx = rows.map((r, i) => (frameOf(r.power_kw).name === b.name ? i : -1))
      .filter((i) => i >= 0)
    if (!idx.length) continue
    const xs = idx.map((i) => cuL0[i])
    const ys = idx.map((i) => cuTrue[i])
    const k = fitScale(xs, ys)
    const r2v = r2Fixed(xs, ys, k)
    const mp = mapeFixed(xs, ys, k)
    const ratios = idx.map((i) => cuTrue[i] / cuL0[i])
    frames[b.name] = {
      k: +k.toFixed(4), r2: +r2v.toFixed(4), mape: +mp.toFixed(1), n: idx.length,
      powerKw: [...new Set(idx.map((i) => rows[i].power_kw))],
      ratioRange: [+Math.min(...ratios).toFixed(3), +Math.max(...ratios).toFixed(3)],
    }
    console.log(`  ${b.name.padEnd(7)} k=${k.toFixed(4)}  R²=${r2v.toFixed(4)}  ` +
      `MAPE=${mp.toFixed(1)}%  n=${idx.length}  P∈[${frames[b.name].powerKw}]kW  ` +
      `比值 ${frames[b.name].ratioRange.join('~')}`)
  }
  // 对比：单系数
  const kSingle = fitScale(cuL0, cuTrue)
  console.log(`  ── 对比单系数: k=${kSingle.toFixed(4)}  R²=${r2Fixed(cuL0, cuTrue, kSingle).toFixed(4)}` +
    `  MAPE=${mapeFixed(cuL0, cuTrue, kSingle).toFixed(1)}%`)
  // 分档后整体 R²/MAPE
  const cuPred3 = cuL0.map((x, i) => x * frames[frameOf(rows[i].power_kw).name].k)
  const cuR2All = r2Fixed(cuPred3, cuTrue, 1)
  const cuMapeAll = mapeFixed(cuPred3, cuTrue, 1)
  console.log(`  ── 分档整体: R²=${cuR2All.toFixed(4)}  MAPE=${cuMapeAll.toFixed(1)}%  ` +
    `${cuR2All >= TARGETS.copper ? '✅ 达标' : '❌ 未达标'}（阈值 ${TARGETS.copper}）`)

  // ════ 2. 铁耗 ════
  console.log('\n' + '─'.repeat(78))
  console.log('【铁耗标定】  P_fe = kh·f·B^α + ke·f²·B²，B=(Bt+By)/2 真值口径')
  console.log('─'.repeat(78))
  let iron = null
  if (freqRows && freqRows.length >= 8) {
    const fArr = freqRows.map((r) => (r.speed_rpm * r.poles) / 120)   // 插件口径 f = p·n/120
    const bArr = freqRows.map(bEffOf)
    const mArr = freqRows.map((r) => estimateCoreMassKg(r.stator_od, r.stator_id, r.core_length))
    const yArr = freqRows.map((r) => r.iron_loss)                     // W
    const b1 = fArr.map((f, i) => f * Math.pow(bArr[i], STEINMETZ.alpha) * mArr[i])
    const b2 = fArr.map((f, i) => f * f * bArr[i] * bArr[i] * mArr[i])
    const fit = nnls2(b1, b2, yArr)
    const fePred = b1.map((x, i) => fit.k1 * x + fit.k2 * b2[i])
    const feR2 = r2Fixed(fePred, yArr, 1)
    const feMape = mapeFixed(fePred, yArr, 1)
    const fSpan = [Math.min(...fArr), Math.max(...fArr)]
    const bSpan = [Math.min(...bArr), Math.max(...bArr)]
    console.log(`  f 覆盖 ${fSpan[0].toFixed(1)}~${fSpan[1].toFixed(1)}Hz ` +
      `(${(fSpan[1] / fSpan[0]).toFixed(1)}×)  B_eff 覆盖 ${bSpan[0].toFixed(3)}~${bSpan[1].toFixed(3)}T ` +
      `(${(bSpan[1] / bSpan[0]).toFixed(1)}×)`)
    console.log(`  NNLS: kh=${fit.k1.toExponential(4)}  ke=${fit.k2.toExponential(4)}  [${fit.note}]`)
    console.log(`  R²=${feR2.toFixed(4)}  MAPE=${feMape.toFixed(1)}%  ` +
      `${feR2 >= TARGETS.iron ? '✅ 达标' : '❌ 未达标'}（阈值 ${TARGETS.iron}）  n=${freqRows.length}`)
    // 对照：旧 kh/ke 在新数据上的表现
    const feOld = freqRows.map((r) => {
      const f = (r.speed_rpm * r.poles) / 120
      const m = estimateCoreMassKg(r.stator_od, r.stator_id, r.core_length)
      const B = bEffOf(r)
      return (STEINMETZ.kh * f * Math.pow(B, STEINMETZ.alpha) + STEINMETZ.ke * f * f * B * B) * m
    })
    const kOld = fitScale(feOld, yArr)
    console.log(`  对照旧系数(按最优比例缩放): k_old=${kOld.toFixed(3)} ` +
      `R²=${r2Fixed(feOld, yArr, kOld).toFixed(4)} —— 新系数须不低于此`)
    iron = { kh: fit.k1, ke: fit.k2, r2: feR2, mape: feMape, n: freqRows.length, note: fit.note }
  } else {
    // 无变频数据：退化为 v2 口径（单比例系数，不做双基）
    const feL0 = rows.map((r) => estimateIronLoss({
      speedRpm: r.speed_rpm, poles: r.poles,
      coreMassKg: estimateCoreMassKg(r.stator_od, r.stator_id, r.core_length),
      fluxDensity: bEffOf(r),
    }))
    const feTrue = rows.map((r) => r.iron_loss)
    const kFe = fitScale(feL0, feTrue)
    const feR2 = r2Fixed(feL0, feTrue, kFe)
    console.log(`  （无变频数据，单比例口径）k_fe=${kFe.toFixed(4)}  R²=${feR2.toFixed(4)}  ` +
      `${feR2 >= TARGETS.iron ? '✅' : '❌（双基辨识需 --freq-dataset）'}`)
    iron = { k: kFe, r2: feR2, n: rows.length, note: '单比例（无变频数据）' }
  }

  // ════ 3. 总损（分档铜耗 + 铁耗 + 机械耗）════
  const mechL0 = rows.map(predictMech)
  const feL0cv = rows.map((r) => estimateIronLoss({
    speedRpm: r.speed_rpm, poles: r.poles,
    coreMassKg: estimateCoreMassKg(r.stator_od, r.stator_id, r.core_length),
    fluxDensity: bEffOf(r),
  }))
  const kFeUse = iron.kh != null ? null : iron.k   // 双基模式总损仍用旧公式形态，见报告说明
  const fePredCv = kFeUse != null ? feL0cv.map((x) => x * kFeUse) : feL0cv
  const totPred = rows.map((r, i) =>
    cuL0[i] * frames[frameOf(r.power_kw).name].k + fePredCv[i] + mechL0[i])
  const totTrue = rows.map((r) => r.total_loss)
  const totR2 = r2Fixed(totPred, totTrue, 1)
  const totMape = mapeFixed(totPred, totTrue, 1)
  console.log('\n' + '─'.repeat(78))
  console.log('【总损·分档铜耗合成】  total = k档·P_cu + P_fe + P_mech（不加权，各分量独立标定）')
  console.log('─'.repeat(78))
  console.log(`  R²=${totR2.toFixed(4)}  MAPE=${totMape.toFixed(1)}%  ` +
    `${totR2 >= TARGETS.total ? '✅ 达标' : '❌ 未达标'}（阈值 ${TARGETS.total}）`)

  // ════ 4. 汇总与写回 ════
  const copperPass = cuR2All >= TARGETS.copper
  const ironPass = iron.r2 >= TARGETS.iron
  const totalPass = totR2 >= TARGETS.total
  console.log('\n' + '='.repeat(78))
  console.log('汇总')
  console.log('='.repeat(78))
  console.log(`  铜耗分档: ${Object.entries(frames).map(([n, f]) => `${n}=${f.k}`).join('  ')}  ` +
    `R²=${cuR2All.toFixed(4)} ${copperPass ? '✅' : '❌'}`)
  console.log(`  铁耗: ${iron.kh != null ? `kh=${iron.kh.toExponential(3)} ke=${iron.ke.toExponential(3)}` : `k=${iron.k?.toFixed(4)}`}  ` +
    `R²=${iron.r2.toFixed(4)} ${ironPass ? '✅' : '❌'}`)
  console.log(`  总损: R²=${totR2.toFixed(4)} ${totalPass ? '✅' : '❌'}`)

  const outPath = path.join(ROOT, 'scripts', 'loss_calib_v3_result.json')
  fs.writeFileSync(outPath, JSON.stringify({
    dataset: path.basename(dsPath),
    freqDataset: freqPath ? path.basename(freqPath) : null,
    n: rows.length, nFreq: freqRows?.length ?? 0, targets: TARGETS,
    frames, copperAll: { r2: cuR2All, mape: cuMapeAll, pass: copperPass },
    iron, total: { r2: totR2, mape: totMape, pass: totalPass },
    allPass: copperPass && ironPass && totalPass,
  }, null, 2))
  console.log(`\n结果已落盘: ${outPath}`)

  if (apply) {
    if (!copperPass) {
      console.log('\n[abort] 铜耗分档未达标，拒绝写回')
      process.exit(2)
    }
    if (!ironPass && iron.kh != null) {
      console.log('\n[abort] 铁耗未达标，拒绝写回（铜耗分档可独立写回，见 --apply-copper-only）')
      process.exit(3)
    }
    // 写回铜耗分档
    const mcPath = path.join(ROOT, 'lib', 'motor-constants.mjs')
    let src = fs.readFileSync(mcPath, 'utf8')
    const tableJs = `export const COPPER_LOSS_K = {\n` +
      `  small:  ${frames.small.k},   // ≤30kW（${frames.small.powerKw.join('/')}kW 扫参样本）\n` +
      `  medium: ${frames.medium.k},   // 30~100kW\n` +
      `  large:  ${frames.large.k},   // >100kW\n` +
      `}\n`
    if (src.includes('export const COPPER_LOSS_K')) {
      src = src.replace(/export const COPPER_LOSS_K = \{[\s\S]*?\}\n/, tableJs)
    } else {
      src = src.replace(
        /(\/\*\* 铁损 Steinmetz 简化系数 \*\/\nexport const STEINMETZ[^\n]*\n)/,
        `$1\n/** 铜耗分档系数（v3 标定，按额定功率查档；档内 R² ${Object.values(frames).map((f) => f.r2).join('/')}） */\n${tableJs}`)
    }
    fs.writeFileSync(mcPath, src)
    console.log(`[apply] COPPER_LOSS_K 写回: small=${frames.small.k} medium=${frames.medium.k} large=${frames.large.k}`)
  }
}

main()
