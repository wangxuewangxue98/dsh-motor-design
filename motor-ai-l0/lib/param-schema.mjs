/**
 * Param 锁 —— L0 参数字典的「单一字段真源」
 * ===========================================================
 * 存在理由（v1 取证 #2 / v3 §3.2 决策②）：
 *   原资料稿用 {slots, cooling, speed_rpm}，而现有 L1 真源是另一套名字，
 *   两侧各写一份必然漂移。本模块把字段名、别名、必填性、交接白名单集中定义，
 *   param-matrix / l0-estimate / 后续 design-validate 一律从此处取字段，
 *   禁止任何模块手写字段字面量。
 *
 * 真源证据（行号对应 Main worktree 的既有文件）：
 *   - L1 参数矩阵输出字典：Scripts/physics_kernel.py:618-646（focused_scan designs.append）
 *   - L1 模拟求解输入字典：Scripts/motor_tools.py:726-778（_run_simulated）
 *   - L1 模拟求解输出字典：Scripts/motor_tools.py:785+（return {...}）
 *
 * 三条硬规则：
 *   1. 任何「要喂给 L1」的参数，字段名必须落在本文件的 L1_MATRIX_FIELDS 里
 *   2. L1_STRICT_REQUIRED 四个字段缺一即 KeyError（Python 侧为 params["x"] 直接索引）
 *   3. 属于 L0 私有的字段（如 cooling）不得进入交接载荷 —— 见 pickL1Payload()
 *
 * 本模块零依赖、纯数据 + 纯函数，可被 Node 直接 import 做单测。
 */

// ═════════════════════════════════════════════════════════════
// 1. L1 侧字段（严格镜像 physics_kernel.py:618-646）
// ═════════════════════════════════════════════════════════════

/**
 * L1 严格必填 —— motor_tools.py:732-735
 *   od = params["stator_od"]; poles = params["poles"]
 *   voltage = params["voltage"]; speed = params["speed"]
 * 这 4 个键在 Python 侧是 **直接索引**，不是 .get()，缺失直接 KeyError。
 */
export const L1_STRICT_REQUIRED = ['stator_od', 'poles', 'voltage', 'speed']

/**
 * L1 参数矩阵顶层字段全量 —— physics_kernel.py:618-634
 * 顺序与 Python 侧 append 顺序一致，便于人工 diff。
 */
export const L1_MATRIX_FIELDS = [
  'stator_od',          // int   定子外径 mm
  'stator_id',          // float 定子内径 mm
  'rotor_od',           // float 转子外径 mm
  'core_length',        // float 铁心长度 mm
  'air_gap',            // float 气隙 mm
  'tooth_width',        // float 齿宽 mm
  'yoke_thickness',     // float 轭厚 mm
  'shaft_dia',          // float 轴径 mm
  'poles',              // int   极数
  'voltage',            // number 电压 V
  'peak_current',       // number 峰值电流 A
  'speed',              // number 转速 rpm
  'slots_stator',       // int   定子槽数
  'slots_rotor',        // int   转子槽数
  'turns_per_coil',     // int   每线圈匝数
  'parallel_circuits',  // int   并联支路数
]

/**
 * _physics 物理核验子结构 —— physics_kernel.py:636-645
 */
export const PHYSICS_FIELDS = [
  'd_squared_l',        // float D²L (m³)
  'lambda',             // float 长径比 L/D
  'lambda_valid',       // bool  λ 是否落在 LAMBDA_RANGE 内
  'lambda_advice',      // str   λ 越界的处理建议
  'estimated_torque',   // float D²L 类比推算转矩 Nm
  'ref_model',          // str   参照机型
  'scenario',           // str   'catalog' | 'analogy'
  'target_torque',      // float 修正后目标转矩 Nm
]

/**
 * L1 模拟输出字段（用于 L0 → L1 同口径镜像）—— motor_tools.py:785+
 */
export const L1_OUTPUT_FIELDS = [
  'efficiency', 'max_temp', 'torque', 'power',
  'stator_od', 'stator_id', 'rotor_od', 'core_length', 'air_gap',
  'tooth_width', 'yoke_thickness', 'shaft_dia',
  'poles', 'voltage', 'speed', 'slots_stator', 'slots_rotor', 'turns_per_coil',
  'stator_tooth_flux_density', 'stator_yoke_flux_density',
  'rotor_yoke_flux_density', 'air_gap_flux_density',
  'current_density', 'power_factor',
  'copper_loss', 'iron_loss', 'mechanical_loss', 'total_loss',
  'thermal_factor', 'copper_temp', 'magnet_temp',
]

// ═════════════════════════════════════════════════════════════
// 2. 字段类型与单位（Param 锁的「字典」本体）
// ═════════════════════════════════════════════════════════════

/**
 * 字段 Schema：type / unit / required / source
 * source 取值：
 *   'L1'        —— 必须与 Python 侧同名，改动需同步 physics_kernel.py
 *   'L1-output' —— L1 求解输出字段名
 *   'L0'        —— L0 私有字段，不参与交接
 */
export const PARAM_SCHEMA = {
  // ---- L1 输入侧 ----
  stator_od:         { type: 'integer', unit: 'mm',  required: true,  source: 'L1' },
  stator_id:         { type: 'number',  unit: 'mm',  required: true,  source: 'L1' },
  rotor_od:          { type: 'number',  unit: 'mm',  required: true,  source: 'L1' },
  core_length:       { type: 'number',  unit: 'mm',  required: true,  source: 'L1' },
  air_gap:           { type: 'number',  unit: 'mm',  required: true,  source: 'L1' },
  tooth_width:       { type: 'number',  unit: 'mm',  required: true,  source: 'L1' },
  yoke_thickness:    { type: 'number',  unit: 'mm',  required: true,  source: 'L1' },
  shaft_dia:         { type: 'number',  unit: 'mm',  required: true,  source: 'L1' },
  poles:             { type: 'integer', unit: '',    required: true,  source: 'L1' },
  voltage:           { type: 'number',  unit: 'V',   required: true,  source: 'L1' },
  peak_current:      { type: 'number',  unit: 'A',   required: true,  source: 'L1' },
  speed:             { type: 'number',  unit: 'rpm', required: true,  source: 'L1' },
  slots_stator:      { type: 'integer', unit: '',    required: true,  source: 'L1' },
  slots_rotor:       { type: 'integer', unit: '',    required: true,  source: 'L1' },
  turns_per_coil:    { type: 'integer', unit: '',    required: true,  source: 'L1' },
  parallel_circuits: { type: 'integer', unit: '',    required: true,  source: 'L1' },

  // ---- L0 私有：不进交接载荷 ----
  power_kw:          { type: 'number',  unit: 'kW',  required: true,  source: 'L0' },
  torque_nm:         { type: 'number',  unit: 'Nm',  required: true,  source: 'L0' },
  cooling:           { type: 'string',  unit: '',    required: false, source: 'L0' },
  motor_type:        { type: 'string',  unit: '',    required: false, source: 'L0' },

  // ---- L1 输出侧镜像 ----
  efficiency:        { type: 'number',  unit: '%',   required: false, source: 'L1-output' },
  max_temp:          { type: 'number',  unit: 'C',   required: false, source: 'L1-output' },
  power:             { type: 'number',  unit: 'kW',  required: false, source: 'L1-output' },
  copper_loss:       { type: 'number',  unit: 'W',   required: false, source: 'L1-output' },
  iron_loss:         { type: 'number',  unit: 'W',   required: false, source: 'L1-output' },
  mechanical_loss:   { type: 'number',  unit: 'W',   required: false, source: 'L1-output' },
  total_loss:        { type: 'number',  unit: 'W',   required: false, source: 'L1-output' },
}

// ═════════════════════════════════════════════════════════════
// 3. L0 结果字段（v3 决策②：双字段策略）
// ═════════════════════════════════════════════════════════════

/** L0 原生字段 —— 物理损耗通道的独立产出 + v0.1.6 可追溯诊断字段 */
export const L0_NATIVE_FIELDS = [
  'efficiency',       // %   物理损耗模型推算
  'torque',           // Nm  9550·P/n
  'temp_rise',        // K   热负荷法温升（注意：与 max_temp 的 °C 口径不同）
  'total_loss',       // W   铜损+铁损+机械损
  'torque_density',   // Nm/m³
  'prediction_source',// 'formula' | 'surrogate'
  // ---- v0.1.6 可追溯诊断（让报告「每值给公式、可判定」）----
  'electrical_frequency_hz', // Hz   n·p/120
  'air_gap_flux_density',   // T   气隙磁密基准（缺省 0.80）
  'tooth_flux_density',     // T   齿磁密 2·Bg·Dsi/(Qs·bt·k_stack)
  'yoke_flux_density',      // T   轭磁密 Bg·Dsi/(p·yoke·k_stack)
  'current_density',        // A/mm²  设计电密 J_DESIGN
  'power_factor',           // -    功率因数基准
  'slot_fill_ratio',        // -    槽满率（几何可行性）
  'back_emf_v',             // V    反电势闭环值
  'back_emf_ok',            // bool 反电势自洽
  'thermal_rise_k',         // K    温升
  'thermal_limit_k',        // K    绝缘等级温升限值
  'insulation_class',       // -    B/F/H
  'peak_current',           // A    峰值电流（闭环反推）
  'turns_per_coil',         // -    每线圈匝数（闭环反推）
  'parallel_circuits',      // -    并联支路
  'feasible',               // bool 电气+几何+热三关全过
  'verdict',                // str  feasible / infeasible_geometry / infeasible_thermal
]

/** L0 元信息 */
export const L0_META_FIELDS = ['confidence']

/**
 * L1 镜像字段 —— 让 L0 结果能被 L1 下游零翻译消费
 *
 * ⚠ 取值来源已修订：镜像字段一律取自 **L0 物理通道**，而非 Python 仿真经验式。
 *   原因：SIM_EFF/SIM_TEMP 是在 ~180mm 外径 / 8 极 / 3000rpm 附近标定的，
 *   把它套到 200kW/22000rpm 会推出 80% 效率、130°C 触顶这类失真值；
 *   若镜像照抄，同一行结果会出现 native.total_loss 与 mirror.copper_loss 互不咬合。
 *   故：仿真口径只作为 **诊断列**（见 L1_DIAG_FIELDS），不进可消费镜像。
 */
export const L1_MIRROR_FIELDS = [
  'max_temp',              // °C   环境温度 + L0 温升，钳位到 [45,130]
  'power',                 // kW   torque·speed/9550（同 motor_tools.py:760）
  'copper_loss',           // W    L0 总损耗按 0.55 拆分（同 L775 拆分比）
  'iron_loss',             // W    L0 总损耗按 0.30 拆分（同 L776 拆分比）
  'mechanical_loss',       // W    L0 总损耗按 0.15 拆分（同 L777）
  'solve_mode',            // 'l0' 恒定标记，下游据此识别未精算
  'l1_handoff',            // obj  TopN 交接载荷（上层汇总时挂载）
]

/**
 * 诊断列 —— 只用于量化「L0 物理通道」与「Python 仿真经验式」的偏差，
 * 供 W4 回归质量门判定是否需要标定，**不可被下游消费**。
 */
export const L1_DIAG_FIELDS = [
  'l1_efficiency_proxy',   // %  SIM_EFF 去噪克隆（motor_tools.py:750-752）
  'l1_temp_proxy',         // °C SIM_TEMP 去噪克隆（motor_tools.py:755-756）
]

/** 允许进入交接载荷的字段（= L1 输入侧 + _physics） */
export const HANDOFF_WHITELIST = [...L1_MATRIX_FIELDS]

// ═════════════════════════════════════════════════════════════
// 4. 工具入参别名（吸收资料稿与口语化命名的差异）
// ═════════════════════════════════════════════════════════════

/**
 * 工具入参别名表 —— 键为「外部写法」，值为「规范名」
 * 设计动机：原资料稿用 slots/speed_rpm/cooling，而 Python 真源用
 *           slots_stator/speed。别名表让两侧的调用都能归一化，
 *           但**规范化之后只认规范名**，杜绝两套管用。
 */
export const SPEC_ALIASES = {
  power: 'power_kw',
  power_kw: 'power_kw',
  p_kw: 'power_kw',
  speed: 'speed_rpm',
  rpm: 'speed_rpm',
  speed_rpm: 'speed_rpm',
  voltage: 'voltage_v',
  voltage_v: 'voltage_v',
  volt: 'voltage_v',
  od_limit: 'stator_od_limit',
  stator_od_limit: 'stator_od_limit',
  torque: 'torque_nm',
  torque_nm: 'torque_nm',
  count: 'count',
  design_count: 'count',
  pole: 'poles',
  poles: 'poles',
}

/** 冷却方式合法取值（L0 私有，L1 侧无此概念） */
export const COOLING_ALLOWED = [
  'natural', 'forced_air', 'liquid_jacket', 'oil_spray', 'oil_immersed',
]

// ═════════════════════════════════════════════════════════════
// 5. 纯函数：归一化 / 形状校验 / 交接载荷
// ═════════════════════════════════════════════════════════════

/**
 * 归一化工具入参：把别名写法统一成规范名
 * @param {Record<string, unknown>} rawSpec
 * @returns {{spec: Record<string, unknown>, applied: string[], dropped: string[]}}
 */
export function normalizeSpec(rawSpec) {
  const spec = {}
  const applied = []
  const dropped = []

  for (const [key, value] of Object.entries(rawSpec ?? {})) {
    if (value === undefined || value === null) continue
    if (Object.prototype.hasOwnProperty.call(SPEC_ALIASES, key)) {
      const canonical = SPEC_ALIASES[key]
      if (canonical !== key) applied.push(`${key} → ${canonical}`)
      spec[canonical] = value
    } else {
      // 未登记别名：原样透传，但记入 dropped 供调用方审计
      dropped.push(key)
      spec[key] = value
    }
  }
  return { spec, applied, dropped }
}

/**
 * 校验单个矩阵项是否满足 L1 输入形状
 * @param {Record<string, unknown>} row
 * @returns {{ok: boolean, missing: string[], unknown: string[], typeErrors: string[]}}
 */
export function assertMatrixShape(row) {
  const missing = []
  const typeErrors = []
  const unknown = []

  for (const field of L1_STRICT_REQUIRED) {
    if (!(field in row) || row[field] === undefined || row[field] === null) {
      missing.push(field)
    }
  }

  for (const [field, meta] of Object.entries(PARAM_SCHEMA)) {
    if (meta.source !== 'L1') continue
    if (!(field in row)) continue
    const v = row[field]
    if (meta.type === 'integer' && !Number.isInteger(v)) {
      typeErrors.push(`${field} 应为 integer，实际 ${typeof v}:${v}`)
    } else if (typeof v !== 'number' && typeof v !== 'string') {
      typeErrors.push(`${field} 类型异常: ${typeof v}`)
    }
  }

  for (const field of Object.keys(row)) {
    if (field === '_physics') continue
    if (!Object.prototype.hasOwnProperty.call(PARAM_SCHEMA, field)) {
      unknown.push(field)
    }
  }

  return { ok: missing.length === 0 && typeErrors.length === 0, missing, typeErrors, unknown }
}

/**
 * 从一行结果里摘出「可直接喂给 L1」的参数载荷
 * 规则：只保留 HANDOFF_WHITELIST 内字段，剔除 L0 私有字段（power_kw/torque_nm/cooling）
 *       以及所有 L1-output 镜像字段，避免污染下游入参。
 *
 * 形状容错：L0 结果有两种摆放方式
 *   - 嵌套式 {params: {...}, efficiency, ...}（资料稿 §9 的口径，本仓库采用）
 *   - 平铺式 {stator_od, poles, ...}（矩阵原始行）
 * 两种都认，优先取 params 子对象。
 *
 * @param {Record<string, unknown>} row
 * @returns {Record<string, unknown>}
 */
export function pickL1Payload(row) {
  const src = (row && typeof row.params === 'object' && row.params !== null)
    ? row.params
    : row
  const payload = {}
  for (const field of HANDOFF_WHITELIST) {
    if (Object.prototype.hasOwnProperty.call(src, field)) {
      payload[field] = src[field]
    }
  }
  return payload
}

/**
 * 构造 C 部交接载荷（v3 §10.1：L0 → L1 handoff）
 * @param {Array<Record<string, unknown>>} candidates L0 TopN 的完整结果行
 * @param {{ fromLevel?: string, toLevel?: string, ranking?: string[], timestamp?: string }} [opts]
 */
export function buildHandoff(candidates, opts = {}) {
  return {
    handoff: {
      from_level: opts.fromLevel ?? 'l0',
      to_level: opts.toLevel ?? 'l1',
      candidates: candidates.map(pickL1Payload),
      l0_ranking: opts.ranking ?? ['efficiency', 'torque_density'],
      timestamp: opts.timestamp ?? new Date().toISOString(),
    },
  }
}

/**
 * 合并「L0 原生 + L1 镜像」双字段结果（v3 决策②落地）
 * @param {{params: object, native: object, mirror: object, confidence?: number}} input
 */
export function buildL0Result({ params, native, mirror, confidence = 1.0 }) {
  const result = { params }
  for (const f of L0_NATIVE_FIELDS) {
    if (f in native) result[f] = native[f]
  }
  // 可消费镜像 + 诊断列都要带上：诊断列是 W4 回归门的输入，丢了回归门就瞎了
  for (const f of [...L1_MIRROR_FIELDS, ...L1_DIAG_FIELDS]) {
    if (f in mirror) result[f] = mirror[f]
  }
  result.confidence = confidence
  return result
}

export default {
  L1_STRICT_REQUIRED,
  L1_MATRIX_FIELDS,
  PHYSICS_FIELDS,
  L1_OUTPUT_FIELDS,
  PARAM_SCHEMA,
  L0_NATIVE_FIELDS,
  L0_META_FIELDS,
  L1_MIRROR_FIELDS,
  L1_DIAG_FIELDS,
  HANDOFF_WHITELIST,
  SPEC_ALIASES,
  COOLING_ALLOWED,
  normalizeSpec,
  assertMatrixShape,
  pickL1Payload,
  buildHandoff,
  buildL0Result,
}
