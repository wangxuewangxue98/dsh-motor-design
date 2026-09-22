/**
 * 本地用量日志（零依赖 JSONL）
 * ============================================================
 * 设计原则：
 *   1. 不注入任何 DSH 服务 —— 核心遥测服务实名是 sessionTelemetry
 *      （由 dsh-session-telemetry-otel 提供），不存在名为 telemetry 的
 *      可注入服务；照抄社区资料的 inject: ['telemetry'] 会让插件永久
 *      pending（复刻 apiProxy 事故）。因此自研纯文件方案。
 *   2. 绝不影响工具主流程 —— 所有 IO 都包在 try/catch 里，静默降级。
 *   3. 只记元数据不记内容 —— 不落设计参数与结果明细（行业 Know-how
 *      留在会话内），只记调用次数、耗时、规模与成败，足够支撑：
 *      - 公式系数标定时的真实工况分布统计
 *      - 工具链命中率（param_matrix → estimate 转化率）
 *      - 社区统计面板（dsh-usage-statistics-panel / dsh-usage-unified）
 *        的本地数据源补齐。
 *
 * 落盘位置：$DSH_HOME/storages/dsh-motor-ai-l0/usage.jsonl
 *   （DSH_HOME 缺省为 ~/.dsh；每行一个 JSON 对象，追加写。）
 *
 * 用法（仅在 registerX 的 execute 闭包里调用，纯函数 run* 不触碰，
 * 因此 scripts/verify.mjs 的离线自检不会产生日志文件）：
 *   const sink = getUsageSink(config)
 *   logUsage(sink, { tool: 'motor_l0_estimate', ok: true, elapsed_ms: 2, n: 20 })
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

/** 进程级单例：同一 DSH 进程只解析一次落盘目录 */
const memo = { resolved: false, sink: null }

/**
 * 解析用量日志落盘槽位。
 * @param {object} config 插件配置（读 usageLog 开关）
 * @returns {{ file: string } | null} null = 关闭或解析失败（静默不记）
 */
export function getUsageSink(config = {}) {
  if (config.usageLog === false) return null
  if (memo.resolved) return memo.sink
  memo.resolved = true
  try {
    const home = process.env.DSH_HOME || path.join(os.homedir(), '.dsh')
    const dir = path.join(home, 'storages', 'dsh-motor-ai-l0')
    fs.mkdirSync(dir, { recursive: true })
    memo.sink = { file: path.join(dir, 'usage.jsonl') }
  } catch {
    memo.sink = null
  }
  return memo.sink
}

/**
 * 追加一条用量记录（JSONL）。任何失败都静默吞掉。
 * @param {{ file: string } | null} sink getUsageSink 的返回值
 * @param {{ tool: string, ok: boolean, elapsed_ms?: number, n?: number, error?: string, [k: string]: unknown }} record
 */
export function logUsage(sink, record) {
  if (!sink?.file) return
  try {
    const line = JSON.stringify({ ts: new Date().toISOString(), ...record })
    fs.appendFileSync(sink.file, line + '\n', 'utf8')
  } catch {
    /* 用量日志绝不影响工具调用 */
  }
}

export default { getUsageSink, logUsage }
