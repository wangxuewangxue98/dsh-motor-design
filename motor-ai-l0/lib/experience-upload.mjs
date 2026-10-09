/**
 * 设计经验脱敏回传（design_exp 通道，零新增依赖，显式可选，默认关闭）
 * ============================================================
 * 与 telemetry.mjs 的关系：
 *   telemetry.mjs 回传的是「使用统计」—— TELEM_FIELDS 白名单刻意**不含**设计内容。
 *   本模块是**并行的第二条通道**，回传的是「脱敏设计样本」—— 规格 + 几何 + 结论 +
 *   误差带（design_exp record），用于把外部真实设计分布喂回 L0 代理重训 / 真值库 /
 *   可行性画像。两通道**各用一个开关、各用一份白名单、各走一个端点**，互不干扰。
 *
 * 为什么需要它（2026-10-09 取证）：
 *   使用遥测健全（55 调用 / 0 失败），但「外部用户实际设计了哪些电机」不回流。
 *   本机只有手动 experience-hub capture 的 1 份 12000 行样本。design_exp 通道补上
 *   「外部设计域 → 本仓真值库」这一段，且**只回流已脱敏的规格/几何/结论**，不碰
 *   任何身份信息。
 *
 * 合规底线（对齐 DSH 生态遥测规范，与 telemetry.mjs 同级）：
 *   1. 只回传 DESIGN_EXP_FIELDS 白名单字段（硬编码），逐条明细里的其它键一律丢弃。
 *   2. 白名单**不含**：账号/client_id/提示词/工作目录/绝对路径/文件名/密钥/设备标识。
 *      规格与几何属可公开设计空间（不含身份信息），可回流。
 *   3. fire-and-forget：HTTP 失败/超时**绝不影响**工具主流程，offset 不推进、下批重报。
 *   4. 默认关闭：designExperienceUpload=false 且端点为空 = 完全不启用（显式 opt-in）。
 *   5. N1 教训固化：写样本前做「规格键完整率」自检；残缺规格**降级为不带 spec**
 *      （只回传几何+结论），绝不带残缺规格污染真值库。
 *
 * 体量护栏：
 *   - 单条 design_exp ~400B；单批上限 experienceBatchSize（默认 200 条）。
 *   - 本地 experience.jsonl 48h 滚动：超期记录在 GC 时丢弃最旧，避免无限堆积。
 *   - 去重：rec_id（规格+几何哈希）幂等，管理端 ingest 侧做库级去重（P2）。
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import crypto from 'node:crypto'

/** 从 telemetry.mjs 复用字节 offset 增量读取（同一份 usage.jsonl 落盘约定下的通用能力） */
export { readPendingRecords } from './telemetry.mjs'

// ═════════════════════════════════════════════════════════════
// 1. design_exp 脱敏白名单（单一真源，硬编码）
// ═════════════════════════════════════════════════════════════

/**
 * 允许外发的字段白名单（嵌套在 spec / geo / out 三段内）。
 * 全部是「可公开设计空间 + 结论 + 误差带」，没有任何身份信息。
 * 白名单外的键（含任何意外混入的路径/账号/提示词）在 buildDesignExp 里被丢弃。
 */
export const DESIGN_EXP_SPEC_FIELDS = [
  'power_kw',           // kW
  'voltage_v',          // V
  'speed_rpm',          // rpm
  'poles',              // 极数
  'line_freq_hz',       // Hz  生效电源频率
  'cooling',            // 冷却方式（L0 私有取值）
  'insulation_class',   // B/F/H
  'motor_type',         // induction / pmsm（resolveMotorType 归一后的英文类型）
]

export const DESIGN_EXP_GEO_FIELDS = [
  'stator_od', 'stator_id', 'core_length', 'air_gap',
  'slots_stator', 'slots_rotor',
]

export const DESIGN_EXP_OUT_FIELDS = [
  'efficiency_raw',     // %  未封顶真值
  'temp_rise',          // K
  'total_loss',         // W
  'feasible',           // bool
  'verdict',            // feasible / infeasible_geometry / infeasible_thermal
  'confidence',         // 0..1
  'confidence_band_pp', // 误差带（百分点）
]

/** spec 段的核心键（N1 自检用）：缺任一即视为「规格残缺」→ 降级不带 spec */
export const DESIGN_EXP_SPEC_CORE = ['power_kw', 'speed_rpm', 'voltage_v', 'poles']

/**
 * 红线键黑名单：这些键**永远**不得出现在 design_exp 里。
 * 与「本机绝对路径泄漏」门禁同形态——写样本时做反向断言（verify.mjs 消费）。
 */
export const DESIGN_EXP_FORBIDDEN = [
  'client_id', 'user', 'account', 'username', 'hostname', 'cwd',
  'prompt', 'token', 'api_key', 'secret', 'password',
  'stator_od_limit', // 这是 L0 私有过滤参数，不是设计量，不进样本
]

// ═════════════════════════════════════════════════════════════
// 2. 从 L0 结果行裁剪 design_exp（纯函数，可单测）
// ═════════════════════════════════════════════════════════════

/**
 * N1 自检：判断一条样本的规格是否完整。
 * 只有核心键（power_kw/speed_rpm/voltage_v/poles）齐备才返回 true；
 * 残缺时调用方应降级为「仅回传几何+结论」（spec 段整体省略，并打 spec_complete:false）。
 * @param {object} spec 归一化后的 spec 段
 * @returns {boolean}
 */
export function isSpecComplete(spec) {
  return DESIGN_EXP_SPEC_CORE.every((k) => spec[k] !== undefined && spec[k] !== null)
}

/**
 * rec_id：规格 + 几何的 sha1 前 12 位（幂等键，供管理端库级去重）。
 * 用 JSON.stringify 的**键排序**保证同内容同 hash，与字段书写顺序无关。
 */
export function recIdOf(spec, geo) {
  const norm = (o) => {
    const keys = Object.keys(o).sort()
    return keys.map((k) => `${k}=${o[k] === undefined ? '' : o[k]}`).join('|')
  }
  const raw = `spec:${norm(spec)};geo:${norm(geo)}`
  return crypto.createHash('sha1').update(raw).digest('hex').slice(0, 12)
}

/**
 * 从一行 L0 结果裁剪一条 design_exp record。
 * @param {object} row L0 结果行（quickL0Estimate/buildL0Result 的产物，params 嵌套）
 * @param {object} meta { pluginVersion?: string }
 * @returns {object} design_exp record（只含白名单字段 + 脱敏红线自检结果）
 *
 * 关键：spec 段**从 row.params 的 L1 矩阵字段取**（speed/voltage/poles/power_kw/cooling/
 * motor_type），而非误读 speed_rpm/voltage_v（N1 事故根因）；线频率/绝缘等级取行级
 * 原生诊断字段。geo 段取 L1 几何。out 段取 L0 结论 + 误差带。
 */
export function buildDesignExp(row, meta = {}) {
  const p = (row && row.params && typeof row.params === 'object') ? row.params : {}

  // ---- spec 段：L1 矩阵行字段 → 归一化 spec 命名 ----
  const spec = {}
  if (p.power_kw !== undefined) spec.power_kw = p.power_kw
  // 矩阵行用 speed / voltage（L1_MATRIX_FIELDS 命名），归一到 spec 口径
  if (p.speed !== undefined) spec.speed_rpm = p.speed
  if (p.voltage !== undefined) spec.voltage_v = p.voltage
  if (p.poles !== undefined) spec.poles = p.poles
  if (row.line_freq_hz !== undefined) spec.line_freq_hz = row.line_freq_hz
  if (row.insulation_class !== undefined) spec.insulation_class = row.insulation_class
  if (p.cooling !== undefined) spec.cooling = p.cooling
  // motor_type 用 resolveMotorType 归一后的英文类型（避免中文别名混入样本）
  if (p.motor_type !== undefined) spec.motor_type = p.motor_type

  // ---- geo 段：L1 几何字段 ----
  const geo = {}
  for (const k of DESIGN_EXP_GEO_FIELDS) {
    if (p[k] !== undefined) geo[k] = p[k]
  }

  // ---- out 段：L0 结论 + 误差带 ----
  const out = {}
  if (row.efficiency_raw !== undefined) out.efficiency_raw = row.efficiency_raw
  if (row.temp_rise !== undefined) out.temp_rise = row.temp_rise
  if (row.total_loss !== undefined) out.total_loss = row.total_loss
  if (row.feasible !== undefined) out.feasible = row.feasible
  if (row.verdict !== undefined) out.verdict = row.verdict
  if (row.confidence !== undefined) out.confidence = row.confidence
  // 误差带：公式通道给固定经验带（P2-1 未接入自适应误差带前用常数占位，避免造假）
  out.confidence_band_pp = 1.5

  // ---- N1 完整率自检：规格残缺则降级不带 spec ----
  const specComplete = isSpecComplete(spec)
  const record = {
    rec_type: 'design_exp',
    rec_id: recIdOf(specComplete ? spec : {}, geo),
    plugin_version: meta.pluginVersion || '',
    spec_complete: specComplete,
  }
  if (specComplete) record.spec = spec
  record.geo = geo
  record.out = out
  // 红线自检结果供调用方/门禁审计（本身不上传）
  record._selfcheck = { forbidden_hit: scanForbidden(record) }
  return record
}

/**
 * 红线扫描：rec 里（spec/geo/out 顶层）是否出现任何 DESIGN_EXP_FORBIDDEN 键。
 * 命中即返回键名数组（正常应为空）。
 * @returns {string[]}
 */
export function scanForbidden(record) {
  const hits = []
  for (const seg of ['spec', 'geo', 'out']) {
    const o = record?.[seg]
    if (!o || typeof o !== 'object') continue
    for (const k of Object.keys(o)) {
      if (DESIGN_EXP_FORBIDDEN.includes(k)) hits.push(`${seg}.${k}`)
    }
  }
  return hits
}

// ═════════════════════════════════════════════════════════════
// 3. 本地 experience.jsonl 落盘槽位 + 48h 滚动 GC
// ═════════════════════════════════════════════════════════════

/**
 * 解析设计经验本地落盘槽位（$DSH_HOME/storages/dsh-motor-ai-l0）。
 * @param {object} config 插件配置（读 designExperienceUpload 开关）
 * @returns {{ file: string, offsetFile: string } | null}
 */
export function getExperienceSink(config = {}) {
  if (config.designExperienceUpload === false) return null
  try {
    const home = process.env.DSH_HOME || path.join(os.homedir(), '.dsh')
    const dir = path.join(home, 'storages', 'dsh-motor-ai-l0')
    fs.mkdirSync(dir, { recursive: true })
    return { file: path.join(dir, 'experience.jsonl'), offsetFile: path.join(dir, 'experience.offset') }
  } catch {
    return null
  }
}

/**
 * 追加一条 design_exp 到本地 experience.jsonl（JSONL，fire-and-forget，静默降级）。
 * @param {{ file: string } | null} sink
 * @param {object} record buildDesignExp 的产物（含 rec_id/rec_type，_selfcheck 会被剥掉）
 */
export function logExperience(sink, record) {
  if (!sink?.file) return
  try {
    const { _selfcheck, ...clean } = record
    fs.appendFileSync(sink.file, JSON.stringify(clean) + '\n', 'utf8')
  } catch { /* 绝不影响工具主流程 */ }
}

/** 48h 滚动阈值（毫秒）：本地样本超期丢弃最旧，避免无限堆积 */
export const EXPERIENCE_TTL_MS = 48 * 3600 * 1000

/**
 * 48h 滚动 GC：读 experience.jsonl，保留 ts 在 48h 内的记录，重写文件并清零 offset。
 * 只在无待上报数据时安全执行；offset 归零后下一批从头读。
 * @param {object} sink getExperienceSink 的返回值
 * @returns {{ kept: number, dropped: number } | null}
 */
export function gcExperience(sink) {
  if (!sink?.file) return null
  if (!fs.existsSync(sink.file)) return { kept: 0, dropped: 0 }
  try {
    const now = Date.now()
    const lines = fs.readFileSync(sink.file, 'utf8').split('\n').filter((l) => l.trim() !== '')
    const keep = []
    let dropped = 0
    for (const l of lines) {
      let ts
      try { ts = new Date(JSON.parse(l).ts || 0).getTime() } catch { dropped++; continue }
      if (now - ts <= EXPERIENCE_TTL_MS) keep.push(l)
      else dropped++
    }
    fs.writeFileSync(sink.file, keep.length ? keep.join('\n') + '\n' : '', 'utf8')
    // 文件被重写，offset 必须清零，否则增量读取会错位
    try { fs.writeFileSync(sink.offsetFile, '0', 'utf8') } catch { /* offset 写失败仅导致重读 */ }
    return { kept: keep.length, dropped }
  } catch {
    return null
  }
}

// ═════════════════════════════════════════════════════════════
// 4. 回传（fire-and-forget，offset 增量，失败不推进）
// ═════════════════════════════════════════════════════════════

/**
 * 回传一次 design_exp 样本（offset 增量 + 48h GC + fire-and-forget）。
 * @param {object} config 插件配置（designExperienceUpload / experienceEndpoint / experienceBatchSize）
 * @param {object} opts { pluginVersion?: string }
 * @returns {Promise<{ reported: boolean, samples: number, reason?: string }>}
 */
export async function reportExperience(config, opts = {}) {
  const enabled = config?.designExperienceUpload === true
  const endpoint = String(config?.experienceEndpoint || config?.telemetryEndpoint || '').trim()
  if (!enabled || !endpoint) return { reported: false, samples: 0, reason: 'disabled' }

  const sink = getExperienceSink(config)
  if (!sink) return { reported: false, samples: 0, reason: 'no-sink' }

  // 48h 滚动 GC（先清旧，避免把过期样本夹进本批）
  gcExperience(sink)

  const batch = Math.max(1, Number(config.experienceBatchSize) || 200)
  const pending = readPendingRecords(sink, batch)
  if (!pending || pending.records.length === 0) {
    return { reported: false, samples: 0, reason: 'no-pending' }
  }

  // payload：逐条 design_exp（已脱敏白名单裁剪）+ 平台元数据（无身份字段）
  const payload = {
    plugin: 'dsh-motor-ai-l0',
    plugin_version: opts.pluginVersion || '',
    kind: 'l0-design-exp',
    records: pending.records,
    sample_count: pending.records.length,
    platform: os.platform(),
    node_major: process.versions.node.split('.')[0],
    reported_at: new Date().toISOString(),
  }

  try {
    const controller = typeof AbortController !== 'undefined' ? new AbortController() : null
    const timer = controller ? setTimeout(() => controller.abort(), 5000) : null
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'User-Agent': 'dsh-motor-ai-l0/experience' },
      body: JSON.stringify(payload),
      ...(controller ? { signal: controller.signal } : {}),
    })
    if (timer) clearTimeout(timer)

    // 2xx/3xx 推进 offset；否则不推进（下批重报）
    if (res && res.ok) {
      try {
        fs.mkdirSync(path.dirname(sink.file), { recursive: true })
        fs.writeFileSync(sink.offsetFile, String(pending.nextOffset), 'utf8')
      } catch { /* offset 写失败仅导致重复上报，无副作用 */ }
      return { reported: true, samples: pending.records.length, payload }
    }
    return { reported: false, samples: 0, reason: `http_${res?.status ?? 'n/a'}` }
  } catch (err) {
    return { reported: false, samples: 0, reason: String(err?.message ?? err).slice(0, 60) }
  }
}

export default {
  DESIGN_EXP_SPEC_FIELDS, DESIGN_EXP_GEO_FIELDS, DESIGN_EXP_OUT_FIELDS,
  DESIGN_EXP_SPEC_CORE, DESIGN_EXP_FORBIDDEN,
  isSpecComplete, recIdOf, buildDesignExp, scanForbidden,
  getExperienceSink, logExperience, gcExperience, EXPERIENCE_TTL_MS,
  reportExperience,
}
