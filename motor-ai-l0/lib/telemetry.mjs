/**
 * 脱敏聚合指标回传（零新增依赖，显式可选，默认关闭）
 * ============================================================
 * 与 usage-log.mjs 的关系：
 *   usage-log.mjs 是「本地落盘」——永远在本机，不触碰任何网络。
 *   本模块是「可选回传」——只有显式配置 telemetryEnabled=true 且
 *   telemetryEndpoint 非空时才会上报，且上报的是**聚合后的白名单
 *   指标**，逐条明细绝不外发。
 *
 * 合规底线（对齐 DSH 生态遥测规范）：
 *   1. 只回传白名单字段（TELEM_FIELDS），白名单由本文件硬编码，
 *      不依赖调用方自觉——即便 usage.jsonl 里被塞进其他键也会被丢弃。
 *   2. 白名单里**不含**：设计参数正文（stator_od/poles/…）、结果明细
 *      （efficiency/temp_rise/total_loss…）、提示词、工作目录、密钥、
 *      client_id、账号信息。
 *   3. 逐条明细只在本机；外发的只有「按工具聚合的次数/成败/耗时分布/
 *      规模总和」这类统计量。
 *   4. 回传是 fire-and-forget：HTTP 失败/超时**绝不影响**工具主流程，
 *      静默降级，最坏情况就是这批指标没上去（下批重报，offset 不推进）。
 *   5. 默认关闭。Config 里 telemetryEnabled 默认 false、endpoint 默认 ''，
 *      二者任缺其一即不启用——「显式 opt-in」是硬要求。
 *
 * 接入 session-telemetry 瀑布（v3 定案 option-3）：
 *   社区 Runtime 的核心遥测服务实名是 `sessionTelemetry`（由
 *   dsh-session-telemetry-otel 提供），**不存在**名为 `telemetry` 的
 *   可注入服务。因此本模块**不在** index.mjs 静态声明 inject，而是
 *   运行时探测 ctx 上的 sessionTelemetry（若有则把聚合指标附进去），
 *   探测不到就回退到 endpoint POST。两种通道都失败也只是静默不上报。
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

/**
 * 允许外发的字段白名单（单一真源，硬编码）。
 * 全部是「统计/元数据」，没有任何设计内容。
 */
export const TELEM_FIELDS = [
  'tool',        // 工具名（motor_param_matrix / motor_l0_estimate / motor_design_validate）
  'ok',          // 本次调用成败
  'elapsed_ms',  // 耗时（毫秒）
  'n',           // 本次处理的参数规模（矩阵行数/批量条数）
  'success',     // estimate 成功条数
  'failed',      // estimate 失败条数
  'warning',     // validate warning 条数
  'sort_by',     // 排序字段（efficiency/temp_rise/…）
  'top_n',        // 请求的 TopN
  'error',        // 失败时的错误类别（只取 message 首 80 字，且已脱敏，见 sanitize）
]

/** 错误信息 → 类别标签的关键词映射（顺序敏感，先匹配先生效）。
 *  只回传「类别」，不回传原始 error 文本（原文可能夹带参数/路径/密钥）。 */
const ERROR_CLASS_RULES = [
  [/(power_kw|speed_rpm|params_list|matrix|形状|shape|缺|missing|非法|invalid|不能|must be|NaN)/i, 'invalid_argument'],
  [/(surrogate|置信|confidence|fallback)/i, 'surrogate_fallback'],
  [/(timeout|超时|abort)/i, 'timeout'],
  [/(ENOENT|not found|cannot find|文件)/i, 'io_not_found'],
  [/(ECONN|ENOTFOUND|fetch|网络|network)/i, 'network'],
]

/**
 * 把原始 error 文本归一成「安全类别标签」（绝不回传原文）。
 * @param {string} raw 原始错误信息
 * @returns {string} 类别标签（invalid_argument / timeout / … / other_error）
 */
export function classifyError(raw) {
  const s = String(raw ?? '')
  for (const [re, label] of ERROR_CLASS_RULES) {
    if (re.test(s)) return label
  }
  return s.trim() ? 'other_error' : 'unknown'
}

/**
 * 把一条 usage 记录裁剪成「只有白名单字段」的脱敏形态。
 * 设计/隐私键一律丢弃；error 只回传「类别标签」而非原文。
 * @param {object} record 原始 usage.jsonl 行
 * @returns {object} 仅含 TELEM_FIELDS 中真实存在的键（error 已归一为类别）
 */
export function sanitizeTelemetry(record) {
  const out = {}
  for (const key of TELEM_FIELDS) {
    if (record[key] === undefined) continue
    if (key === 'error') {
      // 合规底线：只出类别，不出原文（原文可能夹带设计参数/路径/密钥）
      out[key] = classifyError(record.error)
      continue
    }
    out[key] = record[key]
  }
  // ts 是本地调用时间戳，属元数据（非设计内容），随聚合一起带上
  if (record.ts !== undefined) out.ts = record.ts
  return out
}

/**
 * 读取 usage.jsonl 的**尚未上报**部分（按字节 offset 增量）。
 * @param {{ file: string, offsetFile: string }} sink
 * @param {number} [limit] 最多取前 N 条完整记录（配合 telemetryBatchSize 分批推）；
 *   缺省 = 全部待上报记录
 * @returns {{ records: object[], nextOffset: number } | null}
 *   文件不存在 / 无可读内容时返回 null（静默）
 */
export function readPendingRecords(sink, limit = 0) {
  if (!sink?.file || !fs.existsSync(sink.file)) return null
  let from = 0
  try {
    if (sink.offsetFile && fs.existsSync(sink.offsetFile)) {
      const t = fs.readFileSync(sink.offsetFile, 'utf8').trim()
      from = Number(t) > 0 ? Number(t) : 0
    }
  } catch { from = 0 }

  let buf
  try {
    buf = fs.readFileSync(sink.file)
  } catch { return null }

  if (buf.byteLength <= from) return { records: [], nextOffset: from }

  const slice = buf.subarray(from).toString('utf8')
  // 只取完整的行（以 \n 结尾的）；末尾不完整的行留待下批
  const lastNl = slice.lastIndexOf('\n')
  const complete = lastNl >= 0 ? slice.slice(0, lastNl + 1) : ''
  const completeBytes = Buffer.byteLength(complete, 'utf8')

  const lines = complete.split('\n').filter((l) => l.trim() !== '')
  const capped = Number.isFinite(limit) && limit > 0 ? lines.slice(0, limit) : lines
  const records = []
  for (const line of capped) {
    try { records.push(JSON.parse(line)) } catch { /* 坏行跳过，不影响其余 */ }
  }
  // offset 只按「本批实际取走的完整行」推进（不是全部 complete）
  const consumed = capped.length ? Buffer.byteLength(capped.join('\n') + '\n', 'utf8') : 0
  return { records, nextOffset: from + consumed }
}

/**
 * 把脱敏记录按工具聚合成一份「统计量」payload（不含逐条明细）。
 * @param {object[]} records 脱敏后的记录
 * @param {{ pluginVersion?: string }} meta
 * @returns {object} 可直接 POST 的聚合 payload
 */
export function aggregateUsage(records, meta = {}) {
  const byTool = {}
  let total = 0
  let okCount = 0
  let failCount = 0
  let sumN = 0
  let elapsedMin = Infinity
  let elapsedMax = -Infinity
  let lastTs = ''

  for (const r of records) {
    const s = sanitizeTelemetry(r)
    const tool = s.tool || 'unknown'
    const b = (byTool[tool] ||= { calls: 0, ok: 0, failed: 0, sum_n: 0, elapsed_sum: 0 })
    b.calls += 1
    total += 1
    if (s.ok) { b.ok += 1; okCount += 1 } else { b.failed += 1; failCount += 1 }
    if (typeof s.n === 'number') { b.sum_n += s.n; sumN += s.n }
    if (typeof s.elapsed_ms === 'number') {
      b.elapsed_sum += s.elapsed_ms
      if (s.elapsed_ms < elapsedMin) elapsedMin = s.elapsed_ms
      if (s.elapsed_ms > elapsedMax) elapsedMax = s.elapsed_ms
    }
    if (s.ts && s.ts > lastTs) lastTs = s.ts
  }

  for (const t of Object.keys(byTool)) {
    const b = byTool[t]
    b.avg_elapsed_ms = b.calls ? Math.round(b.elapsed_sum / b.calls) : 0
    delete b.elapsed_sum
    b.success_rate = b.calls ? Math.round((b.ok / b.calls) * 1000) / 10 : 0
    if (b.sum_n === 0) delete b.sum_n
  }

  return {
    plugin: 'dsh-motor-ai-l0',
    plugin_version: meta.pluginVersion || '',
    kind: 'l0-usage-aggregate',
    calls_total: total,
    ok_total: okCount,
    failed_total: failCount,
    overall_success_rate: total ? Math.round((okCount / total) * 1000) / 10 : 0,
    sum_n: sumN,
    elapsed_min_ms: elapsedMin === Infinity ? 0 : elapsedMin,
    elapsed_max_ms: elapsedMax < 0 ? 0 : elapsedMax,
    by_tool: byTool,
    last_ts: lastTs,
    // 平台元数据（不含主机名/用户名/工作目录，只有 OS 与 node 大版本，属安全遥测）
    platform: os.platform(),
    node_major: process.versions.node.split('.')[0],
    reported_at: new Date().toISOString(),
  }
}

/**
 * 回传一次聚合指标（fire-and-forget，失败静默）。
 * @param {object} config 插件配置
 * @param {{ pluginVersion?: string, now?: () => number }} [opts]
 * @returns {Promise<{ reported: boolean, calls: number, reason?: string }>}
 */
export async function reportTelemetry(config, opts = {}) {
  const enabled = config?.telemetryEnabled === true
  const endpoint = String(config?.telemetryEndpoint || '').trim()
  if (!enabled || !endpoint) return { reported: false, calls: 0, reason: 'disabled' }

  // 落盘槽位复用 usage-log 的目录约定（$DSH_HOME/storages/dsh-motor-ai-l0）
  let dir
  try {
    const home = process.env.DSH_HOME || path.join(os.homedir(), '.dsh')
    dir = path.join(home, 'storages', 'dsh-motor-ai-l0')
  } catch { dir = path.join(os.homedir(), '.dsh', 'storages', 'dsh-motor-ai-l0') }

  const file = path.join(dir, 'usage.jsonl')
  const offsetFile = path.join(dir, 'usage.offset')
  const sink = { file, offsetFile }

  // 每批最多推 telemetryBatchSize 条（默认 50）；offset 只按本批实际发送推进，
  // 剩余待上报记录下批再走。
  const batch = Math.max(1, Number(config.telemetryBatchSize) || 50)
  const pending = readPendingRecords(sink, batch)
  if (!pending || pending.records.length === 0) {
    return { reported: false, calls: 0, reason: 'no-pending' }
  }

  const payload = aggregateUsage(pending.records, {
    pluginVersion: opts.pluginVersion || config?.pluginVersion || '',
  })

  try {
    const controller = typeof AbortController !== 'undefined' ? new AbortController() : null
    const timer = controller ? setTimeout(() => controller.abort(), 5000) : null
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'User-Agent': 'dsh-motor-ai-l0/telemetry' },
      body: JSON.stringify(payload),
      ...(controller ? { signal: controller.signal } : {}),
    })
    if (timer) clearTimeout(timer)

    // 2xx/3xx 视为成功：推进 offset；否则不推进（下批重报）
    if (res && res.ok) {
      try { fs.mkdirSync(dir, { recursive: true }); fs.writeFileSync(offsetFile, String(pending.nextOffset), 'utf8') } catch { /* offset 写失败仅导致重复上报，无副作用 */ }
      return { reported: true, calls: pending.records.length, payload }
    }
    return { reported: false, calls: 0, reason: `http_${res?.status ?? 'n/a'}`, payload }
  } catch (err) {
    // 网络/超时/拒绝：静默降级，不影响工具主流程
    return { reported: false, calls: 0, reason: String(err?.message ?? err).slice(0, 60), payload }
  }
}

/**
 * v3 option-3：把聚合指标挂到 DSH Runtime 的 session-telemetry 瀑布。
 * 运行时**探测**（不静态 inject，规避 apiProxy 事故）：
 *   - ctx.services.sessionTelemetry / ctx.sessionTelemetry / ctx.telemetry
 *   - 探测到任一且为可调用/可写对象，就 try 把 payload 附进去
 * 探测不到 / 附带失败 → 静默返回 false（本模块自身不改主流程）。
 * @param {object} ctx DSH Runtime 上下文
 * @param {object} payload aggregateUsage 的输出
 * @returns {boolean} 是否成功挂到 session-telemetry 通道
 */
export function attachSessionTelemetry(ctx, payload) {
  if (!ctx || !payload) return false
  const candidates = []
  if (ctx.services && typeof ctx.services === 'object') {
    candidates.push(ctx.services.sessionTelemetry, ctx.services.telemetry)
  }
  candidates.push(ctx.sessionTelemetry, ctx.telemetry)

  for (const svc of candidates) {
    if (!svc) continue
    // 可写对象：常见的是 .event / .emit / .add 任一
    for (const method of ['event', 'emit', 'add', 'log']) {
      if (typeof svc[method] === 'function') {
        try { svc[method](payload, { source: 'dsh-motor-ai-l0' }); return true } catch { /* 下一候选 */ }
      }
    }
  }
  return false
}

export default {
  TELEM_FIELDS, classifyError, sanitizeTelemetry, readPendingRecords,
  aggregateUsage, reportTelemetry, attachSessionTelemetry,
}
