/**
 * scripts/regression.mjs —— W4 回归质量门运行器
 * ============================================================
 * 用法：
 *   node scripts/regression.mjs                 运行质量门（比对基线）
 *   node scripts/regression.mjs --update-baseline  重新生成基线快照（标定后必须显式执行）
 *   node scripts/regression.mjs --strict        DEBT 也按失败计（发布前可用）
 *   node scripts/regression.mjs --json          机器可读输出（供 CI 解析）
 *
 * 退出码：
 *   0  通过（PASS，或 DEBT 但未加 --strict）
 *   1  阻断（物理边界越界，或相对基线发生劣化）
 *   2  运行异常（标定集缺失/损坏）
 *
 * 两道门的职责见 lib/regression-gate.mjs 头部注释：
 *   门 A 物理边界门（sanity）  —— 越界即真 bug
 *   门 B 回归门（regression）  —— 不能比上次差
 *   诊断 divergence           —— L0 vs 仿真偏差，默认不阻断
 */

import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { evaluateSuite, makeBaseline, compareBaseline } from '../lib/regression-gate.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const BENCH_PATH = join(ROOT, 'benchmarks', 'l0-benchmarks.json')
const BASELINE_PATH = join(ROOT, 'benchmarks', 'baseline.json')

const argv = process.argv.slice(2)
const wantUpdate = argv.includes('--update-baseline')
const strict = argv.includes('--strict')
const asJson = argv.includes('--json')

const lines = []
const log = (s) => lines.push(s)

if (!existsSync(BENCH_PATH)) {
  console.error('❌ 标定集不存在: benchmarks/l0-benchmarks.json')
  process.exit(2)
}

let suite
try {
  suite = JSON.parse(readFileSync(BENCH_PATH, 'utf8'))
} catch (err) {
  console.error(`❌ 标定集解析失败: ${String(err?.message ?? err)}`)
  process.exit(2)
}

const evaluation = evaluateSuite(suite, {})

if (wantUpdate) {
  const baseline = { ...makeBaseline(evaluation), updated_at: new Date().toISOString() }
  writeFileSync(BASELINE_PATH, `${JSON.stringify(baseline, null, 2)}\n`, 'utf8')
  console.log(`✅ 基线已更新: benchmarks/baseline.json（${Object.keys(baseline.metrics).length} 个案例）`)
  process.exit(0)
}

const baseline = existsSync(BASELINE_PATH)
  ? JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))
  : null
const regression = compareBaseline(evaluation, baseline, suite.regression_tolerance ?? {})

if (asJson) {
  console.log(JSON.stringify({
    verdict: evaluation.verdict,
    sanity: evaluation.sanity,
    divergence: evaluation.divergence,
    regression,
    results: evaluation.results,
  }, null, 2))
} else {
  log('')
  log('══════ motor-ai-l0 · L0 回归质量门 ══════')
  log('')
  log(`预算目标：仿真适用域内 |Δη| ≤ ${evaluation.budget.efficiency_pt}pt，|ΔT| ≤ ${evaluation.budget.temp_k}K`)
  log('')
  log('┌─ 逐案例结果 ' + '─'.repeat(56))
  for (const r of evaluation.results) {
    const m = r.metrics
    const flag = r.sanity.status === 'fail' ? '❌' : r.sanity.status === 'warn' ? '⚠️' : '✅'
    log(`│ ${flag} ${r.id.padEnd(10, ' ')} η=${String(m.efficiency).padStart(5, ' ')}%  (带 ${m.efficiency_band?.join('~')}%, 偏离 ${m.efficiency_deviation_pt}pt)`)
    log(`│           ΔT=${String(m.temp_rise).padStart(6, ' ')}K  限值 ${m.temp_limit_assessment_k}/${m.temp_limit_design_k}K(考核/设计)  损耗 ${m.total_loss}W`)
    log(`│           诊断: L1 proxy η=${m.l1_efficiency_proxy}% T=${m.l1_temp_proxy}°C ⇒ Δη=${r.divergence.delta_efficiency_pt}pt ΔT=${r.divergence.delta_temp_k}K [${r.divergence.status}]`)
    for (const i of r.sanity.issues) {
      log(`│           ${i.level === 'fail' ? '❌' : '⚠️'} ${i.msg}`)
    }
  }
  log('└' + '─'.repeat(68))
  log('')
  log(`门 A 物理边界: ${evaluation.sanity.status.toUpperCase()}`
    + `（fail ${evaluation.sanity.failed.length} / warn ${evaluation.sanity.warned.length}）`
    + `  阻断=${evaluation.sanity.blocking ? '是' : '否（标定未收敛，仅计 DEBT）'}`)
  log(`诊断 偏差:     适用域内达标 ${evaluation.divergence.within} 例，`
    + `超预算 ${evaluation.divergence.debt.length} 例，`
    + `域外豁免 ${evaluation.divergence.out_of_domain.length} 例 [${evaluation.divergence.out_of_domain.join(',') || '无'}]`)
  log(`门 B 回归:     ${regression.baseline_found
    ? (regression.ok ? 'OK（无劣化）' : `${regression.regressions.length} 项劣化`)
    : '无基线（首次运行，请用 --update-baseline 生成）'}`)
  for (const g of regression.regressions) {
    log(`  ❌ ${g.id}.${g.metric}: ${g.baseline} → ${g.current}（劣化 ${g.delta}${g.unit ?? ''} > 容差 ${g.tolerance ?? ''}）`)
  }
  log('')
  log(`总判定: ${evaluation.verdict}${!regression.ok ? ' + REGRESSION' : ''}`)
  log('')
}

const blocked = evaluation.verdict === 'FAIL' || !regression.ok || (strict && evaluation.verdict === 'DEBT')

if (!asJson) console.log(lines.join('\n'))
process.exit(blocked ? 1 : 0)
