/**
 * 篇三配图原始数据（真实运行产出，不做任何修饰）
 */
import { readFileSync } from 'node:fs'
import { runL0Pipeline } from './tools/l0/pipeline.mjs'
import { buildParamMatrix } from './tools/l0/param-matrix.mjs'
import { quickL0Estimate } from './lib/formula-engine.mjs'

console.log('########## FIG12 代理模型分族统计 ##########')
const m = JSON.parse(readFileSync('./models/l0_surrogate_family.json', 'utf8'))
console.log('schema      :', m.schema)
console.log('version     :', m.version)
console.log('features    :', JSON.stringify(m.features))
console.log('target      :', m.target)
console.log('gate        :', JSON.stringify(m.gate))
console.log('data_sources:', JSON.stringify(m.data_sources))
console.log('generated_at:', m.generated_at)
for (const [mk, key] of [['INDUCTION', 'induction_segments'], ['PMSM', 'pmsm_segments']]) {
  const segs = m[key]
  if (!segs) { console.log(`${mk}: (无)`); continue }
  console.log(`--- ${mk} ---`)
  for (const [name, s] of Object.entries(segs)) {
    const p = s.primary || {}
    console.log(`  ${name.padEnd(14)} od_range=[${s.od_range}]  n=${p.n}  cv_r2=${p.cv_r2}  coef_len=${(p.coef || []).length}`)
  }
}

console.log('\n########## FIG14 450kW 效率钳位 ##########')
const p450 = runL0Pipeline({ power_kw: 450, speed_rpm: 985, voltage_v: 690, motor_type: 'induction', poles: 6, count: 120, top_n: 10, write_handoff: false }, {})
console.log('summary:', JSON.stringify(p450.summary))
console.log('efficiency_cap:', p450.summary?.efficiency_cap ?? '(n/a)')
console.log('capped_count :', p450.efficiency_capped_count ?? '(n/a)')
console.log('#  OD    poles  efficiency  efficiency_raw  capped')
for (const r of p450.results.slice(0, 10)) {
  const pr = r.params || {}
  console.log(`   ${String(pr.stator_od).padEnd(6)}${String(pr.poles).padEnd(7)}${String(r.efficiency).padEnd(12)}${String(r.efficiency_raw ?? '(n/a)').padEnd(16)}${r.efficiency_capped ?? '(n/a)'}`)
}

console.log('\n########## FIG15 三案例 verdict 分布 ##########')
const CASES = [
  ['15kW/1460 异步', { power_kw: 15, speed_rpm: 1460, voltage_v: 380, motor_type: 'induction', count: 120 }],
  ['200kW/22000 PMSM', { power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm', cooling: 'liquid_jacket', count: 120 }],
  ['450kW/985 异步', { power_kw: 450, speed_rpm: 985, voltage_v: 690, motor_type: 'induction', poles: 6, count: 120 }],
]
for (const [name, spec] of CASES) {
  const rows = buildParamMatrix(spec, {}).matrix
  const est = rows.map((r) => { try { return quickL0Estimate(r, {}) } catch { return null } }).filter(Boolean)
  const dist = {}
  for (const e of est) dist[e.verdict] = (dist[e.verdict] ?? 0) + 1
  const th = est.filter((e) => e.verdict === 'infeasible_thermal')
  const cur = th.length ? Math.max(...th.map((e) => e.params?.peak_current ?? 0)) : 0
  const tt = th.length ? [Math.min(...th.map((e) => e.temp_rise)), Math.max(...th.map((e) => e.temp_rise))] : []
  const cu = th.length ? [Math.min(...th.map((e) => e.copper_loss)), Math.max(...th.map((e) => e.copper_loss))] : []
  console.log(`${name}: n=${est.length} ${JSON.stringify(dist)}`)
  const geo = est.filter((e) => e.verdict === 'infeasible_geometry')
  const thYs = th.filter((e) => e.yoke_sat === true).length
  const geoYs = geo.filter((e) => e.yoke_sat === true).length
  console.log(`    轭饱和交叉核对: thermal ${th.length} 行中 yoke_sat=true 有 ${thYs} 行; geometry ${geo.length} 行中 ${geoYs} 行`)
  if (th.length) {
    const yl = [Math.min(...th.map((e) => e.yoke_flux_density)), Math.max(...th.map((e) => e.yoke_flux_density))]
    console.log(`    thermal 行轭磁密: ${yl[0].toFixed(2)}~${yl[1].toFixed(2)}T  (判废限 1.9T)`)
  }
  if (th.length) console.log(`    thermal=${th.length} peak_current=${cur.toFixed(0)}A 温升=${tt[0]}~${tt[1]}K 铜损=${cu[0].toFixed(0)}~${cu[1].toFixed(0)}W (限值105K/F级)`)
  else console.log('    thermal=0')
}
