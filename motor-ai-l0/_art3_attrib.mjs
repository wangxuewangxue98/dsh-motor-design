/**
 * 篇三取证：直接估算 Top5 的逐规则判废归因 + v0.2.3/v0.2.4 Top1 变迁
 * 用 include_reports 拿到逐行规则明细
 */
import { buildParamMatrix } from './tools/l0/param-matrix.mjs'
import { runDesignValidate } from './tools/l0/design-validate.mjs'
import { runL0Estimate } from './tools/l0/l0-estimate.mjs'

const spec = { power_kw: 15, speed_rpm: 1460, voltage_v: 380, motor_type: 'induction', count: 120 }
const rows = buildParamMatrix(spec, {}).matrix

const v = runDesignValidate({ params_list: rows, include_reports: true }, {})
const rep = v.reports || []
const failedSet = new Set(v.failed_indices)

const e2 = runL0Estimate({ params_list: rows, top_n: 5, sort_by: 'efficiency' }, {})

console.log('=== 直接估算 Top5 的逐规则归因 ===')
console.log('#   OD    poles  slots  N   eta     行号  V08        V14        V16')
for (let i = 0; i < e2.results.length; i++) {
  const r = e2.results[i], p = r.params || {}
  const idx = rows.findIndex((x) => x.stator_od === p.stator_od && x.stator_id === p.stator_id && x.core_length === p.core_length && x.poles === p.poles && x.slots_stator === p.slots_stator)
  const rr = rep[idx] || {}
  const items = rr.issues || []
  const get = (id) => {
    const it = Array.isArray(items) ? items.find((z) => (z.rule || z.id || '').startsWith(id)) : null
    return it ? (it.level || '?') : 'ok'
  }
  console.log(`Top${i + 1} ${String(p.stator_od).padEnd(6)}${String(p.poles).padEnd(7)}${String(p.slots_stator).padEnd(7)}${String(p.turns_per_coil).padEnd(4)}${String(r.efficiency).padEnd(9)}${String(idx).padEnd(5)}${get('V08').padEnd(11)}${get('V14').padEnd(11)}${get('V16')}`)
}

console.log('\n=== 判废行按规则归因（全 120 行）===')
const tally = {}
for (const i of v.failed_indices) {
  const rr = rep[i] || {}
  for (const it of (rr.issues || [])) {
    if (it.level === 'failed') tally[it.rule] = (tally[it.rule] ?? 0) + 1
  }
}
console.log(JSON.stringify(tally))

console.log('\n=== 判废行的极数分布 ===')
const byPole = {}
for (const i of v.failed_indices) { const p = rows[i].poles; byPole[p + '极'] = (byPole[p + '极'] ?? 0) + 1 }
console.log(JSON.stringify(byPole))

console.log('\n=== 保留行的极数分布 ===')
const keptPole = {}
rows.forEach((r, i) => { if (!failedSet.has(i)) keptPole[r.poles + '极'] = (keptPole[r.poles + '极'] ?? 0) + 1 })
console.log(JSON.stringify(keptPole))

console.log('\n=== report 结构探针（行 0）===')
console.log(JSON.stringify(rep[0], null, 1).slice(0, 1200))
