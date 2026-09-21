/**
 * 电机设计常量 —— L0 估算层的数值基石
 * ===========================================================
 * 真源政策（v3 决策① P1）：
 *   本文件是 **构建期生成产物**，真源是 knowledge-sync/sync-constants.py
 *   从 Python 侧导出的 constants.json。W2-A 之前先用手工镜像版本（见下方行号），
 *   一旦运行 sync 校验，本文件必须能被重新生成且 **0 diff**。
 *   ⇒ 禁止在本文件里手写任何「只此一份」的魔数。
 *
 * 行号证据（Scripts/physics_kernel.py / Scripts/motor_tools.py）：
 *   LAMBDA_RANGE          physics_kernel.py:27-32
 *   FRAME_SHIFT_*         physics_kernel.py:35-39
 *   AIR_GAP_MIN/MAX       physics_kernel.py:42-43
 *   id_ratio              physics_kernel.py:573
 *   air_gap 经验式        physics_kernel.py:587-588
 *   rotor_od              physics_kernel.py:591
 *   SLOT_MAP              physics_kernel.py:560-566
 *   tooth_width           physics_kernel.py:598
 *   yoke_thickness        physics_kernel.py:601-602
 *   shaft_dia             physics_kernel.py:605
 *   turns 基数            physics_kernel.py:608
 *   无型谱回退            physics_kernel.py:536-538
 *   仿真效率模型          motor_tools.py:750-752
 *   仿真温升模型          motor_tools.py:755-756
 *   损耗拆分比            motor_tools.py:775-778
 */

// ═════════════════════════════════════════════════════════════
// 1. 几何与物理常量（Python 真源镜像）
// ═════════════════════════════════════════════════════════════

/** 长径比 λ = L/D 合理范围（按极数）—— physics_kernel.py:27-32 */
export const LAMBDA_RANGE = {
  2: [0.6, 1.3],
  4: [0.7, 1.4],
  6: [0.6, 1.2],
  8: [0.5, 1.1],
}

/** 缺省 λ 范围（未登记极数）—— physics_kernel.py:90 兜底 (0.5, 1.5) */
export const LAMBDA_RANGE_DEFAULT = [0.5, 1.5]

/**
 * P3 高速扩表（v3 决策③，纳入 W2）
 * ---------------------------------------------------------------
 * 缺口取证：Python 侧 LAMBDA_RANGE 仅覆盖 2/4/6/8 极，
 *           不覆盖用户的 200kW/22000rpm 高速工况（1 极、f≈367Hz、L_stack=170mm）。
 *           以下为 L0 侧扩展，属「新增资产」，Python 侧无同名真源，
 *           sync 脚本需将其列入 ALLOW_LOCAL_ONLY 白名单，不做 0-diff 比对。
 */
// 注：初版含 1 极档 [0.8, 1.6]，已作废 —— 永磁同步机不存在 1 极。
//     200kW/22000rpm 实为 2 极（f = 22000×2/120 = 366.7Hz），由 LAMBDA_RANGE[2] 覆盖。
export const LAMBDA_RANGE_EXT = {
  10: [0.5, 1.1],
  12: [0.5, 1.0],
}

/** 机座号跃迁阈值（转矩比）—— physics_kernel.py:35-39 */
export const FRAME_SHIFT_THRESHOLDS = { micro: 0.05, fine: 0.20, coarse: Infinity }

/** 气隙经验边界 (mm) —— physics_kernel.py:42-43 */
export const AIR_GAP_MIN = 0.30
export const AIR_GAP_MAX = 1.50

/**
 * 定子内外径比经验式 —— physics_kernel.py:573
 *   id_ratio = 0.55 + (pole - 2) * 0.03
 * P3 扩表：1 极按外推给 0.52（Python 侧无此档）
 */
export function idRatio(poles) {
  // poles < 2 在物理上非法（永磁同步机不存在 1 极），退化到 2 极档防御异常输入
  if (poles < 2) return 0.55
  return 0.55 + (poles - 2) * 0.03
}

/** 类比模式的 OD 估算比 —— physics_kernel.py:532 (base_od = base_d / 0.60) */
export const ANALOGY_OD_RATIO = 0.60

/**
 * 极槽配合表 [定子槽, 转子槽] —— physics_kernel.py:560-566
 * P3 扩表补充 1 极 / 12 极档（Python 侧无）
 */
export const SLOT_MAP = {
  2: [[18, 16], [24, 20], [30, 26]],
  4: [[24, 22], [36, 28], [48, 44]],
  6: [[36, 28], [54, 44], [72, 58]],
  8: [[48, 44], [54, 44], [72, 58]],
  10: [[60, 50], [72, 58], [90, 72]],
  12: [[72, 58], [90, 72], [108, 90]],     // P3 新增
}

/** 每线圈匝数基数 —— physics_kernel.py:608 */
export function baseTurns(voltage, statorOd) {
  return Math.max(6, Math.floor(voltage * 0.3 + (450 - statorOd) * 0.02))
}

/** 无型谱回退的经验尺寸 —— physics_kernel.py:536-538 */
export function empiricalBaseSize(targetTorqueNm) {
  const d = 100 + Math.pow(targetTorqueNm, 0.3) * 30
  return { d, l: d * 0.9 }
}

// ═════════════════════════════════════════════════════════════
// 2. L1 仿真模型系数（motor_tools.py:750-778）
//    用途：产出 l1_efficiency_proxy / max_temp 镜像字段，供 W4 回归比对
// ═════════════════════════════════════════════════════════════

/** 仿真效率模型：base = 85 + (od-180)*0.04 + (poles-8)*1.2，clamp[80,96] */
export const SIM_EFF = {
  base: 85.0, odRef: 180, odCoef: 0.04,
  poleRef: 8, poleCoef: 1.2,
  clampMin: 80, clampMax: 96,
}

/** 仿真温升模型：base = 55 + (od-180)*0.25 + (speed-3000)*0.004，clamp[45,130] */
export const SIM_TEMP = {
  base: 55, odRef: 180, odCoef: 0.25,
  speedRef: 3000, speedCoef: 0.004,
  clampMin: 45, clampMax: 130,
}

/** 损耗拆分比 —— motor_tools.py:775-777 */
export const LOSS_SPLIT = { copper: 0.55, iron: 0.30, mechanical: 0.15 }

/** 转矩/功率换算常数 —— physics_kernel.py:64 (T = 9550·P/n) */
export const TORQUE_CONST = 9550.0

// ═════════════════════════════════════════════════════════════
// 3. L0 物理估算通道系数（L0 原生，非 Python 真源）
// ═════════════════════════════════════════════════════════════

/**
 * 冷却方式 → 综合换热系数 W/(m²·K)
 * ---------------------------------------------------------------
 * ⚠ 取证修订（相对原资料稿）：资料稿给的 {8,15,40,60,80} 标定偏小约一个量级，
 *    直接套用会让温升虚高一倍以上（见下方标定算例）。
 *
 * 标定依据（本机实算，非查表）：
 *   样本：15kW / 3000rpm / 8极 / OD=180mm / L=100mm
 *   机座光滑外表面积 A = π·0.18·0.10 + π·0.09² ≈ 0.082 m²
 *   该样本典型总损耗 ≈ 733W（见 formula-engine.frame 实算路径）
 *   目标：TEFC 强迫风冷实测温升 ≈ 60~70K
 *   ⇒ 需要 h·A ≈ 11 W/K ⇒ h ≈ 140 W/(m²·K)
 * 故本表以「基于机座光滑外表面积的综合换热系数」口径重标定（已含散热筋折算）。
 *
 * 自然冷却取 25 是有意为之：小功率自然冷却可行，大功率会算出极高ΔT，
 * 这正是要触发 W4 物理校验告警的结果，不应人为放宽。
 */
export const COOLING_COEFFICIENT = {
  natural: 25,
  forced_air: 140,
  liquid_jacket: 350,
  oil_spray: 500,
  oil_immersed: 650,
}

/** 标准环境温度 °C（max_temp 镜像字段 = 环境温度 + 温升） */
export const AMBIENT_TEMP_C = 40

/** 缺省冷却方式（L0 私有；现有 Python 体系无 cooling 概念，交接时必须剔除） */
export const DEFAULT_COOLING = 'forced_air'

/** 极数 → 效率修正（极数越多，端部占比上升，效率略降） */
export const POLE_EFFICIENCY_FACTOR = {
  1: 1.005, 2: 1.0, 4: 0.995, 6: 0.99, 8: 0.985, 10: 0.98, 12: 0.975,
}

/** 铁损 Steinmetz 简化系数 */
export const STEINMETZ = { kh: 0.02, ke: 0.0001, alpha: 1.6, fluxDensity: 1.2 }

/** 电密取值基准 A/mm²（L1 侧 random.uniform(3.5, 7.5)） */
export const CURRENT_DENSITY_REF = 5.5

/** 齿部/轭部磁密目标（用户现场口径：齿 1.02T / 轭 0.82T @150°C） */
export const FLUX_TARGET = { tooth: 1.02, yoke: 0.82, temperatureC: 150 }

/** 叠压系数（用户现场口径） */
export const STACKING_FACTOR = 0.98

// ═════════════════════════════════════════════════════════════
// 3.5 W4 物理校验判据常量（L0 私有，Python 侧无同名真源）
// ═════════════════════════════════════════════════════════════

/**
 * 气隙磁密基准 (T) —— 齿/轭磁密反算的输入
 * PMSM 表贴/内置式气隙磁密典型 0.75~0.90T；取 0.80 作缺省，可由 config 覆盖。
 */
export const AIR_GAP_FLUX_DEFAULT = 0.80

/**
 * 绝缘等级 → 温升限值 (K)（GB/T 755 旋转电机定额与性能，电阻法）
 * B 级 80K / F 级 105K / H 级 125K
 */
export const INSULATION_LIMITS = { B: 80, F: 105, H: 125 }

/** 缺省按 F 级考核（工业变频电机主流） */
export const DEFAULT_INSULATION_CLASS = 'F'

/**
 * P3 高速气隙档：常规机气隙上限 AIR_GAP_MAX=1.5mm 不适用于高速工况。
 * 高速电机受转子动力学挠度、风磨损耗与表面损耗约束，气隙普遍取 1.5~3mm
 * （用户工况 200kW/22000rpm 即取 2.0mm），沿用 1.5mm 会误判为非法。
 */
export const HIGH_SPEED_RPM = 8000
export const AIR_GAP_MAX_HIGH_SPEED = 4.0

/** 气隙上限查询：高速工况放宽到 AIR_GAP_MAX_HIGH_SPEED */
export function airGapMax(speedRpm) {
  return (typeof speedRpm === 'number' && speedRpm >= HIGH_SPEED_RPM)
    ? AIR_GAP_MAX_HIGH_SPEED
    : AIR_GAP_MAX
}

/** 转子轭最小厚度 (mm)：低于此值磁路与机械强度均不可靠 */
export const MIN_ROTOR_YOKE_MM = 10

/** 电频率分档 (Hz)：>warn 提示高频铁损/集肤/变频器约束；>fail 判越界 */
export const FREQ_LIMITS = { warn: 400, fail: 1200 }

/** 磁密偏离目标的相对容差：超出 warn 提示，超出 fail 判不合理 */
export const FLUX_TOLERANCE = { warn: 0.25, fail: 0.50 }

/**
 * 槽宽/槽深比合理区间（过窄难下线，过宽漏抗大）
 * 下界取 0.20：定子梨形/梯形槽常见宽深比 0.2~0.8，深窄槽是高槽满率的常规做法；
 * 初版取下界 0.30 会误判掉绝大多数按 physics_kernel 派生的槽形（实测 26/40 命中），
 * 属判据过紧而非几何不合理。
 */
export const SLOT_ASPECT_RANGE = [0.20, 3.00]

/** 定子内外径比容差：超出 warn，超出 2 倍 fail（资料稿 §7.4 规则 2 的量化） */
export const ID_RATIO_TOLERANCE = { warn: 0.08, fail: 0.16 }

// ═════════════════════════════════════════════════════════════
// 4. 几何派生函数（与 Python 侧同名同式）
// ═════════════════════════════════════════════════════════════

/** 气隙经验式 —— physics_kernel.py:587-588（去掉 random 抖动，改确定性） */
export function empiricalAirGap(statorOd) {
  const raw = 0.30 + (statorOd - 120) * 0.0015
  return round2(clamp(raw, AIR_GAP_MIN, AIR_GAP_MAX))
}

/** 齿宽 —— physics_kernel.py:598 */
export function toothWidth(statorId, slotsStator) {
  return round2((Math.PI * statorId) / (2 * slotsStator))
}

/** 轭厚 —— physics_kernel.py:601-602 */
export function yokeThickness(statorOd, statorId) {
  const half = (statorOd - statorId) / 2
  const slotDepth = round1(half * 0.60)
  return round1(half - slotDepth)
}

/** 轴径 —— physics_kernel.py:605 */
export function shaftDia(rotorOd) {
  return round1(rotorOd * 0.35)
}

/** 转子外径 —— physics_kernel.py:591 */
export function rotorOd(statorId, airGap) {
  return round1(statorId - 2 * airGap)
}

/** λ 范围查询（含 P3 扩表） */
export function lambdaRange(poles) {
  return LAMBDA_RANGE[poles] ?? LAMBDA_RANGE_EXT[poles] ?? LAMBDA_RANGE_DEFAULT
}

// ═════════════════════════════════════════════════════════════
// 5. 数值工具
// ═════════════════════════════════════════════════════════════

export function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)) }
export function round1(v) { return Math.round(v * 10) / 10 }
export function round2(v) { return Math.round(v * 100) / 100 }
export function round3(v) { return Math.round(v * 1000) / 1000 }

/** 取 λ 范围内的修正：对目标尺寸做 *, 返回调整后的 L（不改 D），保持 D²L 语义 */
export function fitLambdaWithinRange(d, l, poles) {
  const [lo, hi] = lambdaRange(poles)
  const lam = d > 0 ? l / d : 0
  if (lam < lo) return round1(d * lo)
  if (lam > hi) return round1(d * hi)
  return round1(l)
}

export default {
  LAMBDA_RANGE, LAMBDA_RANGE_DEFAULT, LAMBDA_RANGE_EXT,
  FRAME_SHIFT_THRESHOLDS, AIR_GAP_MIN, AIR_GAP_MAX,
  idRatio, ANALOGY_OD_RATIO, SLOT_MAP, baseTurns, empiricalBaseSize,
  SIM_EFF, SIM_TEMP, LOSS_SPLIT, TORQUE_CONST,
  COOLING_COEFFICIENT, POLE_EFFICIENCY_FACTOR, STEINMETZ,
  CURRENT_DENSITY_REF, FLUX_TARGET, STACKING_FACTOR,
  AIR_GAP_FLUX_DEFAULT, INSULATION_LIMITS, DEFAULT_INSULATION_CLASS,
  MIN_ROTOR_YOKE_MM, FREQ_LIMITS, FLUX_TOLERANCE, SLOT_ASPECT_RANGE,
  ID_RATIO_TOLERANCE, HIGH_SPEED_RPM, AIR_GAP_MAX_HIGH_SPEED, airGapMax,
  empiricalAirGap, toothWidth, yokeThickness, shaftDia, rotorOd,
  lambdaRange, clamp, round1, round2, round3, fitLambdaWithinRange,
}
