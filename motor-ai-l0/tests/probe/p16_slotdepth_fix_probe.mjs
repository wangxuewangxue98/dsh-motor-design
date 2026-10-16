// RUN88 建议 #9 修复定向验证 —— 槽深公式径向空间守恒（v0.5.6）
import { slotGeometryDerived, estimateCopperLoss } from '../../lib/formula-engine.mjs'
import { slotGeometry, validateDesign } from '../../lib/design-rules.mjs'

let pass = 0, failN = 0
function ck(name, cond, detail) {
  if (cond) { pass++; console.log(`✅ ${name} | ${detail}`) }
  else { failN++; console.log(`❌ ${name} | ${detail}`) }
}

// ── T1 新公式：yoke 有效时 slotDepth = half − yoke ──
const g1 = slotGeometryDerived({ statorOd: 260, statorId: 170, coreLength: 195, slotsStator: 36, poles: 4, yokeThickness: 29.767 })
ck('T1 slotGeometryDerived 扣轭厚', g1.slotDepth === 15.2,
  `slotDepth=${g1.slotDepth}（期望 round1(45−29.767)=15.2）`)

// ── T2 兼容回退：yoke 缺省 = 旧口径 0.6·half ──
const g2 = slotGeometryDerived({ statorOd: 260, statorId: 170, coreLength: 195, slotsStator: 36, poles: 4 })
ck('T2 缺省回退 0.6·half', g2.slotDepth === 27, `slotDepth=${g2.slotDepth}（期望 27）`)

// ── T3 yoke 无效值（0/null）也回退 ──
const g3 = slotGeometryDerived({ statorOd: 260, statorId: 170, coreLength: 195, slotsStator: 36, poles: 4, yokeThickness: 0 })
const g3b = slotGeometryDerived({ statorOd: 260, statorId: 170, coreLength: 195, slotsStator: 36, poles: 4, yokeThickness: null })
ck('T3 yoke=0/null 回退', g3.slotDepth === 27 && g3b.slotDepth === 27, `${g3.slotDepth} / ${g3b.slotDepth}`)

// ── T4 design-rules slotGeometry 同口径 ──
const s1 = slotGeometry({ statorOd: 260, statorId: 170, toothWidth: 7.751, slotsStator: 36, yokeThickness: 29.767 })
const s2 = slotGeometry({ statorOd: 260, statorId: 170, toothWidth: 7.751, slotsStator: 36 })
ck('T4 slotGeometry 双口径', s1.slot_depth === 15.2 && s2.slot_depth === 27,
  `带yoke=${s1.slot_depth} 不带=${s2.slot_depth}`)

// ── T5 Y160L-4 实证对照（RUN88 真值槽深 14.88mm）──
ck('T5 真值对照 <5%', Math.abs(g1.slotDepth - 14.88) / 14.88 < 0.05,
  `公式 ${g1.slotDepth} vs 真值 14.88，偏差 ${(Math.abs(g1.slotDepth - 14.88) / 14.88 * 100).toFixed(1)}%`)

// ── T6 estimateCopperLoss 透传：SF 随槽面积变化方向正确 ──
const cuArgs = { statorOd: 260, statorId: 170, coreLength: 195, slotsStator: 36, poles: 4, peakCurrent: 30, turnsPerCoil: 30, parallelCircuits: 1, airGap: 0.6 }
const cuNew = estimateCopperLoss({ ...cuArgs, yokeThickness: 29.767 })
const cuOld = estimateCopperLoss({ ...cuArgs })
ck('T6 槽面积/SF 方向', cuNew.slot_area_mm2 < cuOld.slot_area_mm2 && cuNew.slot_fill_ratio > cuOld.slot_fill_ratio,
  `带yoke 面积=${cuNew.slot_area_mm2} SF=${cuNew.slot_fill_ratio} | 缺省 面积=${cuOld.slot_area_mm2} SF=${cuOld.slot_fill_ratio}`)

// ── T7 端到端 validateDesign：V09 metrics.slot 反映真实槽深 ──
const row = {
  stator_od: 260, stator_id: 170, core_length: 195, slots_stator: 36, poles: 4,
  tooth_width: 7.751, yoke_thickness: 29.767, shaft_dia: 48, rotor_od: 169,
  voltage: 380, peak_current: 30, speed: 1460, turns_per_coil: 30, parallel_circuits: 1,
}
const r1 = validateDesign(row)
ck('T7 V09 metrics.slot.slot_depth', r1.metrics?.slot?.slot_depth === 15.2,
  `slot_depth=${r1.metrics?.slot?.slot_depth}（旧行为为 27）`)
ck('T7 V15 metrics.slot_fill.area', r1.metrics?.slot_fill?.area_mm2 === cuNew.slot_area_mm2,
  `area=${r1.metrics?.slot_fill?.area_mm2} 与直调一致=${cuNew.slot_area_mm2}`)

// ── T8 V09 yoke 过大时槽深归零不崩溃（fail-open）──
const r2 = validateDesign({ ...row, yoke_thickness: 44 })
ck('T8 yoke=44 槽深~0 不崩溃', typeof r2.status === 'string',
  `status=${r2.status} slot_depth=${r2.metrics?.slot?.slot_depth}`)

console.log('\n════════════════════════════════════')
console.log(failN === 0 ? `✅ 全部通过（${pass}/${pass + failN}）` : `❌ FAIL ${failN}/${pass + failN}`)
process.exit(failN === 0 ? 0 : 1)
