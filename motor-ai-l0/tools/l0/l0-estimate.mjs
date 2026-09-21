/**
 * 工具 2：motor_l0_estimate —— L0 毫秒级估算与排序
 * ===========================================================
 * 输入：buildParamMatrix 产出的参数矩阵（或直接手写的同形状列表）
 * 输出：每项 = L0 原生 6 字段 + L1 镜像字段，并挂载 TopN 交接载荷
 *
 * 排序口径（跨层一致性关键点）：
 *   L0 与 L1 必须能放在一起比。若 L0 封顶 98.5% 而 L1 封顶 96%，
 *   同一批方案在两层的排序会跳变 —— 故效率统一 clamp 到 config.efficiencyCap（默认 96，
 *   对齐 motor_tools.py:752）。
 *
 * 本文件同样保持「纯函数 + 薄封装」结构：
 *   runL0Estimate() 零依赖可单测；registerL0Estimate() 才触碰 DSH Runtime。
 */

import {
  normalizeSpec, assertMatrixShape, buildHandoff, L0_NATIVE_FIELDS, L1_MIRROR_FIELDS,
} from '../../lib/param-schema.mjs'
import { quickL0Estimate } from '../../lib/formula-engine.mjs'
import {
  loadSurrogateModel,
  predictBatch,
  buildSurrogateResult,
  DEFAULT_CONFIDENCE_THRESHOLD,
} from '../../lib/surrogate-engine.mjs'

/** 工具入参声明 */
export const TOOL_PARAMS = {
  params_list: {
    type: 'array', required: true,
    description: '参数组合列表，每项须含 stator_od/poles/voltage/speed 四个 L1 严格必填字段',
  },
  top_n: { type: 'number', required: false, description: '返回前 N 个结果，默认全部' },
  sort_by: {
    type: 'string', required: false,
    description: '排序字段：efficiency / torque_density / temp_rise，默认 efficiency',
  },
}

/** 允许参与排序的字段白名单（防注入 sort 到任意键） */
export const SORTABLE_FIELDS = ['efficiency', 'torque_density', 'temp_rise', 'total_loss', 'power']

/** temp_rise 为「越小越好」，其余为「越大越好」 */
const ASCENDING_FIELDS = new Set(['temp_rise', 'total_loss'])

/**
 * L0 批量估算（纯函数，零依赖，可单测）
 * @param {object} rawArgs
 * @param {object} [config] 插件配置：{ efficiencyCap, tempRiseRange, topNPreview, l0Mode, surrogatePath, surrogateConfidenceThreshold }
 */
export function runL0Estimate(rawArgs, config = {}) {
  const { spec: args } = normalizeSpec(rawArgs)
  const paramsList = Array.isArray(args.params_list) ? args.params_list : []
  const sortBy = SORTABLE_FIELDS.includes(args.sort_by) ? args.sort_by : 'efficiency'
  const topN = Number(args.top_n) > 0 ? Number(args.top_n) : null

  const efficiencyCap = config.efficiencyCap ?? 96
  const maxTempClamp = config.tempRiseRange ?? [45, 130]
  
  // 灰度模式：默认公式通道，仅显式启用 surrogate 时才使用代理模型
  const l0Mode = config.l0Mode ?? 'formula'
  const surrogateThreshold = config.surrogateConfidenceThreshold ?? DEFAULT_CONFIDENCE_THRESHOLD

  const started = Date.now()
  const failures = []
  const results = []
  let surrogateFallbacks = 0

  // ---- 代理模型通道（需显式启用 l0Mode='surrogate'）----
  if (l0Mode === 'surrogate') {
    try {
      const modelPath = config.surrogatePath ?? 'models/l0_surrogate.json'
      const model = loadSurrogateModel(modelPath)

      const { results: predictions, fallbacks } = predictBatch(
        paramsList,
        model,
        surrogateThreshold
      )
      surrogateFallbacks = fallbacks

      for (let i = 0; i < paramsList.length; i++) {
        const row = paramsList[i]
        const shape = assertMatrixShape(row ?? {})
        if (!shape.ok) {
          failures.push({
            index: i,
            missing: shape.missing,
            typeErrors: shape.typeErrors,
          })
          continue
        }

        const pred = predictions[i]
        if (pred.fallback_to_formula || pred.error) {
          // 置信度不足或预测失败，降级公式通道
          results.push(quickL0Estimate(row, { efficiencyCap, maxTempClamp }))
        } else {
          // 代理模型预测成功
          results.push(buildSurrogateResult(row, pred))
        }
      }

      if (results.length === paramsList.length) {
        const elapsed = Date.now() - started
        const ascending = ASCENDING_FIELDS.has(sortBy)
        results.sort((a, b) => (ascending ? a[sortBy] - b[sortBy] : b[sortBy] - a[sortBy]))

        const effectiveTopN = topN ?? results.length
        const topRows = results.slice(0, effectiveTopN)

        const handoff = buildHandoff(topRows, {
          fromLevel: 'l0',
          toLevel: 'l1',
          ranking: ['efficiency', 'torque_density'],
        })

        return {
          results: topRows,
          handoff,
          total: paramsList.length,
          success: results.length,
          failed: failures.length,
          failures: failures.length ? failures : undefined,
          returned: topRows.length,
          sorted_by: sortBy,
          sort_order: ascending ? 'asc' : 'desc',
          l0_mode: 'surrogate',
          surrogate_fallbacks: surrogateFallbacks,
          surrogate_model_version: model.version,
          fields: { native: L0_NATIVE_FIELDS, mirror: L1_MIRROR_FIELDS },
          elapsed_ms: elapsed,
          avg_ms_per_case: paramsList.length ? Math.round((elapsed / paramsList.length) * 100) / 100 : 0,
        }
      }
    } catch (err) {
      // 代理模型加载/预测失败，降级公式通道
      console.warn('[L0] surrogate 通道异常，降级公式:', err.message)
    }
  }
  
  // ---- 公式通道（默认/降级）----
  for (let i = 0; i < paramsList.length; i += 1) {
    const row = paramsList[i]
    const shape = assertMatrixShape(row ?? {})
    if (!shape.ok) {
      failures.push({
        index: i,
        missing: shape.missing,
        typeErrors: shape.typeErrors,
      })
      continue
    }
    try {
      results.push(quickL0Estimate(row, { efficiencyCap, maxTempClamp }))
    } catch (err) {
      failures.push({ index: i, error: String(err?.message ?? err) })
    }
  }

  const ascending = ASCENDING_FIELDS.has(sortBy)
  results.sort((a, b) => (ascending ? a[sortBy] - b[sortBy] : b[sortBy] - a[sortBy]))

  const effectiveTopN = topN ?? results.length
  const topRows = results.slice(0, effectiveTopN)

  // TopN 交接载荷 —— v3 §10.1：L0 → L1
  const handoff = buildHandoff(topRows, {
    fromLevel: 'l0',
    toLevel: 'l1',
    ranking: ['efficiency', 'torque_density'],
  })

  const elapsed = Date.now() - started

  return {
    results: topRows,
    handoff,
    total: paramsList.length,
    success: results.length,
    failed: failures.length,
    failures: failures.length ? failures : undefined,
    returned: topRows.length,
    sorted_by: sortBy,
    sort_order: ascending ? 'asc' : 'desc',
    l0_mode: config.l0Mode ?? 'formula',
    fields: { native: L0_NATIVE_FIELDS, mirror: L1_MIRROR_FIELDS },
    elapsed_ms: elapsed,
    avg_ms_per_case: paramsList.length ? Math.round((elapsed / paramsList.length) * 100) / 100 : 0,
  }
}

/**
 * 工具注册（薄封装，动态 import 保证离线可加载）
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {object} config
 */
export async function registerL0Estimate(ctx, config = {}) {
  const { defineTool } = await import('@deepseek-ai/dsh-tools')

  ctx.tools.register(defineTool({
    name: 'motor_l0_estimate',
    description:
      '基于经验公式对参数矩阵做毫秒级性能估算与排序。\n' +
      '输出每项的效率、转矩、温升、总损耗、转矩密度，以及可直接被 L1 消费的镜像字段。\n' +
      '结果附 TopN 交接载荷（l1_handoff），用于把候选集交给 RMxprt 精算。\n' +
      '纯本地计算，不调用任何外部求解器。',
    parameters: TOOL_PARAMS,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args) {
      try {
        const result = runL0Estimate(args, config)
        return JSON.stringify(result, null, 2)
      } catch (err) {
        return JSON.stringify({ error: true, message: String(err?.message ?? err) }, null, 2)
      }
    },
  }))
}

export default { runL0Estimate, registerL0Estimate, TOOL_PARAMS, SORTABLE_FIELDS }
