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
  B_YOKE_SAT_T,
  J_DESIGN, WINDING_FACTOR, POWER_FACTOR_REF, ETA_REF_FOR_CURRENT,
  DEFAULT_CONNECTION, BACKEMF_TOL, SLOT_FILL_WARN, SLOT_FILL_MAX,
  AIR_GAP_FLUX_DEFAULT, INSULATION_LIMITS, DEFAULT_INSULATION_CLASS,
  lambdaRange, clamp, round1, round2, round3, round4, round5, yokeThickness,
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
 * 槽几何派生（与设计规则一致口径）
 * 返回槽面积 mm² 与平均半匝长度 m（供铜损与槽满率共用）。
 */
export function slotGeometryDerived({ statorOd, statorId, coreLength, slotsStator, poles }) {
  const half = (statorOd - statorId) / 2
  const slotDepth = round1(half * 0.60)
  const meanDia = statorId + slotDepth
  const toothPitch = (Math.PI * meanDia) / slotsStator
  const toothWidth = (Math.PI * statorId) / (2 * slotsStator)
  const slotWidth = Math.max(0, toothPitch - toothWidth)
  const slotAreaMm2 = slotWidth * slotDepth
  const polePitchMm = (Math.PI * statorId) / (poles || 4)
  const meanTurnLength = (coreLength + 2 * END_EXT_COEF * polePitchMm) / 1000  // m
  return { slotDepth, slotWidth, slotAreaMm2, meanTurnLength }
}

/**
 * 铜损 P_cu —— 真实电流闭环（v0.1.6 修复 P0「电流装饰字段」）
 * ---------------------------------------------------------------
 * 旧版用固定电密常量 CURRENT_DENSITY_REF 近似，矩阵里的 peak_current 被完全忽略，
 * 导致「50A 峰值」与「200kW/380V 实需 ~496A」差 10 倍却无人发现。
 * 新版：给定 peak_current（矩阵已闭环反推），按设计电密 J_DESIGN 定线规，
 * 反算相电阻与铜损，并一并返回槽满率（几何可行性判据）。
 *
 * @returns {{
 *   copper_loss:number, slot_fill_ratio:number, slot_area_mm2:number,
 *   conductor_area_mm2:number, current_density:number,
 *   phase_resistance_ohm:number, line_current_rms:number, feasible:boolean
 * }}
 */
export function estimateCopperLoss({
  statorOd, statorId, coreLength, slotsStator, poles,
  peakCurrent, turnsPerCoil, parallelCircuits = 1, airGap = 0.6,
  currentDensityRef = J_DESIGN, windingFactor = WINDING_FACTOR,
}) {
  const a = Math.max(1, parallelCircuits)
  const geo = slotGeometryDerived({ statorOd, statorId, coreLength, slotsStator, poles })
  if (geo.slotAreaMm2 <= 0) {
    return {
      copper_loss: 0, slot_fill_ratio: 0, slot_area_mm2: 0, conductor_area_mm2: 0,
      current_density: 0, phase_resistance_ohm: 0, line_current_rms: 0, feasible: false,
    }
  }
  const iRms = (peakCurrent ?? 0) / Math.SQRT2            // 峰值→有效值
  // 每支路导体电流 = I_rms / a；按设计电密定单根导线截面积
  const aWire = (iRms / a) / currentDensityRef            // mm²
  // 每相串联匝数 N = (Qs/3)·turns_per_coil / a
  const nPhase = ((slotsStator / 3) * (turnsPerCoil ?? 0)) / a
  // 槽满率（推导）：每槽导体铜面积 / 槽面积
  //   = 2·turns_per_coil·aWire / slotArea
  //   等价形式 turns_per_coil·I_rms / (a²·J·slotArea)
  const conductorArea = 2 * (turnsPerCoil ?? 0) * aWire   // 每槽铜面积 mm²
  const slotFill = geo.slotAreaMm2 > 0 ? conductorArea / geo.slotAreaMm2 : 0
  // 相电阻 R = ρ·N·l_mean / aWire
  const rho = copperResistivity()
  const rPhase = nPhase > 0 && aWire > 0
    ? (rho * nPhase * geo.meanTurnLength) / (aWire * 1e-6)   // aWire mm²→m²
    : 0
  const pCu = 3 * iRms * iRms * rPhase
  return {
    copper_loss: Math.round(pCu),
    slot_fill_ratio: round3(Math.max(0, slotFill)),
    slot_area_mm2: geo.slotAreaMm2,
    conductor_area_mm2: round1(conductorArea),
    current_density: round2(currentDensityRef),
    phase_resistance_ohm: round4(rPhase),
    line_current_rms: round1(iRms),
    feasible: slotFill <= SLOT_FILL_MAX,
  }
}

/**
 * 电气闭环反推（v0.1.6 核心修复）
 * ---------------------------------------------------------------
 * 给定电压/转速/极数/几何，反推自洽的：
 *   - 电频率 f = n·p/120
 *   - 每相串联匝数 N（使反电势 E ≈ 相电压 Uph）
 *   - 每线圈匝数 turns_per_coil = round(N·a·3/Qs)
 *   - 峰值电流 I_peak（由功率 P = √3·U·I·pf·η 反推）
 *   - 反电势 E 与 |E−Uph| 偏差（门禁用）
 *   - 设计电密 J、功率因数 pf
 *
 * 反电势：E ≈ 4.44·f·N·Φ·kdp，Φ = 2·Bg·Dsi·L/p（poleFlux 口径，含 2/π 平均因子）
 * 相电压：星接 Uph = U/√3（DEFAULT_CONNECTION=star）
 *
 * @returns {object} 见下；任何几何缺项返回 null（供调用方 skipped）
 */
export function deriveElectricalClosure({
  voltage, speed, poles, statorId, coreLength, airGap = 0.6,
  slotsStator, parallelCircuits = 1, airGapFlux = AIR_GAP_FLUX_DEFAULT,
  connection = DEFAULT_CONNECTION, windingFactor = WINDING_FACTOR,
  powerKw = null, pf = POWER_FACTOR_REF, eta = ETA_REF_FOR_CURRENT,
}) {
  if (![voltage, speed, poles, statorId, coreLength, slotsStator].every((v) => Number.isFinite(v) && v > 0)) {
    return null
  }
  const a = Math.max(1, parallelCircuits)
  const f = (speed * poles) / 120
  const uph = connection === 'delta' ? voltage : voltage / Math.SQRT2 / Math.sqrt(1.5) // 星接 Uph=U/√3
  const phi = (2 * airGapFlux * (statorId / 1000) * (coreLength / 1000)) / poles  // Wb/极
  if (f <= 0 || phi <= 0) return null
  const nPhase = uph / (4.44 * f * phi * windingFactor)
  const turnsPerCoil = Math.max(1, Math.round((nPhase * a * 3) / slotsStator))
  const backEmf = 4.44 * f * nPhase * phi * windingFactor
  const backEmfOk = Math.abs(backEmf - uph) / uph <= BACKEMF_TOL

  // 电流由功率反推（缺功率则用 design 电密估算，仅作回退）
  let peakCurrent = null
  if (Number.isFinite(powerKw) && powerKw > 0) {
    const iRms = powerKw * 1000 / (Math.sqrt(3) * voltage * pf * eta)
    peakCurrent = round1(iRms * Math.SQRT2)
  }

  return {
    freq_hz: round1(f),
    connection,
    phase_voltage: round1(uph),
    flux_per_pole_wb: round5(phi),
    turns_per_phase: round2(nPhase),
    turns_per_coil: turnsPerCoil,
    parallel_circuits: a,
    back_emf_v: round1(backEmf),
    back_emf_ok: backEmfOk,
    back_emf_dev: round3(Math.abs(backEmf - uph) / uph),
    peak_current: peakCurrent,
    line_current_rms: peakCurrent !== null ? round1(peakCurrent / Math.SQRT2) : null,
    power_factor: pf,
    current_density: J_DESIGN,
  }
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
  const insulationClass = opts.insulationClass ?? DEFAULT_INSULATION_CLASS

  const powerKw = params.power_kw ?? computePowerKw(params.torque_nm ?? 0, params.speed)
  const poles = params.poles
  const statorOd = params.stator_od
  const statorId = params.stator_id ?? round1(statorOd * 0.6)
  const coreLength = params.core_length ?? 80
  const slotsStator = params.slots_stator ?? 36
  const toothWidthMm = params.tooth_width ?? round2((Math.PI * statorId) / (2 * slotsStator))
  const rotorOdMm = params.rotor_od ?? round1(statorId - 2 * (params.air_gap ?? 0.6))
  const airGap = params.air_gap ?? 0.6
  const parallelCircuits = params.parallel_circuits ?? 1
  const turnsPerCoil = params.turns_per_coil ?? 6
  const peakCurrent = params.peak_current

  // ---- 电气闭环（v0.1.6）----
  const closure = deriveElectricalClosure({
    voltage: params.voltage ?? 380,
    speed: params.speed, poles, statorId, coreLength, airGap,
    slotsStator, parallelCircuits,
    airGapFlux: opts.airGapFlux ?? AIR_GAP_FLUX_DEFAULT,
    powerKw,
  })
  const effTurns = closure?.turns_per_coil ?? turnsPerCoil
  const effCurrent = closure?.peak_current ?? peakCurrent

  // ---- 损耗三项 ----
  const coreMassKg = estimateCoreMassKg(statorOd, statorId, coreLength)
  const pIron = estimateIronLoss({
    speedRpm: params.speed, poles, coreMassKg,
    fluxDensity: opts.fluxDensity ?? STEINMETZ.fluxDensity,
  })
  const cu = estimateCopperLoss({
    statorOd, statorId, coreLength, slotsStator, poles,
    peakCurrent: effCurrent, turnsPerCoil: effTurns, parallelCircuits, airGap,
  })
  const pCopper = cu.copper_loss
  const pMech = estimateMechanicalLoss({ speedRpm: params.speed, rotorOdMm })
  const totalLossW = pIron + pCopper + pMech

  // ---- 效率（物理通道，再按极数与封顶修正）----
  const poleFactor = POLE_EFFICIENCY_FACTOR[poles] ?? 0.98
  const rawEff = efficiencyFromLosses(powerKw, totalLossW) * poleFactor
  const efficiency = round2(Math.min(efficiencyCap, Math.max(SIM_EFF.clampMin, rawEff)))

  // ---- 温升（物理通道，含液冷标定修正）----
  const tempRise = estimateTempRise({ statorOd, coreLength, cooling }, totalLossW)
  const tempLimit = INSULATION_LIMITS[insulationClass] ?? INSULATION_LIMITS[DEFAULT_INSULATION_CLASS]
  const thermalOk = tempRise <= tempLimit

  // ---- 磁密（齿/轭）可追溯输出 ----
  const airGapFlux = opts.airGapFlux ?? AIR_GAP_FLUX_DEFAULT
  const toothB = round2((2 * airGapFlux * statorId) / (slotsStator * toothWidthMm * STACKING_FACTOR))
  const yokeB = round2((airGapFlux * statorId) / (poles * (params.yoke_thickness ?? yokeThickness(statorOd, statorId)) * STACKING_FACTOR))
  // v0.1.7：轭部饱和判据（Bj 超硅钢饱和极限 ~1.9T ⇒ 轭部无法承载磁通，设计不可行）
  const yokeSat = yokeB > B_YOKE_SAT_T

  // ---- 可行性判定（P0 门禁聚合）----
  const backEmfOk = closure?.back_emf_ok ?? true
  const slotFillOk = cu.slot_fill_ratio <= SLOT_FILL_MAX
  const feasible = backEmfOk && slotFillOk && thermalOk && !yokeSat

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
    // ---- v0.1.6 可追溯诊断字段（让报告「每值给公式」）----
    electrical_frequency_hz: closure?.freq_hz ?? round1((params.speed * poles) / 120),
    air_gap_flux_density: airGapFlux,
    tooth_flux_density: toothB,
    yoke_flux_density: yokeB,
    yoke_sat: yokeSat,
    current_density: cu.current_density,
    power_factor: closure?.power_factor ?? POWER_FACTOR_REF,
    slot_fill_ratio: cu.slot_fill_ratio,
    back_emf_v: closure?.back_emf_v ?? null,
    back_emf_ok: backEmfOk,
    thermal_rise_k: tempRise,
    thermal_limit_k: tempLimit,
    insulation_class: insulationClass,
    peak_current: effCurrent,
    turns_per_coil: effTurns,
    parallel_circuits: parallelCircuits,
    feasible,
    verdict: feasible ? 'feasible' : (thermalOk ? 'infeasible_geometry' : 'infeasible_thermal'),
  }

  // ---- L1 镜像字段（可消费）：全部取自 L0 物理通道，保证同一行结果内部自洽 ----
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
  }

  return buildL0Result({ params, native, mirror, confidence: 1.0 })
}

export default {
  computeTorque, computePowerKw, computeD2L, computeLambda, validateLambda,
  electricalFrequency, estimateCoreMassKg, estimateIronLoss, estimateCopperLoss,
  estimateMechanicalLoss, efficiencyFromLosses, splitLosses, estimateTempRise,
  l1EfficiencyProxy, l1MaxTempProxy, quickL0Estimate, copperResistivity,
  slotGeometryDerived, deriveElectricalClosure,
}
