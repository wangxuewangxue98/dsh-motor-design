/**
 * 损耗模型标定 v2 —— 控制变量扫参数据专用
 * ---------------------------------------------------------------------------
 * 数据：`<MotorDesign>/output/loss_calib/cv_sweep_dataset.json`（可用 `--dataset` 或
 *      环境变量 `L0B_CALIB_DATASET` 指定）
 *   由 Scripts/l0_training/export_cv_dataset.py 从 source_path LIKE 'cvsweep_%' 导出。
 *
 * ════ 为什么必须换采集形态（v1 失败的根因）════
 *   v1 用跨功率档随机抽样（15~450kW 混合），得到：
 *     铜耗 R²=0.107 · 铁耗 R²=0.685 · 总损 R²=0.296，全部远低于阈值。
 *   诊断结论**不是系数没调好**，而是信息量不匹配：
 *     · L0 反推匝数 vs RMxprt 实际匝数 R²=0.962（高度相关）
 *     · 但铜损比值跨 420 倍（0.025~10.5）⇒ 任何乘性系数都无解
 *   根因：跨档抽样时几何/磁密/电流密度同时变化，模型误差与参数变化混淆。
 *
 * ════ v2 的采集形态（本次）════
 *   固定工况档 + **固定几何** + 只扫 turns_per_coil（单变量）
 *     档位：15kW/380V/1460rpm/4极 · 75kW/660V/1480rpm/4极 · 450kW/690V/985rpm/6极
 *     每档几何锁死（OD/Dsi/L/Qs/a 全不变），N 扫 2.6~2.8 倍跨度
 *     数据实测：Cu/Fe 比值跨度 4.3~17.2×（v1 全局仅 1.3×）
 *
 * ════ 标定手法（本版与 v1 的关键差异）════
 *   v1 用 `deriveElectricalClosure` 从规格反推电流/匝数，再比乘性系数
 *      ⇒ 电流本身就是模型输出，比值必然被模型锁定，这是 R² 上不去的结构原因。
 *   v2 **直接用 RMxprt 真值电流与实际匝数**作为模型输入，只标定损耗系数本身：
 *      · 铜耗：真值 I 与真值 N 代入公式，看系数偏差是否稳定（若稳定 ⇒ 可标定）
 *      · 铁耗：仍解 [f·B^α, f²·B²] 双基（几何固定 ⇒ f 固定 ⇒ 只剩幅度可辨识）
 *
 * 用法：
 *   node scripts/calibrate_losses_v2.mjs                     # 标定并打印报告
 *   node scripts/calibrate_losses_v2.mjs --apply             # 写回 motor-constants.mjs
 *   node scripts/calibrate_losses_v2.mjs --dataset <path>
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
const DEFAULT_DATASET = process.env.L0B_CALIB_DATASET || null   // 必须由 --dataset 或环境变量显式指定

/** 验收阈值（沿用规格 §验收） */
const TARGETS = { copper: 0.85, iron: 0.80, total: 0.85 }

/**
 * 用 RMxprt 真值电流（peak = I_rms·√2）与真值匝数代入插件铜损公式。
 * ⚠ 与生产的差别：生产用 deriveElectricalClosure 从规格反推 `peak_current`，
 *   这里是标定专用口径 —— 只检验损耗公式本身的系数偏差。
 */
function predictCopper(c) {
  const iRms = c.stator_current
  const cu = estimateCopperLoss({
    statorOd: c.stator_od,
    statorId: c.stator_id,
    coreLength: c.core_length,
    slotsStator: c.slots_stator,
    poles: c.poles,
    peakCurrent: iRms * Math.SQRT2,   // 真值线电流 → 峰值口径
    turnsPerCoil: c.turns_per_coil,
    parallelCircuits: c.parallel_circuits,
    airGap: 0.6,
  })
  return cu.copper_loss
}

function predictIron(c) {
  const coreMassKg = estimateCoreMassKg(c.stator_od, c.stator_id, c.core_length)
  return {
    pIron: estimateIronLoss({
      speedRpm: c.speed_rpm,
      poles: c.poles,
      coreMassKg,
      fluxDensity: STEINMETZ.fluxDensity,
    }),
    coreMassKg,
  }
}

function predictMech(c) {
  const rotorOdMm = Math.round((c.stator_id - 2 * 0.6) * 10) / 10
  return estimateMechanicalLoss({ speedRpm: c.speed_rpm, rotorOdMm })
}

/** 无截距一元最小二乘 k = Σxy/Σx² */
function fitScale(xs, ys) {
  let sxy = 0, sxx = 0
  for (let i = 0; i < xs.length; i += 1) { sxy += xs[i] * ys[i]; sxx += xs[i] * xs[i] }
  return sxx > 0 ? sxy / sxx : 1
}

/** 无截距二元最小二乘（正规方程 2x2） */
function fitTwoBases(b1, b2, y) {
  let a11 = 0, a12 = 0, a22 = 0, v1 = 0, v2 = 0
  for (let i = 0; i < y.length; i += 1) {
    a11 += b1[i] * b1[i]; a12 += b1[i] * b2[i]; a22 += b2[i] * b2[i]
    v1 += b1[i] * y[i]; v2 += b2[i] * y[i]
  }
  const det = a11 * a22 - a12 * a12
  if (Math.abs(det) < 1e-9) return { k1: 1, k2: 0, ok: false }
  return { k1: (v1 * a22 - a12 * v2) / det, k2: (a11 * v2 - a12 * v1) / det, ok: true }
}

/** R²（带最优比例系数的口径） */
function r2(xs, ys) {
  const n = ys.length
  const k = fitScale(xs, ys)
  const sse = ys.reduce((s, y, i) => s + (y - k * xs[i]) ** 2, 0)
  const sst = ys.reduce((s, y) => s + y * y, 0)
  return sst > 0 ? 1 - sse / sst : 0
}

/** MAPE（%） */
function mape(xs, ys) {
  let s = 0
  for (let i = 0; i < ys.length; i += 1) {
    if (xs[i] !== 0) s += Math.abs(ys[i] - xs[i]) / Math.abs(ys[i])
  }
  return (s / ys.length) * 100
}

function main() {
  const argv = process.argv.slice(2)
  const apply = argv.includes('--apply')
  const dsIdx = argv.indexOf('--dataset')
  const dsPath = dsIdx >= 0 ? argv[dsIdx + 1] : DEFAULT_DATASET

  if (!dsPath) {
    console.error('缺少数据集。请用 --dataset <path> 或环境变量 L0B_CALIB_DATASET 指定。')
    console.error('生成方：MotorDesign Scripts/l0_training/export_cv_dataset.py')
    process.exit(1)
  }
  if (!fs.existsSync(dsPath)) {
    console.error(`数据集不存在: ${dsPath}`)
    process.exit(1)
  }
  const ds = JSON.parse(fs.readFileSync(dsPath, 'utf8'))
  const rows = ds.rows
  console.log('═'.repeat(78))
  console.log('损耗模型标定 v2 —— 控制变量扫参数据集')
  console.log('═'.repeat(78))
  console.log(`数据源: ${dsPath}`)
  console.log(`样本: ${rows.length} 行（原始 ${ds.meta?.n_raw ?? '-'}，剔除 ${
    JSON.stringify(ds.meta?.dropped ?? {})}）`)
  const tags = [...new Set(rows.map((r) => r.tag))]
  for (const t of tags) {
    const sub = rows.filter((r) => r.tag === t)
    const ns = sub.map((r) => r.turns_per_coil)
    const cu = sub.map((r) => r.copper_loss)
    const fe = sub.map((r) => r.iron_loss)
    console.log(`  ${t.padEnd(6)} n=${String(sub.length).padStart(2)}  ` +
      `N∈[${Math.min(...ns)},${Math.max(...ns)}] (${(Math.max(...ns) / Math.min(...ns)).toFixed(2)}×)  ` +
      `Cu∈[${Math.min(...cu).toFixed(0)},${Math.max(...cu).toFixed(0)}]W  ` +
      `Fe∈[${Math.min(...fe).toFixed(0)},${Math.max(...fe).toFixed(0)}]W`)
  }

  // ── 铜耗 ──
  const cuL0 = rows.map(predictCopper)
  const cuTrue = rows.map((r) => r.copper_loss)
  const kCu = fitScale(cuL0, cuTrue)
  const cuR2 = r2(cuL0, cuTrue)
  const cuMape = mape(cuL0.map((x) => x * kCu), cuTrue)
  const cuRatios = rows.map((r, i) => cuTrue[i] / cuL0[i])

  console.log('\n' + '─'.repeat(78))
  console.log('【铜耗标定】  P_cu = 3·I²·R，I/N 均用 RMxprt 真值')
  console.log('─'.repeat(78))
  console.log(`  L0 预测均值 ${(cuL0.reduce((a, b) => a + b, 0) / cuL0.length).toFixed(1)} W` +
    `  真值均值 ${(cuTrue.reduce((a, b) => a + b, 0) / cuTrue.length).toFixed(1)} W`)
  console.log(`  比例系数 k_cu = ${kCu.toFixed(4)}`)
  console.log(`  R²     = ${cuR2.toFixed(4)}   (阈值 ${TARGETS.copper})  ` +
    `${cuR2 >= TARGETS.copper ? '✅ 达标' : '❌ 未达标'}`)
  console.log(`  MAPE   = ${cuMape.toFixed(1)}%`)
  console.log(`  比值范围 ${Math.min(...cuRatios).toFixed(3)} ~ ${Math.max(...cuRatios).toFixed(3)}` +
    `  （极差 ${(Math.max(...cuRatios) / Math.min(...cuRatios)).toFixed(1)}×）`)

  // 分档看比值稳定性
  console.log('  分档 k_cu：')
  for (const t of tags) {
    const idx = rows.map((r, i) => (r.tag === t ? i : -1)).filter((i) => i >= 0)
    const xs = idx.map((i) => cuL0[i])
    const ys = idx.map((i) => cuTrue[i])
    const k = fitScale(xs, ys)
    console.log(`    ${t.padEnd(6)} k=${k.toFixed(3)}  R²=${r2(xs, ys).toFixed(4)}  n=${idx.length}`)
  }

  // ── 铁耗 ──
  const feL0 = rows.map((r) => predictIron(r).pIron)
  const feTrue = rows.map((r) => r.iron_loss)
  const feF = rows.map((r) => (r.speed_rpm * r.poles) / 120)
  const b1 = feF.map((f) => f)
  const b2 = feF.map((f) => f * f)
  const feFit = fitTwoBases(b1, b2, feTrue)
  const kFe = fitScale(feL0, feTrue)
  const feR2 = r2(feL0, feTrue)

  console.log('\n' + '─'.repeat(78))
  console.log('【铁耗标定】  P_fe = kh·f·B^α + ke·f²·B²')
  console.log('─'.repeat(78))
  console.log(`  L0 预测均值 ${(feL0.reduce((a, b) => a + b, 0) / feL0.length).toFixed(1)} W` +
    `  真值均值 ${(feTrue.reduce((a, b) => a + b, 0) / feTrue.length).toFixed(1)} W`)
  console.log(`  整体比例 k_fe = ${kFe.toFixed(4)}   R² = ${feR2.toFixed(4)}  ` +
    `(${feR2 >= TARGETS.iron ? '✅' : '❌'} 阈值 ${TARGETS.iron})`)
  console.log(`  双基解: kh∝${feFit.k1.toExponential(3)}  ke∝${feFit.k2.toExponential(3)}  ` +
    `${feFit.ok ? '' : '(病态: 两基共线)'}`)
  const feRatios = rows.map((r, i) => feTrue[i] / feL0[i])
  console.log(`  比值范围 ${Math.min(...feRatios).toFixed(3)} ~ ${Math.max(...feRatios).toFixed(3)}` +
    `  （极差 ${(Math.max(...feRatios) / Math.min(...feRatios)).toFixed(1)}×）`)

  // ── 机械耗（真值 = total − cu − fe）──
  const mechTrue = rows.map((r) => r.mech_loss_real)
  const mechL0 = rows.map(predictMech)
  const kMech = fitScale(mechL0, mechTrue)
  const mechR2 = r2(mechL0, mechTrue)

  console.log('\n' + '─'.repeat(78))
  console.log('【机械耗标定】  真值 = total − cu − fe（自洽推导）')
  console.log('─'.repeat(78))
  console.log(`  k_mech = ${kMech.toFixed(4)}   R² = ${mechR2.toFixed(4)}`)

  // ── 总损 ──
  const totL0 = rows.map((r, i) => cuL0[i] + feL0[i] + mechL0[i])
  const totTrue = rows.map((r) => r.total_loss)
  const kTot = fitScale(totL0, totTrue)
  const totR2 = r2(totL0, totTrue)
  const totMape = mape(totL0.map((x) => x * kTot), totTrue)

  console.log('\n' + '─'.repeat(78))
  console.log('【总损标定】')
  console.log('─'.repeat(78))
  console.log(`  k_tot = ${kTot.toFixed(4)}   R² = ${totR2.toFixed(4)}  ` +
    `(${totR2 >= TARGETS.total ? '✅' : '❌'} 阈值 ${TARGETS.total})   MAPE = ${totMape.toFixed(1)}%`)

  // ── 汇总 ──
  const results = {
    copper: { k: kCu, r2: cuR2, mape: cuMape, pass: cuR2 >= TARGETS.copper },
    iron: { k: kFe, r2: feR2, pass: feR2 >= TARGETS.iron, twoBase: feFit },
    mech: { k: kMech, r2: mechR2 },
    total: { k: kTot, r2: totR2, mape: totMape, pass: totR2 >= TARGETS.total },
  }
  const allPass = results.copper.pass && results.iron.pass && results.total.pass

  console.log('\n' + '='.repeat(78))
  console.log('汇总')
  console.log('='.repeat(78))
  console.log(`  铜耗  k=${kCu.toFixed(4)}  R²=${cuR2.toFixed(4)}  ${results.copper.pass ? '达标' : '未达标'}`)
  console.log(`  铁耗  k=${kFe.toFixed(4)}  R²=${feR2.toFixed(4)}  ${results.iron.pass ? '达标' : '未达标'}`)
  console.log(`  总损  k=${kTot.toFixed(4)}  R²=${totR2.toFixed(4)}  MAPE=${totMape.toFixed(1)}%  ${results.total.pass ? '达标' : '未达标'}`)
  console.log(`\n  总体: ${allPass ? '✅ 全部达标，可写回' : '❌ 未全部达标 —— 不写回（避免口径错比不标定更糟）'}`)

  const outPath = path.join(ROOT, 'scripts', 'loss_calib_v2_result.json')
  fs.writeFileSync(outPath, JSON.stringify({
    dataset: dsPath, n: rows.length, targets: TARGETS,
    results, allPass,
    note: '机械耗真值为 total−cu−fe；铜/铁/总损为 RMxprt 直读真值',
  }, null, 2))
  console.log(`\n结果已落盘: ${outPath}`)

  if (apply) {
    if (!allPass) {
      console.log('\n[abort] 未全部达标，拒绝写回 motor-constants.mjs')
      process.exit(2)
    }
    const mcPath = path.join(ROOT, 'lib', 'motor-constants.mjs')
    let src = fs.readFileSync(mcPath, 'utf8')
    const khBefore = STEINMETZ.kh, keBefore = STEINMETZ.ke
    const newKh = khBefore * feFit.k1
    const newKe = keBefore * feFit.k2
    if (!Number.isFinite(newKh) || newKh <= 0 || !Number.isFinite(newKe) || newKe < 0) {
      console.log(`[abort] 解出的系数非物理 (kh=${newKh}, ke=${newKe})，拒绝写回`)
      process.exit(3)
    }
    src = src.replace(/kh:\s*[\d.eE+-]+/, `kh: ${newKh}`)
    src = src.replace(/ke:\s*[\d.eE+-]+/, `ke: ${newKe}`)
    fs.writeFileSync(mcPath, src)
    console.log(`[apply] STEINMETZ.kh ${khBefore} → ${newKh}`)
    console.log(`[apply] STEINMETZ.ke ${keBefore} → ${newKe}`)
  }
}

main()