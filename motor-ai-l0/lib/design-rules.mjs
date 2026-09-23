/**
 * 物理一致性校验规则引擎 —— W4 核心（motor_design_validate 的纯逻辑层）
 * ===========================================================
 * 存在理由：
 *   L0 是「广筛层」，参数矩阵由经验式批量派生，必然混入物理上不成立的组合
 *   （如槽宽为负、转子轭过薄、电频率越界）。若不先剔除就直接进估算与排序，
 *   TopN 会被这些"看起来效率高"的假方案污染 —— 预筛层的价值就是在这里。
 *
 * 设计原则：
 *   1. 纯函数、零依赖：可被 node 直接 import 做单测，与 DSH Runtime 无关
 *   2. 三级严重度：failed（必须剔除）/ warning（可保留但需知情）/ passed
 *   3. 每条规则都可追溯判据来源（常量名或 Python 行号），不允许裸魔数
 *   4. 可跳过：缺少必要输入时显式记入 skipped，绝不静默通过
 *
 * 字段真源：本模块只读 lib/param-schema.mjs 定义的 L1 形状字段，
 *           不引入任何新的参数命名。
 */

import {
  SLOT_MAP, PMSM_SLOT_MAP, idRatio, idRatioByPoles, isPmsmType, empiricalAirGap, AIR_GAP_MIN, AIR_GAP_MAX, airGapMax, HIGH_SPEED_RPM,
  FLUX_TARGET, STACKING_FACTOR, AIR_GAP_FLUX_DEFAULT,
  INSULATION_LIMITS, DEFAULT_INSULATION_CLASS, MIN_ROTOR_YOKE_MM,
  FREQ_LIMITS, FLUX_TOLERANCE, SLOT_ASPECT_RANGE, ID_RATIO_TOLERANCE,
  B_YOKE_WARN_T, B_YOKE_SAT_T,
  SLOT_FILL_MAX, BACKEMF_TOL, WINDING_FACTOR,
  clamp, round1, round2, round3,
} from './motor-constants.mjs'
import { validateLambda, quickL0Estimate, estimateCopperLoss } from './formula-engine.mjs'

/** 规则目录 —— 供文档、UI、测试三方共用，避免"规则存在但没人知道" */
export const RULE_CATALOG = [
  { id: 'V01', name: '几何链自洽', severity: 'failed', needs: 'stator_od/stator_id/rotor_od/shaft_dia' },
  { id: 'V02', name: '气隙与转子外径一致', severity: 'failed', needs: 'stator_id/rotor_od/air_gap' },
  { id: 'V03', name: '定子内外径比', severity: 'warning', needs: 'stator_od/stator_id/poles' },
  { id: 'V04', name: '长径比 λ', severity: 'warning', needs: 'core_length/stator_id/poles' },
  { id: 'V05', name: '极槽配合', severity: 'warning', needs: 'poles/slots_stator/slots_rotor' },
  { id: 'V06', name: '并联支路整除极数', severity: 'failed', needs: 'poles/parallel_circuits' },
  { id: 'V07', name: '齿部磁密', severity: 'warning', needs: 'stator_id/tooth_width/slots_stator/poles' },
  { id: 'V08', name: '轭部磁密', severity: 'failed', needs: 'stator_id/yoke_thickness/poles' },
  { id: 'V09', name: '槽形几何', severity: 'failed', needs: 'stator_od/stator_id/tooth_width/slots_stator' },
  { id: 'V10', name: '电频率', severity: 'warning', needs: 'speed/poles' },
  { id: 'V11', name: '转子轭厚度', severity: 'failed', needs: 'rotor_od/shaft_dia' },
  { id: 'V12', name: '温升限值', severity: 'warning', needs: 'power_kw 或 torque_nm（+ 冷却方式）' },
  { id: 'V14', name: '反电势闭环', severity: 'failed', needs: 'voltage/speed/poles/stator_id/core_length/slots_stator/parallel_circuits/turns_per_coil' },
  { id: 'V15', name: '槽满率可行性', severity: 'failed', needs: 'stator_od/stator_id/slots_stator/poles/peak_current/turns_per_coil/parallel_circuits' },
]

/** 可在 config 中把严重度提升为 failed 的规则（默认 warning） */
export const ESCALATABLE_RULES = ['V03', 'V04', 'V05', 'V07', 'V08', 'V10', 'V12']

/**
 * 每极磁通 Φ (Wb)
 * ---------------------------------------------------------------
 * ⚠ 关键因子：气隙磁密沿圆周按正弦分布，一个极距内的 **平均值** 才是磁通，
 *   B_avg = (2/π)·B_peak。极面积 = (π·Dsi/p)·L，故
 *       Φ = (2/π)·B_peak · (π·Dsi/p) · L = 2·B_peak·Dsi·L / p
 *   初版漏掉了 2/π（直接用 B_peak × 极面积），导致磁密被高估 π/2≈1.57 倍，
 *   表现为「齿磁密 1.63T vs 现场目标 1.02T」的假性冲突。修正后自洽。
 */
export function poleFlux({ statorId, coreLength, poles, airGapFlux = AIR_GAP_FLUX_DEFAULT }) {
  if (!(statorId > 0) || !(coreLength > 0) || !(poles > 0)) return null
  return (2 * airGapFlux * (statorId / 1000) * (coreLength / 1000)) / poles
}

/**
 * 齿部磁密反算
 * 推导：每极下齿数 = Qs/p，单齿截面 = bt · L · k_stack
 *       Bt = Φ / [(Qs/p) · bt · L · k_stack] = 2·B_gap·Dsi / (Qs·bt·k_stack)
 *
 * 自洽性检验（W4 修正后）：本仓库齿宽沿用 physics_kernel.py:598
 *   bt = π·Dsi/(2·Qs)（齿宽 = 半个齿距），代入得
 *       Bt = 4·B_gap / (π·k_stack) ≈ 1.30 · B_gap
 *   取 B_gap=0.80T ⇒ Bt ≈ 1.04T —— 与用户现场口径「齿磁密 1.02T」吻合到 2%，
 *   说明齿宽定义与磁密目标本就是同一套假设，初版的 1.63T 纯属 2/π 遗漏所致。
 */
export function toothFluxDensity({ statorId, toothWidth, slotsStator, airGapFlux = AIR_GAP_FLUX_DEFAULT }) {
  if (!(statorId > 0) || !(toothWidth > 0) || !(slotsStator > 0)) return null
  return round2(
    (2 * airGapFlux * statorId) / (slotsStator * toothWidth * STACKING_FACTOR)
  )
}

/**
 * 轭部磁密反算
 * 推导：轭中每侧磁通 = Φ/2，轭截面 = yoke · L · k_stack
 *       By = (Φ/2) / (yoke·L·k_stack) = B_gap·Dsi / (p·yoke·k_stack)
 */
export function yokeFluxDensity({ statorId, yokeThickness, poles, airGapFlux = AIR_GAP_FLUX_DEFAULT }) {
  if (!(statorId > 0) || !(yokeThickness > 0) || !(poles > 0)) return null
  return round2(
    (airGapFlux * statorId) / (poles * yokeThickness * STACKING_FACTOR)
  )
}

/** 槽形派生：槽深按 physics_kernel.py:601 的 0.60 系数，槽宽 = 齿距 - 齿宽 */
export function slotGeometry({ statorOd, statorId, toothWidth, slotsStator }) {
  const half = (statorOd - statorId) / 2
  const slotDepth = round1(half * 0.60)
  const toothPitch = (Math.PI * statorId) / slotsStator
  const slotWidth = round2(toothPitch - toothWidth)
  return {
    slot_depth: slotDepth,
    tooth_pitch: round2(toothPitch),
    slot_width: slotWidth,
    aspect: slotDepth > 0 ? round2(slotWidth / slotDepth) : null,
  }
}

/**
 * 单个方案的物理一致性校验
 * @param {object} row 参数行（L1 形状；power_kw/torque_nm/cooling 为可选）
 * @param {object} [opts]
 * @param {number} [opts.airGapFlux]        气隙磁密基准 T，默认 0.80
 * @param {string} [opts.insulationClass]   绝缘等级 B/F/H，默认 F
 * @param {string[]} [opts.escalate]        需要提升为 failed 的规则 id
 * @param {boolean} [opts.includeThermal]   是否执行温升校验（需功率信息），默认 true
 * @returns {{status:'passed'|'warning'|'failed', issues: object[], checked: number,
 *            skipped: string[], metrics: object}}
 */
export function validateDesign(row, opts = {}) {
  const issues = []
  const skipped = []
  const metrics = {}
  const escalate = new Set(opts.escalate ?? [])
  let status = 'passed'

  const fail = (rule, msg) => {
    issues.push({ rule, level: 'failed', msg })
    status = 'failed'
  }
  const warn = (rule, msg) => {
    if (escalate.has(rule)) { fail(rule, msg); return }
    issues.push({ rule, level: 'warning', msg })
    if (status !== 'failed') status = 'warning'
  }
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)

  const statorOd = num(row?.stator_od)
  const statorId = num(row?.stator_id)
  const rotorOd = num(row?.rotor_od)
  const shaftDia = num(row?.shaft_dia)
  const coreLength = num(row?.core_length)
  const airGap = num(row?.air_gap)
  const toothWidth = num(row?.tooth_width)
  const yokeThickness = num(row?.yoke_thickness)
  const poles = num(row?.poles)
  const speed = num(row?.speed)
  const slotsStator = num(row?.slots_stator)
  const slotsRotor = num(row?.slots_rotor)
  const parallel = num(row?.parallel_circuits)
  const turns = num(row?.turns_per_coil)
  const peakCurrent = num(row?.peak_current)
  const airGapFlux = num(opts.airGapFlux) ?? AIR_GAP_FLUX_DEFAULT

  // ---- V01 几何链自洽 ----
  if ([statorOd, statorId, rotorOd, shaftDia].every((v) => v !== null)) {
    metrics.geometry_chain = { stator_od: statorOd, stator_id: statorId, rotor_od: rotorOd, shaft_dia: shaftDia }
    if (!(statorOd > statorId)) fail('V01', `定子外径 ${statorOd} ≤ 内径 ${statorId}，几何不成立`)
    else if (!(statorId > rotorOd)) fail('V01', `定子内径 ${statorId} ≤ 转子外径 ${rotorOd}，无气隙空间`)
    else if (!(rotorOd > shaftDia)) fail('V01', `转子外径 ${rotorOd} ≤ 轴径 ${shaftDia}`)
    else if (!(shaftDia > 0)) fail('V01', `轴径 ${shaftDia} 非正`)
  } else {
    skipped.push('V01')
  }

  // ---- V02 气隙边界 + 转子外径一致性 ----
  if (airGap !== null && statorId !== null && rotorOd !== null) {
    const gapMax = airGapMax(speed)
    const highSpeed = typeof speed === 'number' && speed >= HIGH_SPEED_RPM
    metrics.air_gap = airGap
    metrics.air_gap_max = gapMax
    if (airGap < AIR_GAP_MIN || airGap > gapMax) {
      fail('V02', `气隙 ${airGap}mm 越出 [${AIR_GAP_MIN}, ${gapMax}]${highSpeed ? '（高速档）' : ''}`)
    }
    const expectedRotor = round1(statorId - 2 * airGap)
    const delta = Math.abs(expectedRotor - rotorOd)
    metrics.rotor_od_expected = expectedRotor
    if (delta > 0.5) {
      fail('V02', `转子外径 ${rotorOd} 与 内径-2×气隙 (${expectedRotor}) 相差 ${round1(delta)}mm`)
    }
    if (statorOd !== null && !highSpeed) {
      // 经验气隙式是按常规转速标定的；高速工况加大气隙是主动设计选择，
      // 拿常规经验值去比会全部误报，故高速档跳过该项。
      const empirical = empiricalAirGap(statorOd)
      metrics.air_gap_empirical = empirical
      if (airGap < empirical * 0.7) {
        warn('V02', `气隙 ${airGap}mm 小于经验值 ${empirical}mm 的 70%，制造困难且齿槽转矩大`)
      } else if (airGap > empirical * 1.5) {
        warn('V02', `气隙 ${airGap}mm 大于经验值 ${empirical}mm 的 1.5 倍，磁负荷不足`)
      }
    }
  } else {
    skipped.push('V02')
  }

  // ---- V03 定子内外径比 ----
  if (statorOd !== null && statorId !== null && poles !== null) {
    const ratio = round2(statorId / statorOd)
    // P0（0.1.4）：PMSM 场景按 is_pm 生产口径给期望比（0.72+0.010(p−2) 钳[0.70,0.80]），
    // 与 param-matrix 生成端同口径；缺省/异步仍走 legacy（0.55+0.03(p−2)），零回归。
    const usePm = isPmsmType(row?.motor_type)
    const expected = usePm ? idRatioByPoles(poles, { isPm: true }) : idRatio(poles)
    metrics.id_ratio = { actual: ratio, expected, is_pm: usePm }
    const dev = Math.abs(ratio - expected)
    if (dev > ID_RATIO_TOLERANCE.fail) {
      warn('V03', `内外径比 ${ratio} 严重偏离经验值 ${expected}（差 ${round2(dev)}），槽满率与轭部空间不可信`)
    } else if (dev > ID_RATIO_TOLERANCE.warn) {
      warn('V03', `内外径比 ${ratio} 偏离经验值 ${expected}（差 ${round2(dev)}）`)
    }
  } else {
    skipped.push('V03')
  }

  // ---- V04 长径比 λ ----
  if (coreLength !== null && statorId !== null && poles !== null) {
    const lam = validateLambda(coreLength, statorId, poles)
    metrics.lambda = { value: lam.lambda, range: lam.range, valid: lam.valid }
    if (!lam.valid) warn('V04', lam.advice || `λ=${lam.lambda} 越出 ${lam.range.join('~')}`)
  } else {
    skipped.push('V04')
  }

  // ---- V05 极槽配合 + 每极每相槽数 ----
  if (poles !== null && slotsStator !== null) {
    const usePm = isPmsmType(row?.motor_type)
    // PMSM（v0.1.6）：转子无笼型槽，slots_rotor 恒为 0，只校验定子槽是否在 PMSM_SLOT_MAP；
    // 异步/缺省：沿用 SLOT_MAP 的 [定子,转子] 二元组配对校验。
    const statorCandidates = usePm
      ? (PMSM_SLOT_MAP[poles] ?? [])
      : (SLOT_MAP[poles] ?? []).map(([s]) => s)
    metrics.pole_slot = {
      poles, slots_stator: slotsStator, slots_rotor: slotsRotor,
      is_pmsm: usePm, registered: statorCandidates.includes(slotsStator),
    }
    if (statorCandidates.length && !statorCandidates.includes(slotsStator)) {
      warn('V05', `定子槽数 ${slotsStator} 不在 ${poles}极登记组合 ${JSON.stringify(statorCandidates)} 内${usePm ? '（PMSM 定子槽表）' : ''}`)
    } else if (!statorCandidates.length) {
      warn('V05', `极数 ${poles} 无登记槽配合表，无法判定`)
    }
    const q = slotsStator / (poles * 3)
    metrics.q_slots_per_pole_per_phase = round2(q)
    if (Math.abs(q - Math.round(q)) > 1e-9) {
      warn('V05', `每极每相槽数 q=${q} 非整数（分数槽绕组），需核查绕组系数与齿槽转矩`)
    }
    // 仅异步机核查定转子槽差（PMSM 转子 0 槽，不做此判据）
    if (!usePm && slotsRotor !== null && slotsStator !== null) {
      const diff = Math.abs(slotsStator - slotsRotor)
      metrics.slot_diff = diff
      if (diff === 0) warn('V05', '定转子槽数相同，易产生同步附加转矩与起动死点')
    }
  } else {
    skipped.push('V05')
  }

  // ---- V06 并联支路数整除极数 ----
  if (poles !== null && parallel !== null) {
    metrics.parallel_circuits = parallel
    if (!(parallel > 0)) fail('V06', `并联支路数 ${parallel} 非正`)
    else if (poles % parallel !== 0) {
      fail('V06', `并联支路数 ${parallel} 不能整除极数 ${poles}，绕组无法均分`)
    }
  } else {
    skipped.push('V06')
  }

  // ---- V07 齿部磁密 ----
  const bt = toothFluxDensity({ statorId, toothWidth, slotsStator, airGapFlux })
  if (bt !== null) {
    const dev = Math.abs(bt - FLUX_TARGET.tooth) / FLUX_TARGET.tooth
    metrics.flux = { ...(metrics.flux ?? {}), tooth_t: bt, tooth_target: FLUX_TARGET.tooth, tooth_dev: round2(dev) }
    if (dev > FLUX_TOLERANCE.fail) {
      warn('V07', `齿磁密 ${bt}T 偏离目标 ${FLUX_TARGET.tooth}T 达 ${Math.round(dev * 100)}%（半齿距齿宽下 Bt≈1.30·B_gap，可反推所需 B_gap=${round2(bt / 1.30)}T）`)
    } else if (dev > FLUX_TOLERANCE.warn) {
      warn('V07', `齿磁密 ${bt}T 偏离目标 ${FLUX_TARGET.tooth}T 达 ${Math.round(dev * 100)}%`)
    }
  } else {
    skipped.push('V07')
  }

  // ---- V08 轭部磁密（v0.1.7：硬门禁，超饱和直接 failed）----
  // Bj 公式与齿磁密同源、物理正确；所谓「失真」是此前仅 warn、未能剔除饱和轭部候选。
  // 硅钢饱和磁感 Bs≈2.0~2.1T，工作磁密上限取 B_YOKE_SAT_T=1.9T：超过即轭部无法承载该磁通。
  // 同时保留对目标 0.82T 的偏离预警（dev），但饱和判据优先于目标偏离。
  const by = yokeFluxDensity({ statorId, yokeThickness, poles, airGapFlux })
  if (by !== null) {
    const dev = Math.abs(by - FLUX_TARGET.yoke) / FLUX_TARGET.yoke
    metrics.flux = {
      ...(metrics.flux ?? {}),
      yoke_t: by, yoke_target: FLUX_TARGET.yoke, yoke_dev: round2(dev),
      yoke_sat: by > B_YOKE_SAT_T,
    }
    if (by > B_YOKE_SAT_T) {
      // 轭部超过硅钢饱和极限：磁通无法由该轭厚承载，设计不可行（需加大定子外径/轭厚或降低气隙磁密）
      fail('V08', `轭磁密 ${by}T 超过硅钢饱和极限(${B_YOKE_SAT_T}T)，轭部将深度饱和、无法承载该磁通；`
        + `需加大定子外径/轭厚，或降低气隙磁密 B_gap（当前 ${airGapFlux}T）`)
    } else if (by > B_YOKE_WARN_T) {
      warn('V08', `轭磁密 ${by}T 偏高(>${B_YOKE_WARN_T}T)，轭部接近饱和，铁损与温升将恶化`)
    } else if (dev > FLUX_TOLERANCE.fail) {
      warn('V08', `轭磁密 ${by}T 偏离目标 ${FLUX_TARGET.yoke}T 达 ${Math.round(dev * 100)}%`)
    } else if (dev > FLUX_TOLERANCE.warn) {
      warn('V08', `轭磁密 ${by}T 偏离目标 ${FLUX_TARGET.yoke}T 达 ${Math.round(dev * 100)}%`)
    }
  } else {
    skipped.push('V08')
  }

  // ---- V09 槽形几何 ----
  if (statorOd !== null && statorId !== null && toothWidth !== null && slotsStator !== null) {
    const geo = slotGeometry({ statorOd, statorId, toothWidth, slotsStator })
    metrics.slot = geo
    if (!(geo.slot_depth > 0)) {
      fail('V09', `槽深 ${geo.slot_depth}mm 非正（内外径差过小）`)
    } else if (geo.slot_width <= 0) {
      fail('V09', `槽宽 ${geo.slot_width}mm ≤ 0：齿宽 ${toothWidth}mm 不小于齿距 ${geo.tooth_pitch}mm，无下线空间`)
    } else if (geo.aspect !== null && (geo.aspect < SLOT_ASPECT_RANGE[0] || geo.aspect > SLOT_ASPECT_RANGE[1])) {
      warn('V09', `槽宽深比 ${geo.aspect} 越出 ${SLOT_ASPECT_RANGE.join('~')}（${geo.slot_width}×${geo.slot_depth}mm）`)
    }
  } else {
    skipped.push('V09')
  }

  // ---- V10 电频率 ----
  if (speed !== null && poles !== null) {
    const f = round1((speed * poles) / 120)
    metrics.freq_hz = f
    if (f > FREQ_LIMITS.fail) {
      warn('V10', `电频率 ${f}Hz 越出常规适用域 ${FREQ_LIMITS.fail}Hz，铁损/集肤/变频器需专项评估`)
    } else if (f > FREQ_LIMITS.warn) {
      warn('V10', `电频率 ${f}Hz 高于 ${FREQ_LIMITS.warn}Hz，需核查高频铁损与集肤效应`)
    }
  } else {
    skipped.push('V10')
  }

  // ---- V11 转子轭厚度 ----
  if (rotorOd !== null && shaftDia !== null) {
    const yoke = round1((rotorOd - shaftDia) / 2)
    metrics.rotor_yoke_mm = yoke
    if (yoke < MIN_ROTOR_YOKE_MM) {
      fail('V11', `转子轭厚 ${yoke}mm < ${MIN_ROTOR_YOKE_MM}mm，磁路与机械强度不可靠`)
    }
  } else {
    skipped.push('V11')
  }

  // ---- V12 温升限值（需功率信息，缺省跳过而非静默通过）----
  const hasPower = num(row?.power_kw) !== null || num(row?.torque_nm) !== null
  if (opts.includeThermal === false) {
    skipped.push('V12')
  } else if (!hasPower) {
    skipped.push('V12')
  } else {
    try {
      const est = quickL0Estimate(row, {
        efficiencyCap: opts.efficiencyCap,
        maxTempClamp: opts.maxTempClamp,
        fluxDensity: opts.fluxDensity,
      })
      const rise = est.temp_rise
      const cls = opts.insulationClass ?? DEFAULT_INSULATION_CLASS
      const limit = INSULATION_LIMITS[cls] ?? INSULATION_LIMITS[DEFAULT_INSULATION_CLASS]
      metrics.thermal = {
        temp_rise_k: rise, insulation_class: cls, limit_k: limit,
        total_loss_w: est.total_loss, cooling: row?.cooling ?? null,
      }
      // ⚠ V12 默认只 warn，不 fail —— 依据：L0 温升模型尚未标定
      //   （散热面积未计散热筋、损耗三项仍欠标定，见 quality gate 的 DEBT 清单）。
      //   未标定的模型一旦判 failed，会把矩阵整批判死（实测 40/40 全 failed），
      //   预筛层就失去意义。需要硬剔除时用 escalate: ['V12'] 显式升级。
      if (rise > limit) {
        warn('V12', `温升 ${rise}K 超过 ${cls} 级限值 ${limit}K（冷却 ${row?.cooling ?? '默认'}）[模型未标定，结论供参考]`)
      } else if (rise > limit * 0.8) {
        warn('V12', `温升 ${rise}K 已达 ${cls} 级限值 ${limit}K 的 80%，余量不足`)
      }
    } catch (err) {
      warn('V12', `温升校验执行失败: ${String(err?.message ?? err)}`)
    }
  }

  // ---- V14 反电势闭环（P0 门禁）----
  // 用**行内 turns_per_coil**反算反电势 E，校验 |E − Uph|/Uph ≤ BACKEMF_TOL。
  // 注意：此处故意不用 deriveElectricalClosure（它会按电压重推匝数，忽略行内匝数），
  // 而是拿行内匝数算 E，专门抓「手填矩阵匝数与电压/频率不自洽」的违规。
  // 矩阵正常路径因 turns 由闭环生成，行内 E 必≈Uph，自然通过。
  if (statorId !== null && coreLength !== null && poles !== null && slotsStator !== null
      && turns !== null && parallel !== null && num(row?.voltage) !== null && speed !== null) {
    const f = (speed * poles) / 120
    const uph = (row?.connection === 'delta' ? num(row.voltage) : num(row.voltage) / Math.SQRT2 / Math.sqrt(1.5))
    const phi = (2 * airGapFlux * (statorId / 1000) * (coreLength / 1000)) / poles
    const nPhase = ((slotsStator / 3) * turns) / Math.max(1, parallel)
    const backEmf = 4.44 * f * nPhase * phi * WINDING_FACTOR
    const dev = uph > 0 ? Math.abs(backEmf - uph) / uph : 1
    metrics.back_emf = { e_v: round1(backEmf), uph_v: round1(uph), dev: round3(dev), ok: dev <= BACKEMF_TOL }
    if (dev > BACKEMF_TOL) {
      fail('V14', `反电势闭环失配：E=${round1(backEmf)}V 与相电压 ${round1(uph)}V 偏差 ${(dev * 100).toFixed(1)}% > ${(BACKEMF_TOL * 100).toFixed(0)}%（匝数/极数/气隙不自洽）`)
    }
  } else {
    skipped.push('V14')
  }

  // ---- V15 槽满率可行性（P0 门禁）----
  // 给定 peak_current 与 turns_per_coil 反算槽满率，> SLOT_FILL_MAX 判几何不可实现。
  if (statorOd !== null && statorId !== null && slotsStator !== null && poles !== null
      && peakCurrent !== null && turns !== null && parallel !== null) {
    const cu = estimateCopperLoss({
      statorOd, statorId, coreLength: coreLength ?? 80, slotsStator, poles,
      peakCurrent, turnsPerCoil: turns, parallelCircuits: parallel, airGap: airGap ?? 0.6,
    })
    metrics.slot_fill = { ratio: cu.slot_fill_ratio, area_mm2: cu.slot_area_mm2, max: SLOT_FILL_MAX }
    if (cu.slot_fill_ratio > SLOT_FILL_MAX) {
      fail('V15', `槽满率 ${cu.slot_fill_ratio} > ${SLOT_FILL_MAX}（槽面积 ${cu.slot_area_mm2}mm² 放不下 ${turns}匝×${peakCurrent}A 导体，需增大机座或降电流）`)
    } else if (cu.slot_fill_ratio > 0.70) {
      warn('V15', `槽满率 ${cu.slot_fill_ratio} 偏高（>0.70），下线困难且温升裕度小`)
    }
  } else {
    skipped.push('V15')
  }

  // ---- V13 每线圈匝数下限（附在 V06 组，不单独占号）----
  // v0.1.6：闭环反推可能给出 turns_per_coil=1（380V/高频低匝数场景，hairpin 扁线绕组可行），
  // 故下限改为 <1（仅排除真正不可实现的 0/负匝数），不再误报 1 匝方案。
  if (turns !== null && turns < 1) {
    fail('V06', `每线圈匝数 ${turns} < 1，绕组不可实现`)
  }

  return {
    status,
    issues,
    checked: RULE_CATALOG.length - skipped.length,
    skipped,
    metrics,
    params_summary: {
      stator_od: statorOd, stator_id: statorId, rotor_od: rotorOd,
      core_length: coreLength, air_gap: airGap, poles, slots_stator: slotsStator,
      slots_rotor: slotsRotor, speed,
    },
  }
}

/**
 * 批量校验并按状态分组
 * @param {object[]} rows
 * @param {object} [opts] 同 validateDesign
 * @returns {{passed: object[], warning: object[], failed: object[],
 *            summary: {total, passed, warning, failed}, reports: object[]}}
 */
export function validateDesignBatch(rows, opts = {}) {
  const reports = []
  const passed = []
  const warning = []
  const failed = []

  for (let i = 0; i < (rows?.length ?? 0); i += 1) {
    const row = rows[i]
    let report
    try {
      report = { index: i, ...validateDesign(row, opts) }
    } catch (err) {
      report = {
        index: i, status: 'failed',
        issues: [{ rule: 'V00', level: 'failed', msg: `校验执行异常: ${String(err?.message ?? err)}` }],
        checked: 0, skipped: [], metrics: {}, params_summary: {},
      }
    }
    reports.push(report)
    const item = { index: i, params: row, report }
    if (report.status === 'failed') failed.push(item)
    else if (report.status === 'warning') warning.push(item)
    else passed.push(item)
  }

  return {
    passed, warning, failed, reports,
    summary: {
      total: rows?.length ?? 0,
      passed: passed.length,
      warning: warning.length,
      failed: failed.length,
    },
  }
}

export default {
  RULE_CATALOG, ESCALATABLE_RULES,
  poleFlux, toothFluxDensity, yokeFluxDensity, slotGeometry,
  validateDesign, validateDesignBatch,
}
