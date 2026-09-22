/**
 * dsh-motor-ai-l0 —— 电机AI辅助设计软件插件 · L0 层（毫秒级广筛）
 * ============================================================
 * 当前交付范围（W1-W5）：
 *   - Config Schema（层级总闸 + L0 运行参数 + 效率口径对齐 + W4 校验参数）
 *   - apply() 生命周期：assertLevelImplemented 快速失败 → 注册 3 个 L0 工具 → 预留 L1/L2 告警
 *   - 注册工具：motor_param_matrix / motor_l0_estimate / motor_design_validate
 *
 * 硬性依赖（由 DSH Runtime 提供，不在 package.json 声明运行时依赖）：
 *   - @deepseek-ai/cordis       上下文与插件体系
 *   - @deepseek-ai/schemastery  配置校验
 *
 * @typedef {object} Config
 * @property {'l0'|'l1'|'l2'} level          层级总闸，当前唯一有效值 'l0'
 * @property {'formula'|'surrogate'|'auto'} l0Mode
 * @property {string} surrogatePath          代理模型权重路径
 * @property {number} surrogateConfidenceThreshold  置信度低于此值降级到公式
 * @property {number} maxMatrixSize          参数矩阵最大组合数
 * @property {number} topNPreview            L0 结果预览 TopN
 * @property {number} efficiencyCap          效率封顶（对齐 _run_simulated L752）
 * @property {[number, number]} tempRiseRange temp 钳位区间 (°C)，对齐 L1 max_temp；命名待议（拟改 maxTempClamp）
 * @property {boolean} [l1Enabled]           预留
 * @property {boolean} [l2Enabled]           预留
 * @property {boolean} [usageLog=true]       本地用量日志(JSONL)开关
 */

import Schema from '@deepseek-ai/schemastery'
import { assertLevelImplemented, isLevelEnabled, tierOf } from './lib/level-gate.mjs'

export const name = 'dsh-motor-ai-l0'

/** 注入依赖：tools（工具注册能力） */
export const inject = ['tools']

export const Config = Schema.object({
  // ---- 层级总闸 ----
  level: Schema.union(['l0', 'l1', 'l2']).default('l0')
    .description('求解层级总闸：l0 经验公式 / l1 RMxprt 磁路法(预留) / l2 Motor-CAD 精验(预留)'),

  // ---- L0 运行参数 ----
  l0Mode: Schema.union(['formula', 'surrogate', 'auto']).default('auto')
    .description('L0 估算模式：formula 纯公式 / surrogate 代理模型 / auto 自动降级'),
  surrogatePath: Schema.string().default('models/l0_surrogate.json')
    .description('代理模型权重文件路径'),
  surrogateConfidenceThreshold: Schema.number().default(0.7)
    .description('代理模型置信度阈值，低于此值自动降级到公式模式'),
  maxMatrixSize: Schema.number().default(2000)
    .description('参数矩阵最大组合数'),
  topNPreview: Schema.number().default(20)
    .description('L0 结果预览返回的 TopN 数量'),

  // ---- 口径对齐（v1 取证 #4：与 _run_simulated 同口径，避免 L0/L1 排序跳变）----
  efficiencyCap: Schema.number().default(96)
    .description('效率封顶值，对齐 Python 侧 _run_simulated (motor_tools.py:752)'),
  tempRiseRange: Schema.tuple([Schema.number(), Schema.number()]).default([45, 130])
    .description('max_temp 钳位区间 [min,max] (°C)，对齐 L1 max_temp 口径（注意：非 L0 温升 K）；命名待议'),

  // ---- W4 物理校验参数 ----
  airGapFluxT: Schema.number().default(0.8)
    .description('气隙磁密基准 (T)，齿/轭磁密反算的输入；PMSM 典型 0.75~0.90'),
  insulationClass: Schema.union(['B', 'F', 'H']).default('F')
    .description('绝缘等级，决定温升限值（B=80K / F=105K / H=125K）'),
  highSpeedRpm: Schema.number().default(8000)
    .description('高速工况阈值 (rpm)：超过后气隙上限放宽到 4mm，经验气隙比对让位'),

  // ---- 用量遥测（本地 JSONL，零依赖，不注入任何 DSH 服务）----
  usageLog: Schema.boolean().default(true)
    .description('本地用量日志开关：每次工具调用追加一行元数据到 ~/.dsh/storages/dsh-motor-ai-l0/usage.jsonl（只记次数/耗时/规模/成败，不记设计参数与结果内容）'),

  // ---- L1/L2 预留（实现后需同步扩展 IMPLEMENTED_LEVELS 允许列表）----
  l1Enabled: Schema.boolean().default(false)
    .description('预留：L1 RMxprt 磁路法求解，当前阶段不生效'),
  l2Enabled: Schema.boolean().default(false)
    .description('预留：L2 Motor-CAD 精细验证，当前阶段不生效'),
})

/**
 * 插件入口
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {Config} config
 */
export async function apply(ctx, config) {
  // 1. 快速失败：任何试图越级激活的配置立即报错（验收 #6）
  assertLevelImplemented(config.level)

  // 2. L1/L2 预留告警（配置里被误打开时提示而非静默生效）
  if (config.l1Enabled) {
    ctx.logger?.warn?.('[dsh-motor-ai-l0] L1 尚未实现，l1Enabled 已忽略')
  }
  if (config.l2Enabled) {
    ctx.logger?.warn?.('[dsh-motor-ai-l0] L2 尚未实现，l2Enabled 已忽略')
  }

  // 3. 注册 L0 工具集（W3 两个 + W4 校验器，共 3 个）
  //    动态 import：tools 层不触碰 DSH Runtime，可在无 Runtime 环境被单独加载做单测
  const { registerL0Tools } = await import('./tools/l0/index.mjs')
  const tools = await registerL0Tools(ctx, config)

  ctx.logger?.info?.(
    `[dsh-motor-ai-l0] L0 已加载，模式: ${config.l0Mode}` +
    ` | 效率封顶 ${config.efficiencyCap}%` +
    ` | 温升区间 ${config.tempRiseRange?.[0]}~${config.tempRiseRange?.[1]}K` +
    ` | 绝缘 ${config.insulationClass} 级` +
    ` | 工具 ${tools.join(',')}` +
    ` | 付费等级 ${tierOf(config.level)}` +
    ` | 用量日志 ${config.usageLog === false ? 'off' : 'on'}`
  )
}

export default { name, inject, Config, apply }
