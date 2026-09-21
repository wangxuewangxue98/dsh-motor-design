/**
 * 经验公式引擎 —— L0 毫秒级估算的计算核心
 * ===========================================================
 * 设计原则：
 *   1. **零依赖**：只用 Node 内置能力，可被 node 直接 import 做单测
 *   2. **确定性**：全部去除 Python 侧的 random 抖动（physics_kernel.py:587/608/609/629
 *      等处有 random），同一入参必得同一结果 —— L0 要能回归比对，抖动是死敌
 *   3. **双通道产出**：
 *        native  物理损耗通道（L0 原生 6 字段，有独立估算价值）
 *        proxy   L1 同口径去噪克隆（专供 W4 回归质量门比对）
 *
 * 取舍说明：为什么不直接照抄 _run_simulated？
 *   照抄会让 L0 退化成「L1 的复制品」，失去预筛层的独立信息量。
 *   保留两套并显式输出 proxy，既能排序一致，又能暴露两侧偏差 —— 这正是 W4 要的门。
 */

import {
  TORQUE_CONST, AIR_GAP_MIN, AIR_GAP_MAX, SIM_EFF, SIM_TEMP, LOSS_SPLIT,
  STEINMETZ, COOLING_COEFFICIENT, DEFAULT_COOLING, POLE_EFFICIENCY_FACTOR,
  CURRENT_DENSITY_REF, FLUX_TARGET, STACKING_FACTOR, AMBIENT_TEMP_C,
  lambdaRange, clamp, round1, round2, yokeThickness,
} from './motor-constants.mjs'
import { buildL0Result } from './param-schema.mjs'

/** 铜电阻率 Ω·m @ FLUX_TARGET.temperatureC（用户现场口径 150°C） */
const RHO_CU_20 = 1.72e-8
const ALPHA_CU = 0.00393

/** 硅钢密度 kg/m³ */
const RHO_FE = 7650

/** 槽内铜填充率（典型槽满率 0.40~0.50） */
const SLOT_FILL_FACTOR = 0.45

/**
 * 端部单边伸出长度系数：end_ext ≈ 1.8·τ_p
 * 起因：原先的常量 1.35 倍端部系数在「少极数 + 短铁心」机器上严重低估绕组长度。
 *       少极数机器极距大 → 端部占比高，这是物理事实，不能用常数近似。
 */
const END_EXT_COEF = 1.8

/** 机械损耗标定系数：P_mech = K·(n/1000)²·(rotor_od/100)² (W) */
const MECH_LOSS_K = 5.0

export function copperResistivity(tempC = FLUX_TARGET.temperatureC) {
  return RHO_CU_20 * (1 + ALPHA_CU * (tempC - 20))
}

// ═════════════════════════════════════════════════════════════
// 1. 基础换算（严格对齐 Python 侧同名函数）
// ═════════════════════════════════════════════════════════════

/** T = 9550·P/n —— physics_kernel.py:63-67 */
export function computeTorque(powerKw, speedRpm) {
  if (!speedRpm || speedRpm <= 0) return 0
  return round2((TORQUE_CONST * powerKw) / speedRpm)
}

/** P = T·n/9550 —— motor_tools.py:760 */
export function computePowerKw(torqueNm, speedRpm) {
  return round2((torqueNm * speedRpm) / TORQUE_CONST)
}

/** D²L (m³) —— physics_kernel.py:70-74 */
export function computeD2L(dMm, lMm) {
  const d = dMm / 1000
  const l = lMm / 1000
  return Math.round(d * d * l * 1e6) / 1e6
}

/** λ = L/D —— physics_kernel.py:77-81 */
export function computeLambda(lMm, dMm) {
  if (!dMm || dMm <= 0) return 0
  return Math.round((lMm / dMm) * 1000) / 1000
}

/** λ 合理性校验 —— physics_kernel.py:84-98（返回结构保持一致） */
export function validateLambda(lMm, dMm, poles) {
  const lam = computeLambda(lMm, dMm)
  const [lo, hi] = lambdaRange(poles)
  const valid = lam >= lo && lam <= hi
  let advice = ''
  if (!valid) {
    advice = lam < lo
      ? `lambda=${lam} 偏小, 铁心过短或内径过大, 建议增加铁心长度`
      : `lambda=${lam} 偏大, 铁心过长或内径过小, 建议减小铁心长度或增大内径`
  }
  return { valid, lambda: lam, range: [lo, hi], advice }
}

/** 电频率 f = n·p/120 */
export function electricalFrequency(speedRpm, poles) {
  return (speedRpm * poles) / 120
}

// ═════════════════════════════════════════════════════════════
// 2. 损耗分解（L0 物理通道）
// ═════════════════════════════════════════════════════════════

/** 定子铁心质量估算 (kg) */
export function estimateCoreMassKg(statorOd, statorId, coreLength) {
  const areaMm2 = (Math.PI / 4) * (statorOd ** 2 - statorId ** 2)
  const volumeM3 = areaMm2 * 1e-6 * (coreLength / 1000)
  return round2(volumeM3 * RHO_FE * STACKING_FACTOR)
}

/** 铁损 P_fe —— Steinmetz 简化：p = kh·f·B^α + ke·f²·B² (W/kg) */
export function estimateIronLoss({
  speedRpm, poles, coreMassKg, fluxDensity = STEINMETZ.fluxDensity,
}) {
  const f = electricalFrequency(speedRpm, poles)
  const B = fluxDensity
  const perKg =
    STEINMETZ.kh * f * Math.pow(B, STEINMETZ.alpha) +
    STEINMETZ.ke * f * f * B * B
  return Math.round(perKg * coreMassKg)
}

/**
 * 铜损 P_cu —— 电密法：ρ(T)·J²·V_cu
 * 槽内可用面积按平均直径处的齿距减齿宽估算，乘以端部系数。
 */
export function estimateCopperLoss({ statorOd, statorId, coreLength, slotsStator, toothWidthMm, poles }) {
  const half = (statorOd - statorId) / 2
  const slotDepth = round1(half * 0.60)          // 同 physics_kernel.py:601
  if (slotDepth <= 0) return 0
  const meanDia = statorId + slotDepth
  const toothPitch = (Math.PI * meanDia) / slotsStator
  const slotWidth = Math.max(0, toothPitch - toothWidthMm)
  const slotAreaMm2 = slotWidth * slotDepth

  // 平均半匝长度 = 铁心段 + 两端端部
  // 端部按 1.8·τ_p 计（τ_p = 极距 = π·D/poles），随极数变小而显著变长
  const polePitchMm = (Math.PI * statorId) / (poles || 4)
  const meanTurnLength = coreLength + 2 * END_EXT_COEF * polePitchMm

  const volumeM3 =
    slotsStator * slotAreaMm2 * SLOT_FILL_FACTOR * meanTurnLength * 1e-9
  const rho = copperResistivity()
  const J = CURRENT_DENSITY_REF * 1e6            // A/mm² → A/m²
  return Math.round(rho * J * J * volumeM3)
}

/** 机械损耗 —— 风磨+轴承经验标定 */
export function estimateMechanicalLoss({ speedRpm, rotorOdMm }) {
  if (!speedRpm || speedRpm <= 0) return 0
  return Math.round(
    MECH_LOSS_K * Math.pow(speedRpm / 1000, 2) * Math.pow(rotorOdMm / 100, 2)
  )
}

/** 由输出功率与总损耗反推效率 (%) */
export function efficiencyFromLosses(powerOutputKw, totalLossW) {
  const pOut = powerOutputKw * 1000
  return round2((pOut / (pOut + totalLossW)) * 100)
}

/** 损耗按 L1 拆分比分解 —— motor_tools.py:775-777 */
export function splitLosses(totalLossW) {
  return {
    copper_loss: round1(totalLossW * LOSS_SPLIT.copper),
    iron_loss: round1(totalLossW * LOSS_SPLIT.iron),
    mechanical_loss: round1(totalLossW * LOSS_SPLIT.mechanical),
  }
}

// ═════════════════════════════════════════════════════════════
// 3. 温升
// ═════════════════════════════════════════════════════════════

/**
 * L0 原生温升 ΔT (K) —— 热负荷法
 * ΔT = P_loss / (h · A)，A 取机座光滑外表面积 + 端盖半面积
 */
export function estimateTempRise({ statorOd, coreLength, cooling }, totalLossW) {
  const h = COOLING_COEFFICIENT[cooling] ?? COOLING_COEFFICIENT[DEFAULT_COOLING]
  const odM = statorOd / 1000
  const lM = coreLength / 1000
  const lateral = Math.PI * odM * lM
  const endCap = Math.PI * (odM / 2) ** 2
  const area = lateral + endCap
  if (area <= 0) return 0
  return round1(totalLossW / (h * area))
}

// ═════════════════════════════════════════════════════════════
// 4. L1 同口径克隆（供 W4 回归比对）
// ═════════════════════════════════════════════════════════════

/**
 * L1 仿真效率去噪克隆 —— motor_tools.py:750-752
 *   base_eff = 85 + (od-180)·0.04 + (poles-8)·1.2，Python 侧另有 ±1.5 噪声
 */
export function l1EfficiencyProxy({ statorOd, poles }, clampMax = SIM_EFF.clampMax) {
  const base =
    SIM_EFF.base +
    (statorOd - SIM_EFF.odRef) * SIM_EFF.odCoef +
    (poles - SIM_EFF.poleRef) * SIM_EFF.poleCoef
  return round2(clamp(base, SIM_EFF.clampMin, clampMax))
}

/**
 * L1 仿真最高温去噪克隆 —— motor_tools.py:755-756
 *   base_temp = 55 + (od-180)·0.25 + (speed-3000)·0.004，Python 侧另有 [-5,+8] 噪声
 * 注意单位：这里是 **°C（绝对温度）**，与 L0 原生 temp_rise 的 K（温升）不是同一口径。
 */
export function l1MaxTempProxy({ statorOd, speed }, clampRange = [SIM_TEMP.clampMin, SIM_TEMP.clampMax]) {
  const base =
    SIM_TEMP.base +
    (statorOd - SIM_TEMP.odRef) * SIM_TEMP.odCoef +
    (speed - SIM_TEMP.speedRef) * SIM_TEMP.speedCoef
  return round1(clamp(base, clampRange[0], clampRange[1]))
}

// ═════════════════════════════════════════════════════════════
// 5. L0 主估算
// ═════════════════════════════════════════════════════════════

/**
 * 单次 L0 估算 —— 返回「L0 原生 + L1 镜像」双字段结果（v3 决策②）
 *
 * @param {object} params 单行参数，须为 L1_MATRIX_FIELDS 形状（另加 power_kw/cooling）
 * @param {object} [opts]
 * @param {number} [opts.efficiencyCap=96]  效率封顶，默认对齐 _run_simulated L752
 * @param {[number,number]} [opts.maxTempClamp=[45,130]] 镜像 max_temp 钳位（对齐 L756）
 * @returns {object} 双字段结果行
 */
export function quickL0Estimate(params, opts = {}) {
  const efficiencyCap = opts.efficiencyCap ?? SIM_EFF.clampMax
  const maxTempClamp = opts.maxTempClamp ?? [SIM_TEMP.clampMin, SIM_TEMP.clampMax]
  const cooling = params.cooling ?? DEFAULT_COOLING

  const powerKw = params.power_kw ?? computePowerKw(params.torque_nm ?? 0, params.speed)
  const poles = params.poles
  const statorOd = params.stator_od
  const statorId = params.stator_id ?? round1(statorOd * 0.6)
  const coreLength = params.core_length ?? 80
  const slotsStator = params.slots_stator ?? 36
  const toothWidthMm = params.tooth_width ?? round2((Math.PI * statorId) / (2 * slotsStator))
  const rotorOdMm = params.rotor_od ?? round1(statorId - 2 * (params.air_gap ?? 0.6))

  // ---- 损耗三项 ----
  const coreMassKg = estimateCoreMassKg(statorOd, statorId, coreLength)
  const pIron = estimateIronLoss({
    speedRpm: params.speed, poles, coreMassKg,
    fluxDensity: opts.fluxDensity ?? STEINMETZ.fluxDensity,
  })
  const pCopper = estimateCopperLoss({
    statorOd, statorId, coreLength, slotsStator, toothWidthMm, poles,
  })
  const pMech = estimateMechanicalLoss({ speedRpm: params.speed, rotorOdMm })
  const totalLossW = pIron + pCopper + pMech

  // ---- 效率（物理通道，再按极数与封顶修正）----
  const poleFactor = POLE_EFFICIENCY_FACTOR[poles] ?? 0.98
  const rawEff = efficiencyFromLosses(powerKw, totalLossW) * poleFactor
  const efficiency = round2(Math.min(efficiencyCap, Math.max(SIM_EFF.clampMin, rawEff)))

  // ---- 温升（物理通道）----
  const tempRise = estimateTempRise({ statorOd, coreLength, cooling }, totalLossW)

  // ---- 转矩密度 ----
  const torque = params.torque_nm ?? computeTorque(powerKw, params.speed)
  const volumeM3 = (statorOd / 1000) ** 2 * (coreLength / 1000)
  const torqueDensity = volumeM3 > 0 ? Math.round(torque / volumeM3) : 0

  const native = {
    efficiency,
    torque: round2(torque),
    temp_rise: tempRise,
    total_loss: totalLossW,
    torque_density: torqueDensity,
    prediction_source: 'formula',
  }

  // ---- L1 镜像字段（可消费）：全部取自 L0 物理通道，保证同一行结果内部自洽 ----
  // 不照抄 SIM_EFF/SIM_TEMP 的理由见 param-schema 中 L1_MIRROR_FIELDS 的注释。
  const proxyEff = Math.min(efficiencyCap, l1EfficiencyProxy({ statorOd, poles }))
  const mirrorPowerKw = computePowerKw(native.torque, params.speed)

  const mirror = {
    max_temp: round1(clamp(AMBIENT_TEMP_C + tempRise, maxTempClamp[0], maxTempClamp[1])),
    power: mirrorPowerKw,
    ...splitLosses(totalLossW),
    solve_mode: 'l0',
    // ---- 诊断列（不可被下游消费，仅供 W4 回归判定偏差）----
    l1_efficiency_proxy: proxyEff,
    l1_temp_proxy: l1MaxTempProxy({ statorOd, speed: params.speed }, maxTempClamp),
    // l1_handoff 由上层汇总 TopN 时统一挂载，单行结果不重复携带
  }

  return buildL0Result({ params, native, mirror, confidence: 1.0 })
}

export default {
  computeTorque, computePowerKw, computeD2L, computeLambda, validateLambda,
  electricalFrequency, estimateCoreMassKg, estimateIronLoss, estimateCopperLoss,
  estimateMechanicalLoss, efficiencyFromLosses, splitLosses, estimateTempRise,
  l1EfficiencyProxy, l1MaxTempProxy, quickL0Estimate, copperResistivity,
}
