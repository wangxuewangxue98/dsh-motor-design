// v0.5.5 P2 功能探针：verdict 密度无关化 + 槽形族 + 杠杆知识库
import { runDesignValidate } from '../../tools/l0/design-validate.mjs'
import { runL0Pipeline } from '../../tools/l0/pipeline.mjs'

const base = {
  stator_od: 210, stator_id: 136, rotor_od: 134.4, core_length: 160, air_gap: 0.8,
  poles: 4, slots_stator: 36, tooth_width: 6.8, yoke_thickness: 14, turns_per_coil: 11,
  parallel_circuits: 2, peak_current: 25, voltage: 380, speed: 1480, power_kw: 15,
  motor_type: 'induction',
}
const cfg = { freeQuotaEnabled: false }

// 1. leverage_audit 带 slot_family + kb_hint + rule_to_levers
const la = runDesignValidate({ leverage_audit: { ...base, yoke_thickness: 3, slot_family: 'pear' } }, cfg)
console.log('[1] la.status=', la.status, 'slot_family=', la.slot_family, 'label=', la.slot_family_label)
console.log('[1] actionable[0].kb_hint.lever=', la.actionable[0]?.kb_hint?.lever)
console.log('[1] rule_to_levers=', JSON.stringify(la.rule_to_levers))
console.log('[1] slot_family_kb.lever=', la.slot_family_kb?.lever)

// 2. leverage_kb 查询
const kbAll = runDesignValidate({ leverage_kb: true }, cfg)
console.log('[2] kb.all.count=', kbAll.count)
const kbOne = runDesignValidate({ leverage_kb: 'yoke_thickness' }, cfg)
console.log('[2] kb.one.found=', kbOne.found, 'label=', kbOne.entry?.label, 'risk=', JSON.stringify(kbOne.entry?.risk_rules))
const kbUnk = runDesignValidate({ leverage_kb: 'nope' }, cfg)
console.log('[2] kb.unknown.found=', kbUnk.found)

// 3. pipeline verdict_density_independent 声明（生成参数，pipeline 自己派生矩阵）
const pl = runL0Pipeline({ power_kw: 15, voltage: 380, speed: 1480, poles: 4, count: 6, top_n: 1 }, cfg)
console.log('[3] pipeline.verdict_density_independent=', pl.verdict_density_independent)

// 4. 反向验证：density 自检开/关，summary.failed 不变（density 只 warning，不翻转 status）
const plNoDense = runL0Pipeline({ power_kw: 15, voltage: 380, speed: 1480, poles: 4, count: 6, top_n: 1, density_check: false }, cfg)
const plDense = runL0Pipeline({ power_kw: 15, voltage: 380, speed: 1480, poles: 4, count: 6, top_n: 1, density_check: true }, cfg)
const failedSame = plNoDense.summary.failed === plDense.summary.failed
const densityInWarnings = plDense.warnings?.some((w) => w.code === 'DENSITY_NOT_CONVERGED')
console.log('[4] failed(no-density)=', plNoDense.summary.failed,
  'failed(with-density)=', plDense.summary.failed,
  'failed_unchanged=', failedSame, 'density_warning_only=', densityInWarnings)
const densityFlips = !failedSame
console.log('[4] density_flips_status=', densityFlips, '(必须为 false)')

console.log(densityFlips ? 'P2_PROBE_PARTIAL' : 'P2_PROBE_ALL_OK')
