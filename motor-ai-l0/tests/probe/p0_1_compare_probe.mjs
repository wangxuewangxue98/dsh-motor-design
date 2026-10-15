// P0-1 compare 分支自检（l0 0.5.4 实施探针）
import { runL0Estimate } from '../../tools/l0/l0-estimate.mjs'

const baseline = {
  stator_od: 210, stator_id: 136, rotor_od: 134.4, core_length: 160, air_gap: 0.8,
  poles: 4, slots_stator: 36, tooth_width: 6.8, yoke_thickness: 20,
  turns_per_coil: 22, parallel_circuits: 2, peak_current: 25, voltage: 380,
  speed: 1480, power_kw: 15, motor_type: 'induction',
}
// 改后：轭加厚（V08 杠杆）+ 匝数微调（V15 杠杆）
const current = {
  ...baseline,
  yoke_thickness: 26,
  turns_per_coil: 20,
}

const config = { efficiencyCap: 96, tempRiseRange: [45, 130], handoffDir: undefined }

// 用探针专用的独立 config 字面量绕过同链闸（P0-门1' 形态 A：新字面量）
const cfgA = { ...config }
const r = runL0Estimate({ params: current, baseline_params: baseline }, cfgA)

console.log('mode =', r.mode)
console.log('changed_fields =', JSON.stringify(r.comparison?.changed_fields))
console.log('summary =', r.comparison?.summary)
console.log('delta =', JSON.stringify(r.comparison?.delta, null, 1))
console.log('rule_changes =', JSON.stringify(r.comparison?.rule_changes))
console.log('temp_rise_is_proxy =', r.temp_rise_is_proxy)
console.log('baseline.efficiency_raw =', r.comparison?.baseline?.efficiency_raw)
console.log('current.efficiency_raw =', r.comparison?.current?.efficiency_raw)
console.log('baseline.rule_status =', r.comparison?.baseline?.rule_status)
console.log('current.rule_status =', r.comparison?.current?.rule_status)

// 形态守卫：不传 baseline_params 时必须走旧路径（逐字节兼容）
const old = runL0Estimate({ params_list: [current] }, { ...config })
console.log('old-mode keys has handoff =', 'handoff' in old, 'total =', old.total)
console.log('old-mode temp_rise_is_proxy =', old.temp_rise_is_proxy)
console.log('ALL_OK')
