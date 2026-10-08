/**
 * 经验回灌闭环（experience hub）—— capture / pair / shadow 三命令
 * ---------------------------------------------------------------------------
 * 背景（2026-10-08，10 万案例压测后授权项 2）：motor_type 透传修复（0.2.6 P0）后，
 * 代理通道的分族选择恢复正确。本模块把「公式通道（v3.1 已标定损耗）」的产出
 * 作为经验样本积累下来，用于评估现役代理模型是否值得触发正式重训。
 *
 * 三命令语义：
 *   capture  批量采样规格案例 → 公式通道求解 → 本地 JSONL 经验样本
 *            （本地文件、不出网、不外发；每行含 spec + 结论，供 pair/shadow 消费）
 *   pair     同一批样本上「现役代理模型 vs 公式通道」逐例配对，
 *            输出 MAE / bias / 覆盖率，按 motor_type × OD 段分组
 *   shadow   用 capture 样本拟合「影子分桶模型」（shadow，不落盘覆盖现役模型），
 *            与现役模型在同一评估集上对比 MAE —— 影子显著更优才建议正式重训
 *
 * 设计红线：
 *  1. capture 只写本地 `models/experience/`，**永不自动出网/上报**（对齐 case-feedback 红线 4）；
 *  2. shadow 只输出评估结论，**不覆盖** `models/l0_surrogate*.json`（影子语义）；
 *  3. 现役模型缺文件/超范围时如实降级为 fallback 统计，不伪造预测值。
 *
 * 用法：
 *   node tools/feedback/experience-hub.mjs capture --n 2000
 *   node tools/feedback/experience-hub.mjs pair    --in models/experience/captured-<ts>.jsonl
 *   node tools/feedback/experience-hub.mjs shadow  --in models/experience/captured-<ts>.jsonl
 */

import { writeFileSync, readFileSync, existsSync, mkdirSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildParamMatrix } from '../l0/param-matrix.mjs'
import { runL0Estimate } from '../l0/l0-estimate.mjs'
import { predictSurrogate, loadSurrogateModel } from '../../lib/surrogate-engine.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const EXPERIENCE_DIR = resolve(ROOT, 'models', 'experience')

// ---------------------------------------------------------------------------
// 采样域：覆盖三个机座档 + 两类电机 + VFD 频率域（对齐 v3 标定扫参域）
// ---------------------------------------------------------------------------
const OD_BINS = [
  { name: 'small', maxKw: 30 },
  { name: 'medium', maxKw: 100 },
  { name: 'large', maxKw: Infinity },
]

/** 确定性伪随机（mulberry32）—— 影子评估必须可复现，禁用 Math.random */
function mulberry32(seed) {
  return function () {
    let t = (seed += 0x6d2b79f5)
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function sampleSpec(rand, i) {
  const powerKw = [7.5, 15, 22, 30, 45, 55, 75, 90, 110, 160, 200, 250, 315, 450][Math.floor(rand() * 14)]
  const speeds = [730, 960, 1460, 1480, 2960, 1500, 1800, 2500, 3000]
  const speedRpm = speeds[Math.floor(rand() * speeds.length)]
  const voltage = powerKw >= 160 ? [380, 660, 690, 1140][Math.floor(rand() * 4)] : [380, 660][Math.floor(rand() * 2)]
  const motorType = rand() < 0.25 ? 'pmsm' : 'induction'
  const lineFreqHz = rand() < 0.3 ? [25, 50, 60, 100, 200][Math.floor(rand() * 5)] : 50
  const cooling = powerKw >= 160 ? (rand() < 0.5 ? 'liquid_jacket' : 'forced_air') : 'forced_air'
  return { power_kw: powerKw, speed_rpm: speedRpm, voltage_v: voltage, motor_type: motorType, line_freq_hz: lineFreqHz, cooling, count: 40, _seed: i }
}

/** 简单 spec 哈希（评估集/训练集确定性二分用） */
function specHash(spec) {
  const s = `${spec.power_kw}|${spec.speed_rpm}|${spec.voltage_v}|${spec.motor_type}|${spec.line_freq_hz}|${spec.cooling}`
  let h = 2166136261
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) }
  return (h >>> 0) / 4294967296
}

// ---------------------------------------------------------------------------
// capture
// ---------------------------------------------------------------------------
function cmdCapture(args) {
  const n = parseInt(args.n ?? '2000', 10)
  const rand = mulberry32(20261008)
  const outDir = EXPERIENCE_DIR
  mkdirSync(outDir, { recursive: true })
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
  const outPath = resolve(outDir, `captured-${stamp}.jsonl`)

  const lines = []
  let matrixSkipped = 0
  for (let i = 0; i < n; i++) {
    const spec = sampleSpec(rand, i)
    let built
    try {
      built = buildParamMatrix(spec, { maxMatrixSize: 2000 })
    } catch (_) { matrixSkipped++; continue }
    if (!built.matrix?.length) { matrixSkipped++; continue }
    let est
    try {
      est = runL0Estimate({ params_list: built.matrix, top_n: 0, sort_by: 'efficiency' }, {})
    } catch (_) { matrixSkipped++; continue }
    for (const row of est.results ?? []) {
      const p = row.params ?? {}
      // 本地经验样本（models/experience/ 不出网）：存代理模型所需的全部特征
      // （features = stator_od/stator_id/core_length/poles + l0_eff）与结论字段。
      lines.push(JSON.stringify({
        motor_type: p.motor_type ?? 'induction',
        stator_od: p.stator_od, stator_id: p.stator_id,
        core_length: p.core_length, poles: p.poles,
        power_kw: p.power_kw, speed_rpm: p.speed_rpm, voltage_v: p.voltage_v,
        line_freq_hz: p.line_freq_hz ?? 50, cooling: p.cooling,
        efficiency_raw: row.efficiency_raw,
        feasible: row.feasible === true,
        verdict: row.verdict,
      }))
    }
  }
  writeFileSync(outPath, lines.join('\n'), 'utf8')
  console.log(`capture: ${lines.length} 样本 → ${outPath}`)
  console.log(`  spec 跳过（矩阵空/估算异常）: ${matrixSkipped}/${n}`)
  const feas = lines.filter((l) => JSON.parse(l).feasible).length
  console.log(`  可行样本: ${feas}/${lines.length}（${(feas / Math.max(1, lines.length) * 100).toFixed(1)}%）`)
  return outPath
}

// ---------------------------------------------------------------------------
// pair：现役代理模型 vs 公式通道真值（capture 记录值）
// ---------------------------------------------------------------------------
function predictWithModel(model, row) {
  if (!model) return { skipped: 'no_model' }
  try {
    // 模型特征 = stator_od/stator_id/core_length/poles/l0_eff（与 l0-estimate 富化口径一致：
    // l0_eff 取公式通道效率，此处用未封顶 raw，等价于富化时的 efficiencyCap=99）
    const pred = predictSurrogate(
      {
        stator_od: row.stator_od, stator_id: row.stator_id, core_length: row.core_length,
        poles: row.poles, l0_eff: row.efficiency_raw,
      },
      model,
      row.motor_type,
    )
    if (pred === null) return { skipped: 'out_of_range' }
    return { value: pred.efficiency, confidence: pred.confidence }
  } catch (err) {
    return { skipped: err.message }
  }
}

function groupStats(rows, keyFn) {
  const g = new Map()
  for (const r of rows) {
    const k = keyFn(r)
    if (!g.has(k)) g.set(k, { n: 0, absSum: 0, biasSum: 0 })
    const s = g.get(k)
    s.n++; s.absSum += Math.abs(r.pair_err); s.biasSum += r.pair_err
  }
  return [...g.entries()].map(([k, s]) => ({
    group: k, n: s.n,
    mae: +(s.absSum / s.n).toFixed(3),
    bias: +(s.biasSum / s.n).toFixed(3),
  })).sort((a, b) => a.group.localeCompare(b.group))
}

function loadRows(path) {
  if (!existsSync(path)) { console.error(`样本文件不存在: ${path}`); process.exit(1) }
  return readFileSync(path, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l))
}

function cmdPair(args) {
  const rows = loadRows(args.in)
  const modelPath = args.model ?? resolve(ROOT, 'models', 'l0_surrogate_family.json')
  const model = existsSync(modelPath) ? loadSurrogateModel(modelPath) : null
  const usable = rows.filter((r) => typeof r.efficiency_raw === 'number' && r.feasible)

  let paired = 0; let skipped = 0
  for (const r of usable) {
    const p = predictWithModel(model, r)
    if (p.skipped !== undefined || typeof p.value !== 'number') { r._skip = p.skipped ?? 'bad_pred'; skipped++ }
    else { r.pair_err = r.efficiency_raw - p.value; paired++ }
  }
  const errRows = usable.filter((r) => typeof r.pair_err === 'number')
  // 跳过原因分布 —— out_of_range 大多是「pmsm 小 OD 无训练段」（模型 pmsm_segments
  // od_range 下限 612mm），属训练域覆盖问题而非预测缺陷，必须与「误判率」区分开
  const skipReasons = {}
  for (const r of usable) if (r._skip) skipReasons[r._skip] = (skipReasons[r._skip] ?? 0) + 1
  const mae = errRows.reduce((s, r) => s + Math.abs(r.pair_err), 0) / Math.max(1, errRows.length)
  const bias = errRows.reduce((s, r) => s + r.pair_err, 0) / Math.max(1, errRows.length)

  console.log(`pair: 现役模型=${model ? 'l0_surrogate_family' : '（缺文件，全跳过）'}`)
  console.log(`  可行样本 ${usable.length}，配对成功 ${paired}，跳过 ${skipped}（超训练域/无模型）`)
  console.log(`  跳过原因分布: ${JSON.stringify(skipReasons)}`)
  console.log(`  整体 MAE=${mae.toFixed(3)}pt  bias=${bias >= 0 ? '+' : ''}${bias.toFixed(3)}pt（正=公式通道更高）`)
  if (errRows.length) {
    console.log('  分组（motor_type × OD 档）:')
    for (const g of groupStats(errRows, (r) => {
      const bin = OD_BINS.find((b) => r.power_kw <= b.maxKw)?.name ?? 'large'
      return `${r.motor_type}/${bin}`
    })) console.log(`    ${g.group.padEnd(14)} n=${String(g.n).padStart(5)}  MAE=${g.mae.toFixed(2)}  bias=${g.bias >= 0 ? '+' : ''}${g.bias}`)
  }
  const skipRate = skipped / Math.max(1, usable.length)
  console.log(`  跳过率=${(skipRate * 100).toFixed(1)}% —— 若主导原因是 out_of_range(pmsm 小 OD 无段)，` +
    `属训练域覆盖缺口（重训待办），不是 0.2.5 压测的「误判 pmsm」缺陷（该缺陷已由 P0 修复）`)
}

// ---------------------------------------------------------------------------
// shadow：影子分桶模型 vs 现役模型（同评估集，不落盘覆盖）
// ---------------------------------------------------------------------------
function fitShadowModel(trainRows) {
  // 影子模型：motor_type × OD 桶 × 转速档 的效率均值回归（最朴素的分段基线，
  // 用于给正式重训（Python 侧 M1/M2/M3 管线）提供「值得/不值得」的证据）
  const buckets = new Map()
  for (const r of trainRows) {
    const bin = OD_BINS.find((b) => r.power_kw <= b.maxKw)?.name ?? 'large'
    const speedBand = r.speed_rpm < 1000 ? 'slow' : r.speed_rpm < 2000 ? 'mid' : 'fast'
    const k = `${r.motor_type}|${bin}|${speedBand}`
    if (!buckets.has(k)) buckets.set(k, { sum: 0, n: 0 })
    const b = buckets.get(k); b.sum += r.efficiency_raw; b.n++
  }
  const model = new Map()
  for (const [k, b] of buckets) model.set(k, b.sum / b.n)
  return model
}

function cmdShadow(args) {
  const rows = loadRows(args.in)
    .filter((r) => typeof r.efficiency_raw === 'number' && r.feasible)
  if (rows.length < 100) { console.error('可行样本不足 100，影子评估无意义'); process.exit(1) }

  // 训练/评估 确定性二分（hash 前半训练、后半评估）
  const train = rows.filter((r) => specHash(r) < 0.5)
  const evalSet = rows.filter((r) => specHash(r) >= 0.5)
  const shadow = fitShadowModel(train)

  const modelPath = args.model ?? resolve(ROOT, 'models', 'l0_surrogate_family.json')
  const model = existsSync(modelPath) ? loadSurrogateModel(modelPath) : null

  let sPaired = 0, sAbs = 0
  for (const r of evalSet) {
    const bin = OD_BINS.find((b) => r.power_kw <= b.maxKw)?.name ?? 'large'
    const speedBand = r.speed_rpm < 1000 ? 'slow' : r.speed_rpm < 2000 ? 'mid' : 'fast'
    const v = shadow.get(`${r.motor_type}|${bin}|${speedBand}`)
    if (typeof v === 'number') { sPaired++; sAbs += Math.abs(r.efficiency_raw - v) }
  }
  let mPaired = 0, mAbs = 0, mSkip = 0
  for (const r of evalSet) {
    const p = predictWithModel(model, r)
    if (typeof p.value === 'number') { mPaired++; mAbs += Math.abs(r.efficiency_raw - p.value) }
    else mSkip++
  }
  const shadowMae = sPaired ? sAbs / sPaired : NaN
  const modelMae = mPaired ? mAbs / mPaired : NaN

  console.log(`shadow: 训练 ${train.length} / 评估 ${evalSet.length}（确定性二分，可复现）`)
  console.log(`  影子分桶模型: 覆盖 ${sPaired}/${evalSet.length}，MAE=${shadowMae.toFixed(3)}pt`)
  console.log(`  现役代理模型: 覆盖 ${mPaired}/${evalSet.length}（跳过 ${mSkip}），MAE=${Number.isNaN(modelMae) ? 'N/A' : modelMae.toFixed(3) + 'pt'}`)

  if (Number.isNaN(modelMae)) {
    console.log('  结论: 现役模型在评估集大面积跳过/缺文件 —— 建议触发正式重训（先修训练域覆盖）')
  } else if (shadowMae < modelMae * 0.8) {
    console.log(`  结论: 影子模型 MAE 优于现役 ${(100 * (1 - shadowMae / modelMae)).toFixed(0)}% —— 建议触发正式重训（导出 capture 样本给 Python 侧）`)
  } else {
    console.log('  结论: 影子基线未显著优于现役 —— 暂不重训，继续 capture 积累')
  }
  console.log('  （shadow 未写任何模型文件 —— 影子语义）')
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------
const [, , cmd, ...rest] = process.argv
const args = Object.fromEntries(rest.flatMap((a, i) => {
  if (!a.startsWith('--')) return []
  const v = rest[i + 1] && !rest[i + 1].startsWith('--') ? rest[i + 1] : true
  return [[a.slice(2), v]]
}))

try {
  if (cmd === 'capture') cmdCapture(args)
  else if (cmd === 'pair') cmdPair(args)
  else if (cmd === 'shadow') cmdShadow(args)
  else {
    console.log('用法: experience-hub.mjs <capture|pair|shadow> [--n 2000] [--in <jsonl>] [--model <path>]')
    process.exit(cmd ? 1 : 0)
  }
} catch (err) {
  console.error(`experience-hub ${cmd} 失败:`, err.message)
  process.exit(1)
}
