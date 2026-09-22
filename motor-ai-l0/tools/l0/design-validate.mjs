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
  from '../../lib/design-rules.mjs'
import { normalizeSpec } from '../../lib/param-schema.mjs'
import { getUsageSink, logUsage } from '../../lib/usage-log.mjs'

/** 工具入参声明 */
export const TOOL_PARAMS = {
  params: { type: 'object', additionalProperties: true, description: '单个设计参数对象（与 params_list 二选一）' },
  params_list: { type: 'array', description: '参数组合列表（与 params 二选一）' },
  escalate: {
    type: 'array',
    description: `提升为 failed 的规则 id，可选: ${ESCALATABLE_RULES.join('/')}`,
  },
  insulation_class: { type: 'string', description: '绝缘等级 B/F/H，默认 F' },
  air_gap_flux: { type: 'number', description: '气隙磁密基准 T，默认 0.80' },
  include_thermal: { type: 'boolean', description: '是否执行温升校验，默认 true' },
  include_reports: { type: 'boolean', description: '批量模式是否回传逐条报告，默认 false' },
}

/**
 * 物理一致性校验（纯函数，零依赖，可单测）
 * @param {object} rawArgs 工具入参
 * @param {object} [config] 插件配置：{ efficiencyCap, tempRiseRange }
 */
export function runDesignValidate(rawArgs, config = {}) {
  const { spec: args } = normalizeSpec(rawArgs ?? {})

  // 优先级：工具入参 > 插件配置 > 常量默认
  const opts = {
    escalate: Array.isArray(args.escalate) ? args.escalate : undefined,
    insulationClass: args.insulation_class ?? args.insulationClass ?? config.insulationClass,
    airGapFlux: typeof args.air_gap_flux === 'number'
      ? args.air_gap_flux
      : (typeof config.airGapFluxT === 'number' ? config.airGapFluxT : undefined),
    includeThermal: args.include_thermal === false ? false : true,
    efficiencyCap: config.efficiencyCap,
    maxTempClamp: config.tempRiseRange,
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
      '电频率、转子轭厚、温升限值共 12 组规则。\n' +
      '典型用法：流水线第二步——先对参数矩阵批量校验，把 failed 项剔除后\n' +
      '再交给 motor_l0_estimate 估算排序。',
    parameters: TOOL_PARAMS,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args) {
      const t0 = Date.now()
      try {
        const result = runDesignValidate(args, config)
        logUsage(getUsageSink(config), {
          tool: 'motor_design_validate', ok: true, elapsed_ms: Date.now() - t0,
          n: args?.params_list?.length ?? (args?.params ? 1 : 0),
          failed: result?.summary?.failed, warning: result?.summary?.warning,
        })
        return JSON.stringify(result, null, 2)
      } catch (err) {
        logUsage(getUsageSink(config), {
          tool: 'motor_design_validate', ok: false, elapsed_ms: Date.now() - t0,
          error: String(err?.message ?? err),
        })
        return JSON.stringify({ error: true, message: String(err?.message ?? err) }, null, 2)
      }
    },
  }))
}

export default { runDesignValidate, registerDesignValidate, TOOL_PARAMS }
