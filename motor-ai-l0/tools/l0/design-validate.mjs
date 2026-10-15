/**
 * 工具 3：motor_design_validate —— 物理一致性校验（W4）
 * ===========================================================
 * 定位：L0 广筛的「剔假」环节。放在估算之前还是之后？
 *   本仓库选择 **估算之前先粗筛**（V01/V02/V06/V09/V11 这类硬几何规则不需要损耗），
 *   温升类规则（V12）需要功率与冷却信息，在 estimate 之后二次校验同样有效。
 *   故本工具同时支持两种用法：
 *     (a) params_list 批量校验 → 拿 passed 集合喂 motor_l0_estimate
 *     (b) 单个 params 深查 → 拿 metrics 做人工复核
 *
 * 三级语义（下游必须遵守）：
 *   failed  —— 物理不成立，**必须剔除**，不得进入 TopN
 *   warning —— 可保留但需知情（如分数槽、高频、磁密偏离）
 *   passed  —— 全部规则通过
 *
 * 本文件保持「纯函数 + 薄封装」：
 *   runDesignValidate() 零依赖可单测；registerDesignValidate() 才触碰 DSH Runtime。
 */

import { validateDesign, validateDesignBatch, RULE_CATALOG, ESCALATABLE_RULES }
  from '@wangxue98/dsh-motor-ai-core'
import { normalizeSpec } from '@wangxue98/dsh-motor-ai-core'
import { DEFAULT_LINE_FREQ_HZ } from '@wangxue98/dsh-motor-ai-core'
import { quickL0Estimate, solveTurnsPerCoil } from '@wangxue98/dsh-motor-ai-core'
import { getUsageSink, logUsage } from '@wangxue98/dsh-motor-ai-core'
import { assertFreeQuota, QuotaExceededError } from '../../lib/quota.mjs'

/** 工具入参声明 */
export const TOOL_PARAMS = {
  params: { type: 'object', additionalProperties: true, description: '单个设计参数对象（与 params_list 二选一）' },
  params_list: { type: 'array', description: '参数组合列表（与 params 二选一）' },
  escalate: {
    type: 'array',
    description: `提升为 failed 的规则 id，可选: ${ESCALATABLE_RULES.join('/')}`,
  },
  insulation_class: { type: 'string', description: '绝缘等级 B/F/H，默认 F' },
  line_freq_hz: {
    type: 'number',
    description: `电源频率 Hz，默认 ${DEFAULT_LINE_FREQ_HZ}。60Hz 电网与 VFD 变频工况必须显式传入，`
      + '否则 V16 会按 50Hz 判超同步（4 极机在 1500rpm 处形成假断崖）',
  },
  air_gap_flux: { type: 'number', description: '气隙磁密基准 T，默认 0.80' },
  bg_caliber: {
    type: 'string',
    enum: ['actual', 'intent'],
    description: 'v0.2.5：磁密判据口径。actual=按实际整数匝数反解的气隙磁密判定（默认，'
      + '铁芯真正承受的磁密）；intent=按设计意图 0.80T 判定（v0.2.4 旧口径，便于历史对比）。'
      + '显式传 air_gap_flux 时以该值为准。',
  },
  include_thermal: { type: 'boolean', description: '是否执行温升校验，默认 true' },
  include_reports: { type: 'boolean', description: '批量模式是否回传逐条报告，默认 false' },
  // ---- v0.5.4 P1 杠杆三原语（与 params/params_list 互斥，优先级最高）----
  sweep_param: {
    type: 'string',
    description: 'v0.5.4 P1-3 单变量扫描：待扫描的参数名（如 yoke_thickness/turns_per_coil/tooth_width）。'
      + '与 sweep_values/base_params 搭配使用，对每个扫描值做同口径「校验+估算」，输出可行域与效率曲线',
  },
  sweep_values: {
    type: 'array',
    description: 'v0.5.4 P1-3 单变量扫描：扫描值列表（最多 50 个）。每个值 = base_params 覆盖该参数后的整行',
  },
  base_params: {
    type: 'object', additionalProperties: true,
    description: 'v0.5.4 P1-3 单变量扫描的基线参数（完整 L1 行，须含 L1 严格必填四字段）',
  },
  solve_turns: {
    type: 'object', additionalProperties: true,
    description: 'v0.5.4 P1-4 绕组匝数反解：传入参数对象（至少 voltage/speed/poles/stator_id/core_length/slots_stator），'
      + '输出电气闭环理想匝、可取整域（反电势偏差容差内的整数匝）及每个候选匝的整行校验结论与估算指标',
  },
  leverage_audit: {
    type: 'object', additionalProperties: true,
    description: 'v0.5.4 P1-5 杠杆审计：传入单对象参数，输出「哪里坏（issue+margin）、差多少（margin 数值）、往哪拧」'
      + '——V07 齿宽/V08 轭厚/V15 匝数给精确反解（headroom），其余规则按 margin 与文案判读',
  },
}

/**
 * v0.5.4 P1 杠杆三原语（sweep / solve_turns / leverage_audit）—— 设计杠杆验证的引擎侧
 * ---------------------------------------------------------------------------
 * 断链根因（v0.5.3 前的核心痛点）：
 *   「规则报了 warning/failed」到「参数该改成多少」之间没有桥——margin/headroom
 *   数据 core 侧已产出，但工具层没有暴露给调用方，杠杆验证只能靠人工换算。
 * 三个原语把链条闭合：
 *   sweep         —— 单变量扫描，回答「这个杠杆拧多少格最划算」；
 *   solve_turns   —— 匝数自洽反解，回答「匝数该取几、每个候选匝长什么样」；
 *   leverage_audit—— 单行全量审计，回答「哪里坏、差多少、往哪拧」。
 * 共同约束：同口径探针（校验+估算各一次）、失败侧为 null 不伪造、
 *           温升一律带 proxy 标注（历史 pearson≈0.047，仅作量级判断）。
 */

/** 单行「校验+估算」探针：任一失败该侧为 null（宁缺毋滥） */
function probeRow(row, opts, config = {}) {
  const rep = (() => { try { return validateDesign(row, opts) } catch { return null } })()
  const est = (() => {
    try {
      return quickL0Estimate(row, {
        efficiencyCap: config.efficiencyCap ?? 96,
        maxTempClamp: config.tempRiseRange ?? [45, 130],
        ...(row?.insulation_class ? { insulationClass: String(row.insulation_class).toUpperCase() } : {}),
        ...(Number.isFinite(Number(row?.line_freq_hz)) ? { lineFreqHz: Number(row.line_freq_hz) } : {}),
      })
    } catch { return null }
  })()
  return { rep, est }
}

/** 从估算+校验结果挑对比指标（缺侧给 null，不伪造 0） */
function pickMetrics(est, rep) {
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
  return {
    status: rep?.status ?? null,
    failed_count: (rep?.issues ?? []).filter((i) => i.level === 'failed').length,
    warning_count: (rep?.issues ?? []).filter((i) => i.level === 'warning').length,
    efficiency_raw: num(est?.efficiency_raw) ?? num(est?.efficiency),
    temp_rise: num(est?.temp_rise),
    tooth_flux_density: num(est?.tooth_flux_density),
    yoke_flux_density: num(est?.yoke_flux_density),
    slot_fill_ratio: num(est?.slot_fill_ratio),
    torque_density: num(est?.torque_density),
    feasible: est?.feasible ?? null,
  }
}

/**
 * P1-3：单变量扫描——「这个杠杆拧多少格最划算」的直接证据
 * @returns {{mode:'sweep', param:string, swept:number, rows:object[], best:object|null, note:string}}
 */
export function runSweep(param, values, base, opts, config = {}) {
  if (!param || typeof param !== 'string') {
    throw new Error('[motor_design_validate] sweep 模式需提供 sweep_param（待扫描参数名）')
  }
  if (!Array.isArray(values) || values.length === 0) {
    throw new Error('[motor_design_validate] sweep 模式需提供 sweep_values（非空数组）')
  }
  if (!base || typeof base !== 'object' || Array.isArray(base)) {
    throw new Error('[motor_design_validate] sweep 模式需提供 base_params（基线参数对象）')
  }
  const rows = []
  for (const v of values.slice(0, 50)) {
    const row = { ...base, [param]: v }
    const { rep, est } = probeRow(row, opts, config)
    rows.push({ [param]: v, ...pickMetrics(est, rep) })
  }
  // best：无 failed 里效率真值最高者（temp_rise 是 proxy，只展示不参与选择）
  const candidates = rows.filter((r) => r.status === 'passed' || r.status === 'warning')
  let best = null
  for (const r of candidates) {
    if (r.efficiency_raw == null) continue
    if (!best || r.efficiency_raw > best.efficiency_raw) best = r
  }
  return {
    mode: 'sweep',
    param,
    swept: rows.length,
    rows,
    best: best
      ? { ...best, note: `扫描域内 ${param}=${best[param]} 时效率真值最高且无 failed` }
      : null,
    temp_rise_is_proxy: true,
    temp_rise_note: 'temp_rise 为热负荷法代理值，杠杆的温升收益须过 L1/L2 热模型才算数',
    note: '扫描按同口径逐点「校验+估算」；best 只在扫描域内比较，不代表全域最优。'
      + '建议把 best.value 与 leverage_audit 的 headroom 对照，确认还在安全余量内。',
  }
}

/**
 * P1-4：绕组匝数自洽反解——「匝数该取几」的权威答案
 * 与 core solveTurnsPerCoil 同源（E=4.44·f·N·Φ·kw），不依赖功率/电流，缺 peak_current 也能反解。
 * 每个可取整域成员补整行校验+估算（匝数联动槽满率 V15 与磁密 V07/V08）。
 */
export function runSolveTurns(params, opts, config = {}) {
  if (!params || typeof params !== 'object' || Array.isArray(params)) {
    throw new Error('[motor_design_validate] solve_turns 模式需提供对象入参'
      + '（至少 voltage/speed/poles/stator_id/core_length/slots_stator）')
  }
  const sol = solveTurnsPerCoil({
    voltage: params.voltage, speed: params.speed, poles: params.poles,
    statorId: params.stator_id, coreLength: params.core_length,
    slotsStator: params.slots_stator,
    parallelCircuits: params.parallel_circuits ?? 1,
    ...(typeof opts.airGapFlux === 'number' ? { airGapFlux: opts.airGapFlux } : {}),
    ...(params.connection ? { connection: params.connection } : {}),
  })
  if (!sol) {
    throw new Error('[motor_design_validate] solve_turns：参数不足或非法'
      + '（voltage/speed/poles/stator_id/core_length/slots_stator 须均为正数）')
  }
  const options = (sol.integer_options ?? []).slice(0, 7).map((o) => {
    const row = { ...params, turns_per_coil: o.turns }
    const { rep, est } = probeRow(row, opts, config)
    return { ...o, ...pickMetrics(est, rep) }
  })
  return {
    mode: 'solve_turns',
    electrical: {
      f_hz: sol.f_hz, uph_v: sol.uph_v, phi_wb: sol.phi_wb,
      n_phase_ideal: sol.n_phase_ideal,
      turns_ideal: sol.turns_ideal, turns_int: sol.turns_int,
      quant_dev: sol.quant_dev,
      e_ideal_v: sol.e_ideal_v, e_int_v: sol.e_int_v, dev_int: sol.dev_int,
    },
    integer_options: options,
    recommendation: sol.turns_int,
    note: 'turns_ideal 为电气闭环理想匝；integer_options 是反电势偏差在容差内的可取整域，'
      + '每项已带整行校验结论（status）与估算指标。匝数同时影响槽满率（V15）与磁密（V07/V08），'
      + '取整后务必看 options 内的 status，不要只看反电势偏差。',
  }
}

/**
 * P1-5：杠杆审计——一份报告说清「哪里坏、差多少、往哪拧」
 * issues 带 margin（core 1.3.0 起每条规则附结构化裕度）；V07/V08/V15 另有 headroom 精确反解。
 */
export function runLeverageAudit(params, opts) {
  if (!params || typeof params !== 'object' || Array.isArray(params)) {
    throw new Error('[motor_design_validate] leverage_audit 模式需提供单对象入参')
  }
  const rep = validateDesign(params, opts)
  const issues = (rep.issues ?? []).map((i) => ({
    rule: i.rule, level: i.level, message: i.message,
    margin: i.margin ?? null, is_proxy: i.proxy === true,
  }))
  const headroom = rep.metrics?.leverage_headroom ?? {}
  // 可执行杠杆：有精确反解的问题规则（拧到多少有唯一答案）
  const actionable = Object.entries(headroom).map(([rule, h]) => ({
    rule, field: h.field,
    current: h.current_mm ?? h.current,
    adjust_to: h.min_for_target_mm ?? h.min_for_saturation_mm ?? h.max_for_slot_fill,
    adjustable: h.adjustable_mm ?? h.adjustable,
    note: h.note,
  }))
  return {
    mode: 'leverage_audit',
    status: rep.status,
    checked: rep.checked,
    issues,
    leverage_headroom: headroom,
    actionable,
    flux: rep.metrics?.flux ?? {},
    thermal: rep.metrics?.thermal ?? {},
    temp_rise_is_proxy: rep.metrics?.thermal?.is_proxy ?? true,
    summary: actionable.length
      ? `${actionable.length} 条规则给出精确反解（V07 齿宽/V08 轭厚/V15 匝数），其余按 margin 与 message 判读`
      : '本次无 headroom 级反解（问题规则不在 V07/V08/V15，或设计本身已通过）',
  }
}

/**
 * 物理一致性校验（纯函数，零依赖，可单测）
 * @param {object} rawArgs 工具入参
 * @param {object} [config] 插件配置：{ efficiencyCap, tempRiseRange }
 */
export function runDesignValidate(rawArgs, config = {}) {
  if (!config || typeof config !== 'object') config = {}
  // v0.5.1 P0-门1：纯函数入口过闸（详见 lib/quota.mjs assertFreeQuota 注释）
  assertFreeQuota('design-validate', config)
  const { spec: args } = normalizeSpec(rawArgs ?? {})

  // 优先级：工具入参 > 插件配置 > 常量默认
  const opts = {
    escalate: Array.isArray(args.escalate) ? args.escalate : undefined,
    insulationClass: args.insulation_class ?? args.insulationClass ?? config.insulationClass,
    // v0.2.5：电源频率入参透传（V16/V18 判据的自变量）
    lineFreqHz: typeof args.line_freq_hz === 'number' ? args.line_freq_hz
      : (typeof config.line_freq_hz === 'number' ? config.line_freq_hz : undefined),
    airGapFlux: typeof args.air_gap_flux === 'number'
      ? args.air_gap_flux
      : (typeof config.airGapFluxT === 'number' ? config.airGapFluxT : undefined),
    bgCaliber: args.bg_caliber === 'intent' ? 'intent'
      : (args.bg_caliber === 'actual' ? 'actual' : (config.bgCaliber ?? 'actual')),
    includeThermal: args.include_thermal === false ? false : true,
    efficiencyCap: config.efficiencyCap,
    maxTempClamp: config.tempRiseRange,
  }

  // ---- v0.5.4 P1 杠杆三原语（互斥模式，优先级高于 single/batch；一次调用=一次额度）----
  if (args.sweep_param != null || Array.isArray(args.sweep_values)) {
    return runSweep(args.sweep_param, args.sweep_values, args.base_params, opts, config)
  }
  if (args.solve_turns && typeof args.solve_turns === 'object' && !Array.isArray(args.solve_turns)) {
    return runSolveTurns(args.solve_turns, opts, config)
  }
  if (args.leverage_audit && typeof args.leverage_audit === 'object' && !Array.isArray(args.leverage_audit)) {
    return runLeverageAudit(args.leverage_audit, opts)
  }

  // ---- 单对象模式 ----
  if (args.params && typeof args.params === 'object') {
    const report = validateDesign(args.params, opts)
    return {
      mode: 'single',
      ...report,
      rule_count: RULE_CATALOG.length,
    }
  }

  // ---- 批量模式 ----
  const list = Array.isArray(args.params_list) ? args.params_list : []
  if (list.length === 0) {
    throw new Error('[motor_design_validate] 必须提供 params 或 params_list')
  }

  const started = Date.now()
  const batch = validateDesignBatch(list, opts)
  const includeReports = args.include_reports === true

  // 规则命中统计：哪条规则剔得最多 → 直接指出参数矩阵的生成偏差
  const ruleHits = {}
  for (const r of batch.reports) {
    for (const issue of r.issues ?? []) {
      const key = `${issue.rule}:${issue.level}`
      ruleHits[key] = (ruleHits[key] ?? 0) + 1
    }
  }
  const topRules = Object.entries(ruleHits)
    .sort((a, b) => b[1] - a[1])
    .map(([rule, count]) => ({ rule, count }))

  return {
    mode: 'batch',
    summary: batch.summary,
    rule_hits: topRules,
    passed_indices: batch.passed.map((x) => x.index),
    warning_indices: batch.warning.map((x) => x.index),
    failed_indices: batch.failed.map((x) => x.index),
    failed_reasons: batch.failed.map((x) => ({
      index: x.index,
      issues: x.report.issues.filter((i) => i.level === 'failed'),
    })),
    reports: includeReports ? batch.reports : undefined,
    rule_count: RULE_CATALOG.length,
    elapsed_ms: Date.now() - started,
  }
}

/**
 * 工具注册（薄封装，动态 import 保证离线可加载）
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {object} config
 */
export async function registerDesignValidate(ctx, config = {}) {
  const { defineTool } = await import('@deepseek-ai/dsh-tools')

  ctx.tools.register(defineTool({
    name: 'motor_design_validate',
    description:
      '校验电机设计参数的物理一致性，返回 passed / warning / failed 三级结论。\n' +
      '输入 params_list 应来自 motor_param_matrix 的产出（每项含 16 个 L1 顶层字段）。\n' +
      '覆盖几何链、气隙、内外径比、长径比、极槽配合、并联支路、齿轭磁密、槽形、\n' +
      '电频率、转子轭厚、温升限值、匝数闭环、槽满率、极数-转速同步一致性共 15 组规则' +
      '（V01–V12 + V14 反电势闭环 + V15 槽满率 + V16 极数-转速同步一致性，V13 附于 V06）。\n' +
      '典型用法：流水线第二步——先对参数矩阵批量校验，把 failed 项剔除后\n' +
      '再交给 motor_l0_estimate 估算排序。\n' +
      'v0.5.4 杠杆三原语（与 params/params_list 互斥，一次调用=一次额度）：\n' +
      '① sweep_param+sweep_values+base_params 单变量扫描——「这个杠杆拧多少格最划算」；\n' +
      '② solve_turns 绕组匝数反解——「匝数该取几」及每个候选匝的整行校验+估算；\n' +
      '③ leverage_audit 杠杆审计——「哪里坏、差多少、往哪拧」（V07 齿宽/V08 轭厚/V15 匝数给精确反解）。\n' +
      '⚠️ 广筛时本工具批量报告也很大，建议直接用聚合工具 motor_l0_pipeline（内部串三步链，\n' +
      '只回紧凑摘要 + 计数自检，绕开 DSH 2000 行截断）；单独调本工具仅用于调试某条规则。',
    parameters: TOOL_PARAMS,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args) {
      const t0 = Date.now()
      try {
        // v0.5.1：闸门内聚到 runDesignValidate 纯函数入口（P0-门1），execute 不再
        // 单独 enforceQuota（await 边界会放跑消费标记造成双重扣次）。
        const result = runDesignValidate(args, config)
        logUsage(getUsageSink(config), {
          tool: 'motor_design_validate', ok: true, elapsed_ms: Date.now() - t0,
          n: args?.params_list?.length ?? (args?.params ? 1 : 0),
          failed: result?.summary?.failed, warning: result?.summary?.warning,
        })
        return JSON.stringify(result, null, 2)
      } catch (err) {
        if (err instanceof QuotaExceededError) {
          logUsage(getUsageSink(config), {
            tool: 'motor_design_validate', ok: false, elapsed_ms: Date.now() - t0,
            error: 'quota-exhausted',
          })
          return err.message
        }
        logUsage(getUsageSink(config), {
          tool: 'motor_design_validate', ok: false, elapsed_ms: Date.now() - t0,
          error: String(err?.message ?? err),
        })
        return JSON.stringify({ error: true, message: String(err?.message ?? err) }, null, 2)
      }
    },
  }))
}

export default {
  runDesignValidate, registerDesignValidate, TOOL_PARAMS,
  runSweep, runSolveTurns, runLeverageAudit,
}
