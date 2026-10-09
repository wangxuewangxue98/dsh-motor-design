/**
 * 工具 2：motor_l0_estimate —— L0 毫秒级估算与排序
 * ===========================================================
 * 输入：buildParamMatrix 产出的参数矩阵（或直接手写的同形状列表）
 * 输出：每项 = L0 原生 6 字段 + L1 镜像字段，并挂载 TopN 交接载荷
 *
 * 排序口径（跨层一致性关键点）：
 *   L0 与 L1 必须能放在一起比。若 L0 封顶 98.5% 而 L1 封顶 96%，
 *   同一批方案在两层的排序会跳变 —— 故**展示值** efficiency 统一 clamp 到
 *   config.efficiencyCap（默认 96，对齐 motor_tools.py:752）。
 *
 * v0.2.4 修正：cap 是"口径约定"不是物理真值。大机座整批触顶时（450kW 实测 Top10
 *   全 96.00，去重 1 个值、极差 0.00pt），按 efficiency 排序等于没排 —— 谁第一由数组
 *   原序决定。故**排序改用未封顶真值 efficiency_raw**，展示值仍是 efficiency，
 *   并在结果行给出 efficiency_capped 标记 + 顶层 efficiency_note 说明。
 *   跨层一致性未受影响：cap 值本身与钳位行为完全不变。
 *
 * 本文件同样保持「纯函数 + 薄封装」结构：
 *   runL0Estimate() 零依赖可单测；registerL0Estimate() 才触碰 DSH Runtime。
 */

import {
  normalizeSpec, assertMatrixShape, buildHandoff, L0_NATIVE_FIELDS, L1_MIRROR_FIELDS,
} from '../../lib/param-schema.mjs'
import { quickL0Estimate } from '../../lib/formula-engine.mjs'
import { resolveMotorType } from '../../lib/motor-constants.mjs'
import {
  loadSurrogateModel,
  predictBatch,
  buildSurrogateResult,
  DEFAULT_CONFIDENCE_THRESHOLD,
} from '../../lib/surrogate-engine.mjs'
import { getUsageSink, logUsage } from '../../lib/usage-log.mjs'
import { assessApplicability, scenarioFromMatrix } from '../../lib/applicability-gate.mjs'

/** 工具入参声明 */
export const TOOL_PARAMS = {
  params_list: {
    type: 'array', required: true,
    description: '参数组合列表，每项须含 stator_od/poles/voltage/speed 四个 L1 严格必填字段',
  },
  top_n: { type: 'number', description: '返回前 N 个结果。默认 10（已封顶，避免回吐全量触发 DSH 2000 行截断）；显式传 0 才取全量' },
  sort_by: {
    type: 'string',
    description: '排序字段：efficiency / efficiency_raw / torque_density / temp_rise，默认 efficiency。' +
      '注意：默认 efficiency 排序已改用未封顶真值（efficiency_raw），避免大机座整批触顶时排序被压平',
  },
}

/** 允许参与排序的字段白名单（防注入 sort 到任意键） */
export const SORTABLE_FIELDS = ['efficiency', 'efficiency_raw', 'torque_density', 'temp_rise', 'total_loss', 'power']

/** temp_rise 为「越小越好」，其余为「越大越好」 */
const ASCENDING_FIELDS = new Set(['temp_rise', 'total_loss'])

/**
 * 取排序键（v0.2.4）
 * 效率维度必须用**未封顶真值** efficiency_raw：efficiency 被 efficiencyCap 钳过，
 * 大机座整批触顶时会全部相等（450kW 实测 Top10 全 96.00），排序退化成由数组原序决定。
 * 代理通道行没有 efficiency_raw，退回 efficiency。
 */
function sortKey(row, sortBy) {
  const v = sortBy === 'efficiency' ? (row.efficiency_raw ?? row.efficiency) : row[sortBy]
  return typeof v === 'number' && Number.isFinite(v) ? v : 0
}

/**
 * L0 批量估算（纯函数，零依赖，可单测）
 * @param {object} rawArgs
 * @param {object} [config] 插件配置：{ efficiencyCap, tempRiseRange, topNPreview, l0Mode, surrogatePath, surrogateConfidenceThreshold }
 */
export function runL0Estimate(rawArgs, config = {}) {
  const { spec: args } = normalizeSpec(rawArgs)
  const paramsList = Array.isArray(args.params_list) ? args.params_list : []
  const sortBy = SORTABLE_FIELDS.includes(args.sort_by) ? args.sort_by : 'efficiency'
  // C 方案（根治 2000 行截断）：默认不再回吐全量，封顶 Top10；
  // 显式 top_n:0 才取全量（Number(0) > 0 为 false ⇒ 走默认值 10，故「全量」改用下面的特殊分支）
  const topN = Number(args.top_n) > 0 ? Number(args.top_n) : 10
  const wantAll = Number(args.top_n) === 0

  const efficiencyCap = config.efficiencyCap ?? 96
  const maxTempClamp = config.tempRiseRange ?? [45, 130]

  // v0.2.5：按行解析绝缘等级与电源频率，构造 quickL0Estimate 的 opts。
  // 取证根因：此前两处调用只传 {efficiencyCap, maxTempClamp}，行内的
  // insulation_class / line_freq_hz 从未到达公式引擎 ⇒ B 级按 F 级判、V16 恒按 50Hz 判。
  // 行内字段优先于全局 config（同一批内可混排不同等级/频率），缺省才回落 config。
  const estimateOpts = (row) => ({
    efficiencyCap,
    maxTempClamp,
    ...(row?.insulation_class ? { insulationClass: String(row.insulation_class).toUpperCase() } : {}),
    ...(Number.isFinite(Number(row?.line_freq_hz)) ? { lineFreqHz: Number(row.line_freq_hz) } : {}),
    ...(!row?.insulation_class && config.insulationClass
      ? { insulationClass: String(config.insulationClass).toUpperCase() } : {}),
    ...(!Number.isFinite(Number(row?.line_freq_hz)) && Number.isFinite(Number(config.line_freq_hz))
      ? { lineFreqHz: Number(config.line_freq_hz) } : {}),
  })
  
  // 灰度模式：默认公式通道，仅显式启用 surrogate 时才使用代理模型
  const l0Mode = config.l0Mode ?? 'formula'
  const surrogateThreshold = config.surrogateConfidenceThreshold ?? DEFAULT_CONFIDENCE_THRESHOLD

  const started = Date.now()
  const failures = []
  const results = []
  let surrogateFallbacks = 0

  // ---- 代理模型通道（需显式启用 l0Mode='surrogate'）----
  // 'auto' 与 'surrogate' 都走代理通道；'auto' 下代理失败/降级会自动回退公式通道（函数尾部）
  // 代理通道仅输出效率（温升/损耗/磁密/可行判定均由公式通道给出），
  // 因此只在用户显式指定 surrogate 时启用：auto 默认走字段完整的公式通道。
  // ---- v0.2.5 P5 能力门 ----
  // L0 的几何/磁密/反电势模型按工频中低压异步标定。超出标定域时模型不会给错数字，
  //   而是把整批判死（recommended=null），调用方只看到「无解」，无法区分
  //   「设计真不可行」与「这工具不适用」。能力门把后者显式说出来并给路由建议。
  // ⚠ 必须在 surrogate 分支之前求值：surrogate 通道的返回体也要带能力门结论。
  const applicability = assessApplicability(scenarioFromMatrix(paramsList))
  const gateNote = applicability.verdict === 'ok'
    ? undefined
    : `[L0 能力门 ${applicability.verdict.toUpperCase()}] routing=${applicability.routing}\n${applicability.advice}`

  if (l0Mode === 'surrogate') {
    try {
      const modelPath = config.surrogatePath ?? 'models/l0_surrogate_family.json'
      const model = loadSurrogateModel(modelPath)

      // v2.0.0: 推理端注入 l0_eff (公式通道可算的物理基, 非 RMxprt 输出)
      //   训练特征含 l0_eff 但 params 矩阵不含该字段 -> 用 quickL0Estimate 补算后注入;
      //   实测 Python/Node 两套公式仅差 ~0.5pp, 口径一致可安全复用。
      const enriched = paramsList.map((row) => {
        try {
          const l0 = quickL0Estimate(row, { ...estimateOpts(row), efficiencyCap: 99 })
          if (l0 && typeof l0.efficiency === 'number') {
            return { ...row, l0_eff: l0.efficiency }
          }
        } catch (_) { /* 公式失败则该行在代理通道降级公式 */ }
        return row
      })

      const { results: predictions, fallbacks } = predictBatch(
        enriched,
        model,
        surrogateThreshold
      )
      surrogateFallbacks = fallbacks

      // 0.2.6 P0：直传 params_list 的行可能仍无 motor_type（非 buildParamMatrix 产物）。
      // 缺省行已被 detectMotorType 统一判 induction（与生成默认一致），但必须让调用方看见数量。
      const assumedTypeRows = paramsList.filter(
        (r) => resolveMotorType(r?.motor_type).assumed
      ).length

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
          results.push(quickL0Estimate(row, estimateOpts(row)))
        } else {
          // 代理模型预测成功
          results.push(buildSurrogateResult(row, pred))
        }
      }

      if (results.length === paramsList.length) {
        const elapsed = Date.now() - started
        const ascending = ASCENDING_FIELDS.has(sortBy)
        results.sort((a, b) => (ascending
          ? sortKey(a, sortBy) - sortKey(b, sortBy)
          : sortKey(b, sortBy) - sortKey(a, sortBy)))

        const effectiveTopN = wantAll ? results.length : topN
        const topRows = results.slice(0, effectiveTopN)

        const handoff = buildHandoff(topRows, {
          fromLevel: 'l0',
          toLevel: 'l1',
          ranking: ['efficiency', 'torque_density'],
        })

        const feasibleCount = results.filter((r) => r.feasible === true).length
        const recommended = topRows.find((r) => r.feasible === true) ?? null

        return {
          results: topRows,
          handoff,
          total: paramsList.length,
          success: results.length,
          failed: failures.length,
          failures: failures.length ? failures : undefined,
          returned: topRows.length,
          feasible_count: feasibleCount,
          returned_feasible_count: topRows.filter((r) => r.feasible === true).length,
          motor_type_assumed_rows: assumedTypeRows > 0 ? assumedTypeRows : undefined,
          recommended: recommended
            ? { stator_od: recommended.params?.stator_od, poles: recommended.params?.poles, efficiency: recommended.efficiency, verdict: recommended.verdict }
            : null,
          sorted_by: sortBy,
          sort_order: ascending ? 'asc' : 'desc',
          l0_mode: 'surrogate',
          applicability: applicability.verdict,
          applicability_reasons: applicability.reasons.length ? applicability.reasons : undefined,
          applicability_advice: applicability.advice ?? undefined,
          applicability_note: gateNote,
          surrogate_experimental: model.experimental === true,
          surrogate_warning: model.experimental === true
            ? 'surrogate 通道为实验特性：训练域与真实机座不匹配且 cv_r2 偏低，结果仅供参考，请勿直接用于排序决策'
            : undefined,
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
      results.push(quickL0Estimate(row, estimateOpts(row)))
    } catch (err) {
      failures.push({ index: i, error: String(err?.message ?? err) })
    }
  }

  const ascending = ASCENDING_FIELDS.has(sortBy)
  results.sort((a, b) => (ascending
    ? sortKey(a, sortBy) - sortKey(b, sortBy)
    : sortKey(b, sortBy) - sortKey(a, sortBy)))

  const effectiveTopN = wantAll ? results.length : topN
  const topRows = results.slice(0, effectiveTopN)

  // TopN 交接载荷 —— v3 §10.1：L0 → L1
  const handoff = buildHandoff(topRows, {
    fromLevel: 'l0',
    toLevel: 'l1',
    ranking: ['efficiency', 'torque_density'],
  })

  // ---- v0.2.5 P5 能力门（结论已在函数开头求值，此处复用）----
  const elapsed = Date.now() - started
  const feasibleCount = results.filter((r) => r.feasible === true).length
  const recommended = topRows.find((r) => r.feasible === true) ?? null

  // 能力门判定为超出标定域时，「无解」必须归因于工具边界而非设计缺陷 ——
  //   否则 recommended=null 会被误读成「该工况做不出来」。
  const rejectAttribution = applicability.verdict === 'reject' && recommended === null
    ? `feasible_count=0 的原因是**当前工况超出 L0 标定域**（见 applicability），`
      + `而非「设计上无解」。请按 applicability.advice 调整工况或改走 L1/L2。`
    : undefined

  // v0.2.4：统计触顶比例并据实告知 —— efficiency 是「与 L1 同口径」的钳位显示值，
  // 触顶时它不等于真值；排序已改用 efficiency_raw，但展示值仍为 efficiency。
  const cappedCount = results.filter((r) => r.efficiency_capped === true).length
  const efficiencyNote = cappedCount > 0
    ? `本批 ${cappedCount}/${results.length} 行效率触顶被钳到 ${efficiencyCap}%（与 L1 同口径的显示值，非真值）；` +
      `排序已按未钳位真值 efficiency_raw 进行，结果行内可直接读到。`
    : undefined

  return {
    results: topRows,
    handoff,
    total: paramsList.length,
    success: results.length,
    failed: failures.length,
    failures: failures.length ? failures : undefined,
    returned: topRows.length,
    feasible_count: feasibleCount,
    // 0.2.6 P1：返回集可能混入 infeasible_thermal 行（温升超限为合法三态），
    // feasible_count 是全量口径，调用方需要「本次返回里真正可行」的数量来自检。
    returned_feasible_count: topRows.filter((r) => r.feasible === true).length,
    recommended: recommended
      ? {
        stator_od: recommended.params?.stator_od,
        poles: recommended.params?.poles,
        efficiency: recommended.efficiency,
        efficiency_raw: recommended.efficiency_raw,
        efficiency_capped: recommended.efficiency_capped,
        verdict: recommended.verdict,
      }
      : null,
    applicability: applicability.verdict,
    applicability_reasons: applicability.reasons.length ? applicability.reasons : undefined,
    applicability_advice: applicability.advice ?? undefined,
    applicability_note: gateNote ?? rejectAttribution,
    efficiency_cap: efficiencyCap,
    efficiency_capped_count: cappedCount,
    efficiency_note: efficiencyNote,
    // ---- v0.2.5 转矩口径声明（P0）----
    // L0 不预测转矩。矩阵行的 _physics.estimated_torque 是「按 D²L 几何类比外推的可达转矩」，
    // 与 target_torque（9550·P/n，用户规格）是不同物理量，差一个 D²L 比例。
    // 交接给 RMxprt 时必须用 specs[].torque_nm（规格），不得用 estimated_torque 当规格。
    torque_contract: {
      basis: 'analogy_d2l',
      spec_field: 'torque_nm',
      spec_formula: '9550·P(kW)/n(rpm)',
      analogy_field: '_physics.estimated_torque',
      analogy_formula: 'torque_nm × (D²L)/(D²L_base)',
      warning: 'estimated_torque 是几何类比外推量，不是规格承诺；当作规格使用会导致转矩静默违约',
    },
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
      '基于经验公式对参数矩阵做毫秒级性能估算与排序——流水线最后一步。\n' +
      '输入契约：params_list 必须由 motor_param_matrix 生成（每项含 16 个 L1 顶层字段），\n' +
      '不接受 power_kw / speed_rpm 等散装命名规格——请先调 motor_param_matrix。\n' +
      '建议先经 motor_design_validate 批量校验并剔除 failed 项后再传入，\n' +
      '避免「看起来效率高」的假方案污染 TopN。\n' +
      '输出每项的效率、转矩、温升、总损耗、转矩密度，以及可直接被 L1 消费的镜像字段。\n' +
      '结果附 TopN 交接载荷（l1_handoff），用于把候选集交给 RMxprt 精算。\n' +
      '⚠️ 输出默认封顶 Top10（top_n:0 才取全量）；广筛 120 行矩阵请勿直接用本工具回吐全量，\n' +
      '优先用聚合工具 motor_l0_pipeline（内部跑完三步链，只回紧凑摘要 + 文件交接，绕开 DSH 2000 行截断）。\n' +
      '纯本地计算，不调用任何外部求解器。',
    parameters: TOOL_PARAMS,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args) {
      const t0 = Date.now()
      try {
        const result = runL0Estimate(args, config)
        logUsage(getUsageSink(config), {
          tool: 'motor_l0_estimate', ok: true, elapsed_ms: Date.now() - t0,
          n: result?.total ?? args?.params_list?.length ?? 0,
          success: result?.success, failed: result?.failed,
          sort_by: args?.sort_by, top_n: args?.top_n,
        })
        // 设计经验脱敏回传（并行第二通道，默认关闭）：
        //   开启 designExperienceUpload 时，对本次返回的 TopN 结果裁 design_exp
        //   （规格+几何+结论+误差带，已脱敏白名单）落本地 experience.jsonl，
        //   由 index.mjs 的 setupExperienceUpload 周期 flush 增量回传。
        //   开关关 → getExperienceSink 返回 null → logExperience 静默，零副作用。
        {
          const { getExperienceSink, logExperience, buildDesignExp } =
            await import('../../lib/experience-upload.mjs')
          const sink = getExperienceSink(config)
          if (sink) {
            for (const row of result?.results ?? []) {
              logExperience(sink, buildDesignExp(row, {}))
            }
          }
        }
        return JSON.stringify(result, null, 2)
      } catch (err) {
        logUsage(getUsageSink(config), {
          tool: 'motor_l0_estimate', ok: false, elapsed_ms: Date.now() - t0,
          error: String(err?.message ?? err),
        })
        return JSON.stringify({ error: true, message: String(err?.message ?? err) }, null, 2)
      }
    },
  }))
}

export default { runL0Estimate, registerL0Estimate, TOOL_PARAMS, SORTABLE_FIELDS }
