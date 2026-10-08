/**
 * L0 能力门（applicability gate）—— v0.2.5 新增
 * ============================================================================
 * 目的
 *   L0 是「类比 + 经验公式」估算器，其几何/磁密/反电势模型全部围绕
 *   **工频中低压异步电机**标定。超出该标定域时，模型不会给出错误数字，
 *   而是把整批判死（recommended = null），让调用方只看到「无解」，
 *   无法区分「设计真的不可行」与「这个工具不适用」。
 *
 *   本模块在**调用前**判定工况是否落在 L0 的可信域内，并给出明确的
 *   路由建议（继续用 L0 / 改工况 / 直接进 L1），把空转变成可执行决策。
 *
 * 判定依据（全部可追溯到常量，不含经验拍脑袋的隐藏系数）
 *   1. 电频率      f = n·p/120 vs FREQ_LIMITS —— 高速时铁损/集肤/变频器约束
 *                   在 L0 的损耗模型里完全没有体现，结果不可信
 *   2. 电压等级    L0 的 Bg 基准 0.80T 与绕组匝数闭环按 ≤690V 工况标定；
 *                   380V + 极高压大功率 ⇒ 理想匝数趋近 0，圆线整匝绕组无解
 *   3. 转速        高速 (>HIGH_SPEED_RPM) 时转子动力学/风摩/刚度约束
 *                   L0 只有经验风摩公式，无机械强度校核
 *   4. 机种        PMSM 的 dq/弱磁/退磁/转矩脉动在 L0 完全无模型
 *
 * 判定结果三档
 *   ok       —— 落在标定域内，正常走 L0
 *   caution  —— 边界附近，结果仅供横向比较，不可作定量结论（仍返回结果）
 *   reject   —— 超出标定域，**明确拒绝**并给路由建议（不返回伪结果）
 *
 * 设计原则：能力门只判「模型适用性」，不判「设计可行性」。
 *   前者是工具边界（客观、可枚举），后者是工程判断（需专业上下文）。
 *   二者混淆会导致「工具越界 ⇒ 谎称设计不可行」，这正是 v0.2.4 之前的缺陷。
 */

/** 能力门判定的适用域档位常量（集中在此，便于调参与文档引用） */
export const APPLICABILITY = {
  /** L0 标定域上界 —— 电压等级（V）。超过则理想匝数趋 0，绕组无解 */
  VOLTAGE_CALIBRATED_MAX: 690,
  /** 判 caution 的电压上界（V） */
  VOLTAGE_CAUTION: 400,
  /** L0 标定域上界 —— 电频率（Hz）。超过则损耗模型失真 */
  FREQ_CALIBRATED_MAX: 400,
  /** 判 caution 的电频率上界（Hz） */
  FREQ_CAUTION: 200,
  /** L0 标定域上界 —— 转速（rpm）。超过则无机械强度/风摩校核 */
  SPEED_CALIBRATED_MAX: 3000,
  /** 判 caution 的转速上界（rpm） */
  SPEED_CAUTION: 1500,
  /** 理想每线圈匝数下界：低于此值圆线整匝绕组在物理上无解 */
  MIN_IDEAL_TURNS_PER_COIL: 1.0,
}

/**
 * 判定 L0 对给定工况的适用性
 *
 * @param {object} p
 * @param {number} [p.powerKw]     功率 kW
 * @param {number} [p.speedRpm]    转速 rpm
 * @param {number} [p.voltageV]    线电压 V
 * @param {number} [p.poles]       极数
 * @param {string} [p.motorType]   'induction' | 'pmsm' | 缺省
 * @param {number} [p.freqHz]      电频率 Hz（不给则由 n·p/120 推）
 * @returns {{
 *   verdict: 'ok'|'caution'|'reject',
 *   reasons: Array<{code:string, level:string, msg:string, measured:number, limit:number}>,
 *   advice: string|null,
 *   routing: 'l0'|'l0_caution'|'l1',
 *   metrics: object
 * }}
 */
export function assessApplicability(p = {}) {
  const {
    powerKw = null, speedRpm = null, voltageV = null, poles = null,
    motorType = null, freqHz = null,
  } = p

  const reasons = []
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)

  const n = num(speedRpm)
  const v = num(voltageV)
  const po = num(poles)
  const f = num(freqHz) ?? (n !== null && po !== null ? (n * po) / 120 : null)

  // ---- 1. 电频率 ----
  if (f !== null) {
    if (f > APPLICABILITY.FREQ_CALIBRATED_MAX) {
      reasons.push({
        code: 'FREQ_OUT_OF_CALIBRATION', level: 'reject',
        msg: `电频率 ${round1(f)}Hz 超出 L0 标定域（≤${APPLICABILITY.FREQ_CALIBRATED_MAX}Hz）：`
          + `L0 的铁损/集肤/变频器附加损耗均无模型，高频下温升与效率输出不可信`,
        measured: round1(f), limit: APPLICABILITY.FREQ_CALIBRATED_MAX,
      })
    } else if (f > APPLICABILITY.FREQ_CAUTION) {
      reasons.push({
        code: 'FREQ_NEAR_LIMIT', level: 'caution',
        msg: `电频率 ${round1(f)}Hz 已进入 L0 标定域边缘（${APPLICABILITY.FREQ_CAUTION}~${APPLICABILITY.FREQ_CALIBRATED_MAX}Hz）：`
          + `效率与温升仅供横向比较，不可作定量结论`,
        measured: round1(f), limit: APPLICABILITY.FREQ_CALIBRATED_MAX,
      })
    }
  }

  // ---- 2. 电压等级 ----
  if (v !== null) {
    if (v > APPLICABILITY.VOLTAGE_CALIBRATED_MAX) {
      reasons.push({
        code: 'VOLTAGE_OUT_OF_CALIBRATION', level: 'reject',
        msg: `电压 ${round1(v)}V 超出 L0 标定域（≤${APPLICABILITY.VOLTAGE_CALIBRATED_MAX}V）：`
          + `L0 的气隙磁密基准与绕组匝数闭环按中压工况标定，高压下每线圈理想匝数趋 0`,
        measured: round1(v), limit: APPLICABILITY.VOLTAGE_CALIBRATED_MAX,
      })
    } else if (v > APPLICABILITY.VOLTAGE_CAUTION) {
      reasons.push({
        code: 'VOLTAGE_NEAR_LIMIT', level: 'caution',
        msg: `电压 ${round1(v)}V 已进入 L0 标定域边缘（${APPLICABILITY.VOLTAGE_CAUTION}~${APPLICABILITY.VOLTAGE_CALIBRATED_MAX}V）`,
        measured: round1(v), limit: APPLICABILITY.VOLTAGE_CALIBRATED_MAX,
      })
    }
  }

  // ---- 3. 转速（机械约束，L0 无模型） ----
  if (n !== null) {
    if (n > APPLICABILITY.SPEED_CALIBRATED_MAX) {
      reasons.push({
        code: 'SPEED_OUT_OF_CALIBRATION', level: 'reject',
        msg: `转速 ${round1(n)}rpm 超出 L0 标定域（≤${APPLICABILITY.SPEED_CALIBRATED_MAX}rpm）：`
          + `L0 无转子强度/挠度/轴承寿命校核，高速工况的机械可行性完全未覆盖`,
        measured: round1(n), limit: APPLICABILITY.SPEED_CALIBRATED_MAX,
      })
    } else if (n > APPLICABILITY.SPEED_CAUTION) {
      reasons.push({
        code: 'SPEED_NEAR_LIMIT', level: 'caution',
        msg: `转速 ${round1(n)}rpm 已进入 L0 标定域边缘（${APPLICABILITY.SPEED_CAUTION}~${APPLICABILITY.SPEED_CALIBRATED_MAX}rpm）：`
          + `机械强度未校核，方案仅供电磁侧初筛`,
        measured: round1(n), limit: APPLICABILITY.SPEED_CALIBRATED_MAX,
      })
    }
  }

  // ---- 4. 机种（PMSM 无电磁模型） ----
  const mt = typeof motorType === 'string' ? motorType.toLowerCase() : null
  if (mt === 'pmsm' || mt === 'pm' || mt === 'pms') {
    reasons.push({
      code: 'PMSM_NO_EM_MODEL', level: 'caution',
      msg: '永磁同步机：L0 按异步机标定，未建模 dq 电流/弱磁/退磁/转矩脉动；'
        + 'PM 励磁由几何种子(极弧0.688/PM厚12mm)粗略假设，反电势与转矩均非 PMSM 真实值',
      measured: 0, limit: 0,
    })
  }

  // ---- 汇总 ----
  const hasReject = reasons.some((r) => r.level === 'reject')
  const hasCaution = reasons.some((r) => r.level === 'caution')
  const verdict = hasReject ? 'reject' : (hasCaution ? 'caution' : 'ok')

  return {
    verdict,
    reasons,
    advice: verdict === 'ok' ? null : buildAdvice({ verdict, reasons, p: { powerKw, speedRpm, voltageV, poles } }),
    routing: verdict === 'reject' ? 'l1' : (verdict === 'caution' ? 'l0_caution' : 'l0'),
    metrics: {
      freq_hz: f !== null ? round1(f) : null,
      voltage_v: v !== null ? round1(v) : null,
      speed_rpm: n !== null ? round1(n) : null,
      poles: po !== null ? round1(po) : null,
      power_kw: powerKw !== null ? round1(powerKw) : null,
      reject_count: reasons.filter((r) => r.level === 'reject').length,
      caution_count: reasons.filter((r) => r.level === 'caution').length,
    },
  }
}

/**
 * 依据判定结果生成可执行的路由建议
 * 说明：建议必须给出「改什么、改成多少」或「去哪一级」，
 *       只说「不适用」等于把问题丢回给调用方。
 */
function buildAdvice({ verdict, reasons, p }) {
  const codes = new Set(reasons.map((r) => r.code))
  const lines = []

  if (verdict === 'reject') {
    lines.push('【L0 不适用该工况】以下是可行的推进路径：')

    // 电压维度：给出保持功率与转速不变时的可行电压建议
    if (codes.has('VOLTAGE_OUT_OF_CALIBRATION') && p.powerKw && p.speedRpm) {
      lines.push(`  · 升压至 ≤${APPLICABILITY.VOLTAGE_CALIBRATED_MAX}V 可回到 L0 标定域`
        + `（380V 升 690V 是矿用/牵引常用档位；升压后每线圈匝数上升，绕组才有整数解）`)
    }
    if (codes.has('SPEED_OUT_OF_CALIBRATION')) {
      lines.push(`  · 降速至 ≤${APPLICABILITY.SPEED_CALIBRATED_MAX}rpm 可回到 L0 标定域`
        + `（低速大功率反而是 L0 的强项：250~1000rpm 大功率工况实测可行）`)
    }
    if (codes.has('FREQ_OUT_OF_CALIBRATION')) {
      lines.push(`  · 增加极数可降频（f = n·p/120）：当前 ${p.poles ?? '?'} 极，`
        + `降到 ≤${APPLICABILITY.FREQ_CALIBRATED_MAX}Hz 需要的极数约 `
        + `${Math.ceil((APPLICABILITY.FREQ_CALIBRATED_MAX * 120) / (p.speedRpm || 1))} 极`
        + `${p.poles && p.poles % 2 === 0 ? '（须为偶数）' : ''}`)
    }

    lines.push('  · 或直接进 L1（RMxprt 电磁求解，1~3s/例）或 L2（Motor-CAD 精验）——'
      + '高速/高压/大功率工况本就必须由 FEM 求解定案，L0 在此只提供不了额外信息')
  } else {
    lines.push('【L0 可用但结果仅供横向比较】')
    for (const r of reasons.filter((x) => x.level === 'caution')) {
      lines.push(`  · ${r.msg}`)
    }
    lines.push('  · 建议：把 L0 输出当作「候选排序」而非「设计定案」，'
      + '最终结论必须由 L1/L2 电磁仿真给出')
  }

  return lines.join('\n')
}

/**
 * 从参数矩阵首行提取工况特征，用于能力门判定
 * 矩阵行内电压/转速/极数字段名在不同入口可能不同名，这里做容错聚合。
 */
export function scenarioFromMatrix(paramsList = []) {
  const first = paramsList.find((r) => r && typeof r === 'object') ?? {}
  const pick = (...keys) => {
    for (const k of keys) {
      if (typeof first[k] === 'number' && Number.isFinite(first[k])) return first[k]
      if (k === 'motor_type' && typeof first[k] === 'string') return first[k]
    }
    return null
  }
  return {
    powerKw: pick('power_kw'),
    speedRpm: pick('speed'),
    voltageV: pick('voltage'),
    poles: pick('poles'),
    motorType: pick('motor_type'),
  }
}

function round1(v) {
  return typeof v === 'number' && Number.isFinite(v) ? Math.round(v * 10) / 10 : null
}

export default { assessApplicability, scenarioFromMatrix, APPLICABILITY }