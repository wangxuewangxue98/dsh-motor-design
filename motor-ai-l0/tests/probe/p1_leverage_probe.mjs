// P1-3/P1-4/P1-5 功能探针（l0 0.5.4）
import { runDesignValidate } from '../../tools/l0/design-validate.mjs'
import { runL0Pipeline } from '../../tools/l0/pipeline.mjs'

const base = {
  stator_od: 210, stator_id: 136, rotor_od: 134.4, core_length: 160, air_gap: 0.8,
  poles: 4, slots_stator: 36, tooth_width: 6.8, yoke_thickness: 14, // 14 薄轭，刻意触发 V08
  turns_per_coil: 22, parallel_circuits: 2, peak_current: 25, voltage: 380,
  speed: 1480, power_kw: 15, motor_type: 'induction',
}
const cfg = { efficiencyCap: 96, tempRiseRange: [45, 130] }

// ---- P1-3 sweep ----
const sw = runDesignValidate({
  sweep_param: 'yoke_thickness',
  sweep_values: [12, 14, 16, 18, 20, 24, 28, 32],
  base_params: base,
}, { ...cfg })
console.log('[sweep] mode =', sw.mode, 'swept =', sw.swept)
console.log('[sweep] best =', JSON.stringify(sw.best))
console.log('[sweep] statuses =', sw.rows.map((r) => `${r.yoke_thickness}:${r.status}`).join(' '))
console.log('[sweep] proxy =', sw.temp_rise_is_proxy)

// ---- P1-4 solve_turns ----
const st = runDesignValidate({ solve_turns: base }, { ...cfg })
console.log('[solve_turns] mode =', st.mode, 'rec =', st.recommendation)
console.log('[solve_turns] electrical.turns_ideal =', st.electrical?.turns_ideal,
  'f_hz =', st.electrical?.f_hz)
console.log('[solve_turns] options =', JSON.stringify(st.integer_options?.map((o) => ({
  t: o.turns, dev: o.dev_pct, st: o.status, eff: o.efficiency_raw,
}))))

// ---- P1-5 leverage_audit ----
const la = runDesignValidate({ leverage_audit: base }, { ...cfg })
console.log('[audit] mode =', la.mode, 'status =', la.status)
console.log('[audit] actionable =', JSON.stringify(la.actionable, null, 1))
console.log('[audit] issues with margin =', la.issues?.filter((i) => i.margin).map((i) => i.rule).join(','))
console.log('[audit] proxy issues =', la.issues?.filter((i) => i.is_proxy).map((i) => i.rule).join(','))

// ---- P1-5 pipeline leverage_log ----
const pl = runL0Pipeline({
  power_kw: 15, speed_rpm: 1480, voltage_v: 380, count: 12,
  leverage_log: true, write_handoff: false,
}, { ...cfg, handoffDir: '/tmp/l0-probe-home/handoff' })
console.log('[pipeline] leverage_log by_rule =', JSON.stringify(pl.leverage_log?.by_rule))
console.log('[pipeline] leverage_log top1 actionable n =', pl.leverage_log?.top1?.actionable?.length)
console.log('[pipeline] count_ok =', pl.summary?.count_ok)

// ---- 形态守卫：不开 leverage_log 时字段为 null，不影响旧输出 ----
const pl2 = runL0Pipeline({
  power_kw: 15, speed_rpm: 1480, voltage_v: 380, count: 6, write_handoff: false,
}, { ...cfg, handoffDir: '/tmp/l0-probe-home/handoff' })
console.log('[pipeline-old] leverage_log =', pl2.leverage_log)

// ---- 形态守卫：缺参必须显式报错（不静默吞） ----
for (const [name, args] of [
  ['sweep缺values', { sweep_param: 'yoke_thickness', base_params: base }],
  ['sweep缺base', { sweep_param: 'yoke_thickness', sweep_values: [10, 20] }],
  ['solve_turns缺字段', { solve_turns: { voltage: 380 } }],
  ['audit缺对象', { leverage_audit: 'bad' }],
]) {
  try {
    runDesignValidate(args, { ...cfg })
    console.log('[guard]', name, '!! 未报错（缺陷）')
  } catch (e) {
    console.log('[guard]', name, '-> 报错 OK:', String(e.message).slice(0, 40))
  }
}
console.log('ALL_OK')
