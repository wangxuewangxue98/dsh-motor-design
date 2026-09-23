/**
 * 工具 1：motor_param_matrix —— 参数矩阵生成（聚焦扫描）
 * ===========================================================
 * 纯逻辑基于 Scripts/physics_kernel.py:486-696 focused_scan 改写，三处刻意差异：
 *
 *   ┌── 差异 1：去随机化 ──────────────────────────────────────┐
 *   │ Python 侧在 slots/turns/air_gap/peak_current 上用 random   │
 *   │ (:587 .uniform / :595 .choice / :609 / :629 / :634)。      │
 *   │ L0 要能被回归复现，改按「索引轮询」确定选取，同一 spec      │
 *   │ 永远产出同一矩阵。副作用是组合更全 —— 恰好利于广筛。       │
 *   └──────────────────────────────────────────────────────────┘
 *   ┌── 差异 2：无型谱依赖 ────────────────────────────────────┐
 *   │ Python 走 Knowledge/y_series_physics.json 查表。           │
 *   │ L0 要求零外部依赖，故统一走类比模式             │
 *   │ (empiricalBaseSize, :536-538)，把「型谱中心」交给调用方。   │
 *   │ 调用方若想用型谱中心，传 baseDiameter/baseLength 覆盖即可。│
 *   └──────────────────────────────────────────────────────────┘
 *   ┌── 差异 3：规模 ──────────────────────────────────────────┐
 *   │ Python focused_scan 目标 5-20 组；L0 是广筛层，           │
 *   │ 目标可达 config.maxMatrixSize（默认 2000）。              │
 *   └──────────────────────────────────────────────────────────┘
 *
 * 输出：每项严格含 L1_MATRIX_FIELDS(16) + _physics(8) + L0 私有(power_kw, cooling)
 * 可断言：assertMatrixShape(row).ok === true 且 missing 为空。
 */

import {
  idRatio, idRatioByPoles, isPmsmType, SLOT_MAP, PMSM_SLOT_MAP, baseTurns, empiricalBaseSize, rotorOd, toothWidth,
  yokeThickness, shaftDia, empiricalAirGap, fitLambdaWithinRange,
  PMSM_DEFAULT_POLE_ARC, PMSM_DEFAULT_PM_THICK_MM,
  round1, round2, clamp,
} from '../../lib/motor-constants.mjs'
import {
  normalizeSpec, assertMatrixShape, COOLING_ALLOWED, buildHandoff,
} from '../../lib/param-schema.mjs'
import { computeTorque, computeD2L, computeLambda, validateLambda, deriveElectricalClosure } from '../../lib/formula-engine.mjs'
import { getUsageSink, logUsage } from '../../lib/usage-log.mjs'

/** 工具入参声明（与 defineTool.parameters 保持一对一，避免两边漂移） */
export const TOOL_PARAMS = {
  power_kw: { type: 'number', required: true, description: '额定功率 (kW)' },
  speed_rpm: { type: 'number', required: true, description: '额定转速 (rpm)' },
  voltage_v: { type: 'number', description: '电压 (V)，默认 380' },
  poles: { type: 'number', description: '极数，缺省由转速推荐' },
  motor_type: {
    type: 'string',
    description:
      '电机类型（opt-in）。传 PMSM/BLDC/IPM 或中文别名时，内径比按永磁生产口径 ' +
      '0.72+0.010(p−2) 钳[0.70,0.80] 生成，更贴合 PMSM 真实几何；不传则维持 legacy 口径 ' +
      '0.55+0.03(p−2)（与程序 focused_scan 一致，零回归）。',
  },
  torque_nm: { type: 'number', description: '额定转矩 (Nm)，缺省由 9550·P/n 推算' },
  cooling: {
    type: 'string',
    description: `冷却方式，默认 forced_air。合法值: ${COOLING_ALLOWED.join('/')}`,
  },
  stator_od_limit: { type: 'number', description: '定子外径上限 (mm)，默认 450' },
  base_diameter: { type: 'number', description: '基准内径中心 (mm)，覆盖经验估算' },
  base_length: { type: 'number', description: '基准铁心长度中心 (mm)' },
  count: { type: 'number', description: '目标方案数，默认 20' },
}

/** 转速 → 推荐极数（ determinate 场景识别的简化版，对齐 determine_scenario 的选型直觉） */
export function recommendPoles(speedRpm) {
  // ⚠ 取证纠正：初版把 >12000rpm 归入「1 极档」是物理错误 —— 永磁同步机不存在 1 极。
  //   反推校验（现场工况）：200kW/22000rpm，取 2 极时 f = 22000×2/120 = 366.7Hz，
  //   与记录的「f≈367Hz」完全吻合 ⇒ 高速工况应归入 2 极，真正的约束是频率而非极数下限。
  if (speedRpm >= 2500) return 2
  if (speedRpm >= 1400) return 4
  if (speedRpm >= 900) return 6
  return 8
}

/** 候选极数：主极数 ± 2 档（physics_kernel.py:553-557） */
export function poleCandidates(poles) {
  const list = [poles]
  if (poles > 2) list.push(poles - 2)
  if (poles < 8) list.push(poles + 2)
  return list.filter((p) => p >= 1)
}

/** 等距取点（对齐 physics_kernel.py:_linspace 的用法） */
function linspace(lo, hi, n) {
  const count = Math.max(1, n)
  if (count === 1) return [round1((lo + hi) / 2)]
  const step = (hi - lo) / (count - 1)
  return Array.from({ length: count }, (_, i) => round1(lo + step * i))
}

/**
 * 生成参数扫描矩阵（纯函数，零依赖，可单测）
 * @param {object} rawSpec 工具入参（支持别名）
 * @param {object} [config] 插件配置：{ maxMatrixSize, topNPreview }
 * @returns {{matrix: object[], total: number, returned: number, truncated: boolean, spec: object, applied: string[]}}
 */
export function buildParamMatrix(rawSpec, config = {}) {
  const { spec, applied } = normalizeSpec(rawSpec)
  const maxMatrixSize = config.maxMatrixSize ?? 2000

  const powerKw = Number(spec.power_kw)
  const speedRpm = Number(spec.speed_rpm)
  if (!Number.isFinite(powerKw) || powerKw <= 0) {
    throw new Error('[motor_param_matrix] power_kw 必须为正数')
  }
  if (!Number.isFinite(speedRpm) || speedRpm <= 0) {
    throw new Error('[motor_param_matrix] speed_rpm 必须为正数')
  }

  const voltage = spec.voltage_v ?? 380
  const poles = spec.poles ?? recommendPoles(speedRpm)
  const torqueNm = spec.torque_nm ?? computeTorque(powerKw, speedRpm)
  const cooling = COOLING_ALLOWED.includes(spec.cooling) ? spec.cooling : 'forced_air'
  const odLimit = spec.stator_od_limit ?? 450
  const count = Math.min(maxMatrixSize, Math.max(1, Math.floor(spec.count ?? 20)))

  // ---- PMSM 口径开关（P0，0.1.4）----
  // 缺省 → legacy 口径 idRatio(pole)=0.55+0.03(p−2)，与 0.1.3 / 程序 focused_scan 逐字节一致（零回归）；
  // 显式传 PMSM/BLDC/IPM（含中文别名）→ is_pm 生产口径 0.72+0.010(p−2) 钳[0.70,0.80]，更贴合 PMSM 真实几何。
  const motorType = spec.motor_type ?? spec.motorType
  const usePm = isPmsmType(motorType)
  const ratioOf = (pole) => (usePm ? idRatioByPoles(pole, { isPm: true }) : idRatio(pole))

  // ---- 基准尺寸中心（类比模式）----
  const fallback = empiricalBaseSize(torqueNm)
  const baseD = round1(spec.base_diameter ?? fallback.d)
  const baseL = round1(spec.base_length ?? fallback.l)
  const scanRange = 0.15

  // ---- 扫描网格规模：保证够 count 又不至于暴量 ----
  const poleList = poleCandidates(poles)
  const perPole = Math.max(1, Math.ceil(count / poleList.length))
  const axis = Math.max(2, Math.ceil(Math.sqrt(perPole)))

  const dSteps = linspace(baseD * (1 - scanRange), baseD * (1 + scanRange), axis)
  const lSteps = linspace(baseL * (1 - scanRange), baseL * (1 + scanRange), axis)

  const matrix = []
  const seenKeys = new Set()
  let comboIndex = 0

  outer:
  for (const d of dSteps) {
    for (const l of lSteps) {
      for (const pole of poleList) {
        const key = `${Math.round(d)}|${Math.round(l)}|${pole}`
        if (seenKeys.has(key)) continue
        seenKeys.add(key)

        // 外径超限：不丢弃候选，而是把外径夹到上限并反算内径 ——
        // 用户既然给了外径限制，就必须在限制内给出可行方案；
        // 直接 continue 会在「D²L 基准尺寸偏大 + 限制偏紧」时产出空矩阵。
        let od = Math.round(d / ratioOf(pole))
        let dEff = d
        let odClamped = false
        if (od > odLimit) {
          od = Math.round(odLimit)
          dEff = round1(od * ratioOf(pole))
          odClamped = true
        }

        const airGap = empiricalAirGap(od)
        const rOd = rotorOd(dEff, airGap)

        // 确定性槽配合轮询：不再 random.choice
        // PMSM（v0.1.6 修复）：转子无笼型槽 → slots_rotor=0，定子槽取 PMSM_SLOT_MAP；
        // 异步/缺省 → 沿用 SLOT_MAP 的 [定子,转子] 二元组（零回归）。
        const isPm = usePm
        const statorSlotCandidates = isPm
          ? (PMSM_SLOT_MAP[pole] ?? PMSM_SLOT_MAP[8])
          : (SLOT_MAP[pole] ?? SLOT_MAP[8]).map((pair) => pair[0])
        const slotsStator = statorSlotCandidates[comboIndex % statorSlotCandidates.length]
        const slotsRotor = isPm ? 0 : (SLOT_MAP[pole] ?? SLOT_MAP[8])[comboIndex % (SLOT_MAP[pole] ?? SLOT_MAP[8]).length][1]
        const parallels = [1, 2]

        const coreLength = fitLambdaWithinRange(dEff, l, pole)
        const lamCheck = validateLambda(coreLength, dEff, pole)
        const d2l = computeD2L(dEff, coreLength)
        const baseD2l = computeD2L(baseD, baseL)
        const estTorque = baseD2l > 0 ? round2((torqueNm * d2l) / baseD2l) : torqueNm

        // ---- 电气闭环（v0.1.6 核心修复）----
        // 由电压/转速/极数/几何反推自洽的 peak_current 与 turns_per_coil，
        // 彻底取代原 baseTurns(voltage,od) 经验式与 [50,80,100] 固定电流表。
        // 未传 motor_type 时仍按 legacy 几何，但电流/匝数一律走闭环（零回归安全：
        // 旧路径本就未对 N/I 做物理约束，新路径只会让它们更可信）。
        const closure = deriveElectricalClosure({
          voltage, speed: speedRpm, poles: pole,
          statorId: round1(dEff), coreLength, airGap,
          slotsStator, parallelCircuits: parallels[comboIndex % parallels.length],
          powerKw,
        })
        const turnsPerCoil = closure?.turns_per_coil ?? Math.max(6, baseTurns(voltage, od))
        const peakCurrent = closure?.peak_current ?? 80

        matrix.push({
          stator_od: od,
          stator_id: round1(dEff),
          rotor_od: rOd,
          core_length: coreLength,
          air_gap: airGap,
          tooth_width: toothWidth(dEff, slotsStator),
          yoke_thickness: yokeThickness(od, dEff),
          shaft_dia: shaftDia(rOd),
          poles: pole,
          voltage,
          peak_current: peakCurrent,
          speed: speedRpm,
          slots_stator: slotsStator,
          slots_rotor: slotsRotor,
          turns_per_coil: turnsPerCoil,
          parallel_circuits: parallels[comboIndex % parallels.length],
          // ---- L0 私有（不进交接载荷，由 pickL1Payload 过滤）----
          power_kw: powerKw,
          torque_nm: torqueNm,
          cooling,
          // PMSM 场景显式传入时标记；缺省（motorType=undefined）不产生该键
          // → 默认路径的行对象与 0.1.3 逐字节零回归（否则内存对象会多挂 undefined 键）
          ...(motorType !== undefined ? { motor_type: motorType } : {}),
          // ---- 物理核验 ----
          _physics: {
            d_squared_l: d2l,
            lambda: computeLambda(coreLength, dEff),
            lambda_valid: lamCheck.valid,
            lambda_advice: lamCheck.advice,
            od_clamped: odClamped,
            estimated_torque: estTorque,
            ref_model: 'L0-类比估算',
            scenario: 'analogy',
            target_torque: torqueNm,
            // v0.1.6：电气闭环派生量 + PMSM 几何种子（项目基准 极弧0.688 / PM厚12mm）
            electrical_frequency_hz: closure?.freq_hz ?? round1((speedRpm * pole) / 120),
            back_emf_v: closure?.back_emf_v ?? null,
            back_emf_ok: closure?.back_emf_ok ?? null,
            line_current_rms: closure?.line_current_rms ?? null,
            ...(isPm ? {
              rotor_type: 'pm_synchronous_no_cage',
              pole_arc_ratio: PMSM_DEFAULT_POLE_ARC,
              pm_thickness_mm: PMSM_DEFAULT_PM_THICK_MM,
            } : {}),
          },
        })

        comboIndex += 1
        if (matrix.length >= count) break outer
      }
    }
  }

  // ---- 按 D²L 与目标转矩的匹配度排序（physics_kernel.py:689-694）----
  const targetD2l = computeD2L(baseD, baseL)
  if (targetD2l > 0) {
    matrix.sort((a, b) =>
      Math.abs(a._physics.d_squared_l - targetD2l) -
      Math.abs(b._physics.d_squared_l - targetD2l))
  }

  // truncated 必须拿「原始请求量」比，不能拿被 maxMatrixSize 夹断后的 count 比 ——
  // 后者恒为 false，会掩盖真实截断。
  const returned = matrix.slice(0, maxMatrixSize)
  const truncated = Math.max(1, Math.floor(spec.count ?? 20)) > returned.length

  // ---- 形状自检：任何一行缺 L1 严格必填都视为内部错误 ----
  for (const row of returned) {
    const shape = assertMatrixShape(row)
    if (!shape.ok) {
      throw new Error(
        `[motor_param_matrix] 矩阵行形状异常: missing=${shape.missing.join(',')} typeErrors=${shape.typeErrors.join(';')}`
      )
    }
  }

  return {
    matrix: returned,
    total: matrix.length,
    returned: returned.length,
    truncated,
    spec: {
      power_kw: powerKw, speed_rpm: speedRpm, voltage_v: voltage, poles,
      ...(motorType !== undefined ? { motor_type: motorType } : {}),
      torque_nm: torqueNm, cooling, count,
    },
    applied,
  }
}

/**
 * 工具注册（薄封装）
 * 动态 import '@deepseek-ai/dsh-tools' 而非顶层 import：
 * 保证本文件可在无 DSH Runtime 的环境下被 node 直接加载做单测。
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {object} config
 */
export async function registerParamMatrix(ctx, config = {}) {
  const { defineTool } = await import('@deepseek-ai/dsh-tools')

  ctx.tools.register(defineTool({
    name: 'motor_param_matrix',
    description:
      '根据设计需求规格生成电机参数扫描矩阵（聚焦扫描）——电机设计流水线的第一步。\n' +
      '输入额定功率、转速、电压等规格，输出可直接喂给 L1 求解器的参数组合列表。\n' +
      '每个组合严格包含 L1 所需的 16 个顶层字段与 _physics 物理核验字段。\n' +
      '产出可直接作为 params_list 传给 motor_design_validate（先校验剔除 failed）\n' +
      '和 motor_l0_estimate（再估算排序），三者构成标准流水线。\n' +
      '本工具为纯本地计算，毫秒级返回，不调用任何外部求解器。',
    parameters: TOOL_PARAMS,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args) {
      const t0 = Date.now()
      try {
        const result = buildParamMatrix(args, config)
        logUsage(getUsageSink(config), {
          tool: 'motor_param_matrix', ok: true, elapsed_ms: Date.now() - t0,
          n: result?.total ?? result?.matrix?.length ?? 0,
        })
        return JSON.stringify(result, null, 2)
      } catch (err) {
        logUsage(getUsageSink(config), {
          tool: 'motor_param_matrix', ok: false, elapsed_ms: Date.now() - t0,
          error: String(err?.message ?? err),
        })
        return JSON.stringify({ error: true, message: String(err?.message ?? err) }, null, 2)
      }
    },
  }))
}

export default { buildParamMatrix, registerParamMatrix, TOOL_PARAMS, recommendPoles, poleCandidates }
