/**
 * 工具 4：motor_l0_pipeline —— 三步链聚合（根治 DSH 2000 行截断）
 * ===========================================================
 * 设计动机（问题①根因）：
 *   原三步链要求模型在上下文里「读回 120 行矩阵 + 转发 120 行 + 读回校验报告」，
 *   每一跳都暴露 DSH 的 2000 行读取上限。工具回显 4446 行时，模型静默丢行，
 *   计数对不上 120 也**无报错**——这是「让模型亲手编排三步链」的结构性风险。
 *
 * 本工具把三步链收敛到插件进程内一次性跑完：
 *   buildParamMatrix → runDesignValidate → runL0Estimate
 * 只向模型回吐**紧凑结果**（summary + TopN（默认 10 行）+ 交接载荷 + 文件交接路径），
 * 体量恒 < 2000 行，从根本上绕开截断。完整矩阵落盘到 handoff 文件，由模型按需读取。
 *
 * 纯函数 runL0Pipeline() 零依赖、可单测；registerL0Pipeline() 才触碰 DSH Runtime。
 */

import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import { normalizeSpec } from '../../lib/param-schema.mjs'
import { buildParamMatrix } from './param-matrix.mjs'
import { runDesignValidate } from './design-validate.mjs'
import { runL0Estimate } from './l0-estimate.mjs'
import { getUsageSink, logUsage } from '../../lib/usage-log.mjs'

/** 工具入参声明（矩阵生成参数 + 聚合控制） */
export const TOOL_PARAMS = {
  power_kw: { type: 'number', required: true, description: '额定功率 (kW)' },
  speed_rpm: { type: 'number', required: true, description: '额定转速 (rpm)' },
  voltage_v: { type: 'number', description: '电压 (V)，默认 380' },
  poles: { type: 'number', description: '极数，缺省由转速推荐' },
  motor_type: {
    type: 'string',
    description: '电机类型。传 PMSM/BLDC/IPM 走永磁生产口径；不传按 induction（异步 legacy 口径）生成并回吐 MOTOR_TYPE_ASSUMED 提示。PMSM 工况必须显式传入，否则整批按异步几何计算',
  },
  torque_nm: { type: 'number', description: '额定转矩 (Nm)，缺省由 9550·P/n 推算' },
  cooling: { type: 'string', description: '冷却方式，默认 forced_air' },
  stator_od_limit: { type: 'number', description: '定子外径上限 (mm)。缺省时按基准尺寸自伸缩（≈扫描空间上界×1.05，向上取整到 50mm），保证默认路径不被夹紧；显式传入（机座号 / 隔爆外壳等硬约束）则严格尊重，夹紧时回吐 OD_LIMIT_CLAMPED 告警' },
  base_diameter: { type: 'number', description: '基准内径中心 (mm)' },
  base_length: { type: 'number', description: '基准铁心长度中心 (mm)' },
  count: { type: 'number', description: '目标方案数，默认 20（广筛场景可给 120）' },
  top_n: { type: 'number', description: '回吐前 N 个方案，默认 10（聚合工具已封顶，避免回吐全量触发截断）' },
  sort_by: {
    type: 'string',
    description: '排序字段：efficiency / torque_density / temp_rise，默认 efficiency',
  },
  write_handoff: {
    type: 'boolean',
    description: '是否把 TopN + L1 交接载荷写入文件（默认 true）。强烈建议开启：' +
      '模型读文件即可拿到完整候选，无需在上下文里搬运 120 行矩阵',
  },
}

/**
 * 三步链聚合（纯函数，零依赖，可单测）
 * @param {object} rawArgs 工具入参
 * @param {object} [config] 插件配置（透传给三个子工具；handoffDir 控制落盘目录）
 * @returns {{
 *   summary: object, recommended: object|null, results: object[],
 *   handoff: object, handoff_path: string|null, elapsed_ms: number,
 *   note: string
 * }}
 */
export function runL0Pipeline(rawArgs, config = {}) {
  const { spec: args } = normalizeSpec(rawArgs ?? {})
  const t0 = Date.now()

  // ---- 第 1 步：生成参数矩阵 ----
  // 保留完整返回对象：param-matrix 的 warnings / od_limit 必须透传出来，
  // 否则「外径被夹紧」这类会扭曲结论的异常在聚合链路里会被静默吞掉。
  const matrix = buildParamMatrix(args, config)
  const allRows = matrix.matrix
  const requestedCount = Math.max(1, Math.floor(args.count ?? 20))

  // ---- 第 2 步：批量物理校验 ----
  const validated = runDesignValidate({ params_list: allRows }, config)
  const failedSet = new Set(validated.failed_indices ?? [])
  // 剔除 failed（物理不成立），保留 passed + warning
  const feasibleRows = allRows.filter((_, i) => !failedSet.has(i))

  // ---- 第 3 步：L0 估算排序（默认封顶 Top10，根治回吐全量）----
  const topN = Number(args.top_n) > 0 ? Number(args.top_n) : 10
  const sortBy = ['efficiency', 'efficiency_raw', 'torque_density', 'temp_rise', 'total_loss', 'power']
    .includes(args.sort_by) ? args.sort_by : 'efficiency'
  const estimate = runL0Estimate({ params_list: feasibleRows, top_n: topN, sort_by: sortBy }, config)

  // ---- 计数自洽校验（让模型一眼确认没有丢行）----
  const sumStatus = (validated.summary?.passed ?? 0)
    + (validated.summary?.warning ?? 0)
    + (validated.summary?.failed ?? 0)
  const countOk = sumStatus === allRows.length && (matrix.truncated || allRows.length === requestedCount)

  // ---- 文件交接（避免在模型上下文里搬运完整矩阵）----
  let handoffPath = null
  const wantFile = args.write_handoff !== false
  if (wantFile) {
    try {
      const dir = (typeof config.handoffDir === 'string' && config.handoffDir)
        ? config.handoffDir
        : join(process.cwd(), 'l0-handoff')
      mkdirSync(dir, { recursive: true })
      handoffPath = join(dir, 'latest.json')
      writeFileSync(handoffPath, JSON.stringify({
        summary: {
          requested_count: requestedCount,
          generated: allRows.length,
          truncated: matrix.truncated,
          validated_total: validated.summary?.total ?? allRows.length,
          passed: validated.summary?.passed ?? 0,
          warning: validated.summary?.warning ?? 0,
          failed: validated.summary?.failed ?? 0,
          feasible_after_filter: feasibleRows.length,
          top_n_returned: estimate.returned,
          feasible_count: estimate.feasible_count,
          count_ok: countOk,
          sort_by: sortBy,
        },
        recommended: estimate.recommended,
        results: estimate.results,
        handoff: estimate.handoff,
        rule_hits: validated.rule_hits,
      }, null, 2), 'utf8')
    } catch (e) {
      // 落盘失败不阻断流水线：仍回吐紧凑摘要，仅 handoff_path 置空并告警
      handoffPath = null
      // 用一个非序列化副作用标记，便于调用方察觉
      // （不入主结果对象，避免污染 JSON 输出）
      try { console.warn('[pipeline] handoff 文件写入失败，已降级为仅上下文输出:', e.message) } catch { /* noop */ }
    }
  }

  return {
    // 矩阵层告警（如 OD_LIMIT_CLAMPED）放在最前，保证调用方第一眼看到
    warnings: matrix.warnings ?? [],
    od_limit: matrix.od_limit,
    summary: {
      requested_count: requestedCount,
      generated: allRows.length,
      truncated: matrix.truncated,
      validated_total: validated.summary?.total ?? allRows.length,
      passed: validated.summary?.passed ?? 0,
      warning: validated.summary?.warning ?? 0,
      failed: validated.summary?.failed ?? 0,
      feasible_after_filter: feasibleRows.length,
      top_n_returned: estimate.returned,
      feasible_count: estimate.feasible_count,
      count_ok: countOk,
      sort_by: sortBy,
      // v0.2.4：整批触顶时 efficiency 是被钳的显示值，真值在结果行的 efficiency_raw
      efficiency_cap: estimate.efficiency_cap,
      efficiency_capped_count: estimate.efficiency_capped_count,
      rule_hits: validated.rule_hits,
    },
    efficiency_note: estimate.efficiency_note,
    recommended: estimate.recommended,
    results: estimate.results,
    handoff: estimate.handoff,
    handoff_path: handoffPath,
    elapsed_ms: Date.now() - t0,
    note: '聚合工具已内部跑完三步链并封顶 TopN，上下文只占紧凑摘要；' +
      '完整候选见 handoff_path（若为空，确认 write_handoff 与可写目录）。' +
      '计数自检：passed+warning+failed 应等于 generated（= requested_count 除非截断）。',
  }
}

/**
 * 工具注册（薄封装，动态 import 保证离线可加载）
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {object} config
 */
export async function registerL0Pipeline(ctx, config = {}) {
  const { defineTool } = await import('@deepseek-ai/dsh-tools')

  ctx.tools.register(defineTool({
    name: 'motor_l0_pipeline',
    description:
      '电机设计三步链聚合工具（推荐入口，根治 DSH 2000 行截断风险）——\n' +
      '内部一次性跑完 buildParamMatrix → runDesignValidate → runL0Estimate，\n' +
      '只回吐**紧凑结果**：计数摘要 + TopN（默认 10 行）+ L1 交接载荷 + 文件交接路径，\n' +
      '上下文体量恒 < 2000 行，从根本上避免让模型在上下文里搬运 120 行矩阵导致的静默丢行。\n' +
      '完整候选落盘到 handoff_path（默认 ./l0-handoff/latest.json），模型按需读取即可。\n' +
      '结果内置计数自检 summary.count_ok：passed+warning+failed 必须等于 generated（= 请求数，除非截断），\n' +
      '一眼确认没有丢行。\n' +
      '⚠️ 仅在需要单独调试某一步（如查看完整校验报告、手调 failed 阈值）时，才拆开用三个独立工具；\n' +
      '常规广筛请直接用本工具，不要亲手把 120 行矩阵在对话里传来传去。',
    parameters: TOOL_PARAMS,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args) {
      const t0 = Date.now()
      try {
        const result = runL0Pipeline(args, config)
        logUsage(getUsageSink(config), {
          tool: 'motor_l0_pipeline', ok: true, elapsed_ms: Date.now() - t0,
          n: result?.summary?.generated ?? 0,
          failed: result?.summary?.failed,
          feasible: result?.summary?.feasible_count,
        })
        return JSON.stringify(result, null, 2)
      } catch (err) {
        logUsage(getUsageSink(config), {
          tool: 'motor_l0_pipeline', ok: false, elapsed_ms: Date.now() - t0,
          error: String(err?.message ?? err),
        })
        return JSON.stringify({ error: true, message: String(err?.message ?? err) }, null, 2)
      }
    },
  }))
}

export default { runL0Pipeline, registerL0Pipeline, TOOL_PARAMS }
