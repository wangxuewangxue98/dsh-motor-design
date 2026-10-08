/**
 * 篇三取证：15kW 三步链（v0.2.4 含 V16）真实 Top1 与口径计数
 */
import { buildParamMatrix } from './tools/l0/param-matrix.mjs'
import { runDesignValidate } from './tools/l0/design-validate.mjs'
import { runL0Estimate } from './tools/l0/l0-estimate.mjs'
import { runL0Pipeline } from './tools/l0/pipeline.mjs'

const spec = { power_kw: 15, speed_rpm: 1460, voltage_v: 380, motor_type: 'induction', count: 120 }

console.log('=== A. 标准三步链（15kW/1460/380V/induction）===')
const rows = buildParamMatrix(spec, {}).matrix
const v = runDesignValidate({ params_list: rows }, {})
console.log('summary  :', JSON.stringify(v.summary))
console.log('rule_hits:', JSON.stringify(v.rule_hits))
const kept = rows.filter((_, i) => !v.failed_indices.includes(i))
const e = runL0Estimate({ params_list: kept, top_n: 5, sort_by: 'efficiency' }, {})
console.log('可行 feasible_count =', e.feasible_count)
for (let i = 0; i < Math.min(5, e.results.length); i++) {
  const r = e.results[i], p = r.params || {}
  console.log(`  Top${i + 1} OD${p.stator_od} / ID${p.stator_id} / L${p.core_length} / ${p.poles}极 / ${p.slots_stator}槽 / 匝${p.turns_per_coil} / eta=${r.efficiency} raw=${r.efficiency_raw ?? '(n/a)'} capped=${r.efficiency_capped ?? '(n/a)'} 轭${r.yoke_flux_density ?? r.yoke_flux ?? '(n/a)'}T`)
}

console.log('\n=== B. 聚合工具（同一 spec）===')
const pipe = runL0Pipeline({ ...spec, top_n: 5, write_handoff: false }, {})
console.log('summary:', JSON.stringify(pipe.summary))
console.log('Top1   :', JSON.stringify({ od: pipe.results?.[0]?.params?.stator_od, poles: pipe.results?.[0]?.params?.poles, eff: pipe.results?.[0]?.efficiency, raw: pipe.results?.[0]?.efficiency_raw }))
console.log('elapsed_ms:', pipe.elapsed_ms)

console.log('\n=== C. 直接估算（跳过 validate）Top5 ===')
const e2 = runL0Estimate({ params_list: rows, top_n: 5, sort_by: 'efficiency' }, {})
const rep = runDesignValidate({ params_list: rows, include_reports: true }, {}).reports || []
for (let i = 0; i < e2.results.length; i++) {
  const r = e2.results[i], p = r.params || {}
  const idx = rows.findIndex((x) => x.stator_od === p.stator_od && x.stator_id === p.stator_id && x.core_length === p.core_length && x.poles === p.poles && x.slots_stator === p.slots_stator)
  const fails = (rep[idx]?.issues || []).filter((z) => z.level === 'failed').map((z) => z.rule)
  console.log(`  Top${i + 1} OD${p.stator_od} ${p.poles}极/${p.slots_stator}槽 行${idx} N=${p.turns_per_coil} eta=${r.efficiency}  判废=${fails.length ? fails.join('+') : '无'}`)
}
console.log('  直接估算 feasible_count =', e2.feasible_count, ' <- 其中含 V16 判废的 6 极行')
