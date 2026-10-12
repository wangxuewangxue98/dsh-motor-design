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
} from '@wangxue98/dsh-motor-ai-core'
import { quickL0Estimate } from '@wangxue98/dsh-motor-ai-core'
import { resolveMotorType } from '@wangxue98/dsh-motor-ai-core'
import {
  loadSurrogateModel,
  predictBatch,
  buildSurrogateResult,
  DEFAULT_CONFIDENCE_THRESHOLD,
} from '@wangxue98/dsh-motor-ai-core'
import { getUsageSink, logUsage } from '@wangxue98/dsh-motor-ai-core'
import { enforceQuota } from '../../lib/quota.mjs'
import { assessApplicability, scenarioFromMatrix } from '@wangxue98/dsh-motor-ai-core'

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
  mc_samples: {
    type: 'number',
    description: 'v0.4.0 P2：蒙特卡洛不确定度采样数（默认 0=关闭）。开启后对 TopN 每行做 N 次 ±3% 参数扰动重算，' +
      '给出效率 95% 置信区间 [ci_low, ci_high] 作为「水位线」，排序应结合 CI 而非只看点估计。建议 100~500',
  },
  mc_ci_level: {
    type: 'number',
    description: 'v0.4.0 P2：置信水平，默认 0.95（可选 0.90/0.99），仅 mc_samples>0 时生效',
  },
}

/** 允许参与排序的字段白名单（防注入 sort 到任意键） */
export const SORTABLE_FIELDS = ['efficiency', 'efficiency_raw', 'torque_density', 'temp_rise', 'total_loss', 'power']

/** temp_rise 为「越小越好」，其余为「越大越好」 */
const ASCENDING_FIELDS = new Set(['temp_rise', 'total_loss'])

const r1 = (v) => Math.round(v * 10) / 10
const r2 = (v) => Math.round(v * 100) / 100
const r3 = (v) => Math.round(v * 1000) / 1000

/**
 * v0.4.0 P2：蒙特卡洛不确定度（MC/CI 水位线）
 * ---------------------------------------------------------------------------
 * L0 是「点估计」，但几何/电气参数存在制造公差与估算误差。对每行做 N 次 ±3% 参数
 * 扰动重算，统计效率分布，给出置信区间 [ci_low, ci_high] 作为「水位线」——
 * 点估计低于 ci_low 的方案在误差带下可能不达标，排序应结合 CI 而非只看点估计。
 * 默认关闭（mc_samples=0）以避免性能/回归风险。CI 用正态近似（ci 0.90/0.95/0.99 → z）。
 * @param {object} baseParams 原始参数行
 * @param {object} baseResult 原始点估计结果
 * @param {number} samples 扰动次数
 * @param {number} ciLevel 置信水平（0.90/0.95/0.99）
 * @param {(row:object)=>object} estimateOpts 行级 opts 构造器
 * @returns {object|null}
 */
function monteCarloRow(baseParams, baseResult, samples, ciLevel, estimateOpts) {
  const effs = []
  const temps = []
  for (let i = 0; i < samples; i++) {
    const perturb = (v, frac) => (typeof v === 'number' && Number.isFinite(v))
      ? v * (1 + (Math.random() * 2 - 1) * frac) : v
    const p = {
      ...baseParams,
      stator_id: r1(perturb(baseParams.stator_id, 0.03)),
      core_length: r1(perturb(baseParams.core_length, 0.03)),
      turns_per_coil: Math.max(1, Math.round(perturb(baseParams.turns_per_coil, 0.03))),
      peak_current: r1(perturb(baseParams.peak_current, 0.03)),
      air_gap: r3(perturb(baseParams.air_gap, 0.05)),
    }
    try {
      const e = quickL0Estimate(p, estimateOpts(p))
      if (typeof e.efficiency === 'number' && Number.isFinite(e.efficiency)) effs.push(e.efficiency)
      if (typeof e.temp_rise === 'number' && Number.isFinite(e.temp_rise)) temps.push(e.temp_rise)
    } catch { /* 跳过非法扰动 */ }
  }
  if (effs.length < 2) return { samples: effs.length, available: false }
  const mean = effs.reduce((a, b) => a + b, 0) / effs.length
  const variance = effs.reduce((a, b) => a + (b - mean) ** 2, 0) / effs.length
  const se = Math.sqrt(variance) / Math.sqrt(effs.length)
  const z = ciLevel >= 0.99 ? 2.576 : (ciLevel >= 0.95 ? 1.96 : 1.645)
  return {
    samples: effs.length,
    available: true,
    ci_level: ciLevel,
    efficiency_mean: r2(mean),
    efficiency_ci: [r2(mean - z * se), r2(mean + z * se)],
    efficiency_point: typeof baseResult?.efficiency === 'number' ? r2(baseResult.efficiency) : null,
    efficiency_spread_pp: r2(Math.max(...effs) - Math.min(...effs)),
    temp_rise_mean: temps.length ? r1(temps.reduce((a, b) => a + b, 0) / temps.length) : null,
  }
}

/** 对 TopN 结果批量附加 MC 不确定度（无副作用：失败不影响点估计） */
function applyMonteCarlo(topRows, mcSamples, mcCiLevel, estimateOpts) {
  if (mcSamples <= 0) return false
  for (const r of topRows) {
    try {
      const mc = monteCarloRow(r.params, r, mcSamples, mcCiLevel, estimateOpts)
      if (mc) r.mc = mc
    } catch { /* MC 异常静默，保留点估计 */ }
  }
  return true
}

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
  // v0.4.0 P2：MC/CI 模式解析（默认关闭）
  const mcSamples = Number(args.mc_samples) > 0 ? Math.min(Math.floor(Number(args.mc_samples)), 2000) : 0
  const mcCiLevel = [0.90, 0.95, 0.99].includes(Number(args.mc_ci_level)) ? Number(args.mc_ci_level) : 0.95

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
        applyMonteCarlo(topRows, mcSamples, mcCiLevel, estimateOpts)

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
          // v0.4.0 P2：MC/CI 水位线（与公式通道同口径）
          mc_enabled: mcSamples > 0,
          mc_samples: mcSamples > 0 ? mcSamples : undefined,
          mc_ci_level: mcSamples > 0 ? mcCiLevel : undefined,
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
  applyMonteCarlo(topRows, mcSamples, mcCiLevel, estimateOpts)

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
    // v0.4.0 P2：MC/CI 水位线（仅在 mc_samples>0 时有意义）
    mc_enabled: mcSamples > 0,
    mc_samples: mcSamples > 0 ? mcSamples : undefined,
    mc_ci_level: mcSamples > 0 ? mcCiLevel : undefined,
    mc_watermark: mcSamples > 0
      ? `本批 TopN 已附蒙特卡洛效率置信区间（N=${mcSamples}, 水平 ${mcCiLevel}）。` +
        `排序请结合效率_ci 水位线：点估计低于 ci_low 的方案在 ±3% 参数误差带下可能不达标。`
      : undefined,
    // ---- 转矩口径声明（v0.2.5 P0 → v0.4.0 dual_field_resolver 升级）----
    // 期望输出 torque（排序口径）= spec 9550·P/n 精确值；可达能力 torque_capability
    // = surrogate(T1, env 默认 OFF) → d2l(方法2) → stress(方法3) 物理代理（低置信）。
    // 旧 _physics.estimated_torque 线性类比已弃用（同几何多转矩点误差 max 523%）。
    // 交接给 RMxprt 时必须用 specs[].torque_nm（规格），不得用能力值当规格。
    torque_contract: {
      basis: 'dual_field_resolver',
      spec_field: 'torque_nm',
      spec_formula: '9550·P(kW)/n(rpm)',
      expected_output: {
        fields: ['torque', 'torque_density', 'torque_source', 'torque_confidence'],
        note: '排序口径：spec 9550·P/n 精确值（conf 1.0）优先；无 P/n 时回退能力估计',
      },
      capability_output: {
        fields: ['torque_capability', 'capability_source', 'capability_confidence'],
        methods: ['surrogate(T1, env DSH_L0_SURROGATE_TORQUE 默认 OFF)', 'd2l(T2, 方法2 电磁负荷)', 'stress(T2b, 方法3 切向应力)'],
        note: '可达能力代理估算，绝不当作额定转矩规格使用',
      },
      deprecated_analogy: {
        field: '_physics.estimated_torque',
        formula: 'torque_nm × (D²L)/(D²L_base)',
        status: '已弃用',
        reason: '线性类比对同几何多转矩点误差 max 523%，已被方法2/3 物理公式取代',
      },
      surrogate_env_switch: 'DSH_L0_SURROGATE_TORQUE',
      warning: 'torque_capability 是广筛可行性代理（低置信），交接 RMxprt 必须用 specs[].torque_nm（规格），不得用能力值当规格',
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
        const quotaErr = await enforceQuota(config)
        if (quotaErr) return quotaErr.error
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
            await import('@wangxue98/dsh-motor-ai-core')
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
