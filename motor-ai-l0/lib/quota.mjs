/**
 * 每日免费额度（G1）：L0 免费档的调用配额闸门 —— l0 免费包专用版。
 * ============================================================================
 * v0.5.0 语义升级（**重要，改前必读**）：闸门从「后端决定 + 断网放行」改为
 * **「本地为准的硬闸 + 非阻塞旁路遥测」**。原因是一条实测到的事故级缺陷：
 *
 *   旧实现 `checkFreeQuota` 里是 `await fetch(...)` + 4s AbortController 超时。
 *   一旦 `commerceBackendUrl` 指向一个**在中国大陆不可达**的地址（实测
 *   `*.workers.dev` 被 DNS 污染、TCP 443 直连超时），国内用户**每次工具调用
 *   都要白等 4 秒**才能拿到 fail-open 放行 —— 限流没生效，延迟先炸了。
 *
 * 新语义（与完整版 lib/quota.mjs 对齐，去掉激活/档位部分）：
 *  1. **本地为准**：每日计数只认本地 free-quota.json，**不依赖后端**。
 *     后端可达时仅作旁路遥测（best-effort），不可达/异常不影响放行决策，
 *     **也不阻塞调用链**（fire-and-forget，调用方不 await）。
 *  2. **免费档硬闸（fail-closed）**：默认 10 次/天，用尽抛 QuotaExceededError。
 *     断网也拦 —— 否则「分发出去即失联」时闸门形同虚设，这正是本次要修的问题。
 *  3. **一次性消费标记**：同一调用链（同一 config 对象）只计 1 次；
 *     pipeline 内连调多个内部函数不重复扣。绕过工具网关直调内部函数同样被计。
 *  4. **与完整版共用一本账**：配额文件路径与完整版 lib/license.mjs:52 的
 *     激活文件同目录同文件名（`$DSH_HOME/storages/dsh-motor-ai/free-quota.json`），
 *     同机上 l0 与完整版的免费额度不会分裂成两本。
 *
 * 与完整版 lib/quota.mjs 的关键差异（改前必读）：
 *  - **不 import ./license.mjs**：l0 免费包没有激活模块，恒为免费档，不做档位判定。
 *  - **无付费档**：l0 不提供付费能力，故无 paidLimit / DEFAULT_PAID_DAILY_LIMIT。
 *  - **getDeviceId 算法与完整版 lib/license.mjs:86 逐字节一致**
 *    （sha256(hostname|platform|arch).slice(0,16)）——同一台机器上 l0 与完整版
 *    必须是同一个 device_id，否则后端额度账会分裂成两本。
 *  - **额度用尽引导不含 activate 工具**（l0 未提供），改为引导安装完整版。
 *
 * 后端契约（Core/dsh_registry.py:free_call → POST /api/dsh/free-call，仅遥测用）：
 *   入参 { plugin_id, device_id }
 *   成功 { allowed, used, remaining, limit, day }
 *
 * 诚实边界：本地限制本质是「软限制」——懂技术用户可删/改 free-quota.json 清零。
 * 但它比旧版（后端不可达即无限）强一个数量级：正常用户无法无声跳过。
 */
import { hostname, homedir } from 'node:os'
import { createHash } from 'node:crypto'
import { join, dirname } from 'node:path'
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'

/** 插件侧上报的 plugin_id（与完整版一致：两侧共用同一本免费额度账） */
export const DEFAULT_QUOTA_PLUGIN_ID = 'dsh-motor-ai-l0'

/** 每日免费额度默认上限（可用 DSH_FREE_DAILY_LIMIT 覆盖） */
export const DEFAULT_FREE_DAILY_LIMIT = 10

/** 旁路遥测超时（毫秒）。仅用于后台 Promise，**不在调用链上等待** */
const TELEMETRY_TIMEOUT_MS = 4000

/**
 * 设备指纹 —— 必须与完整版 lib/license.mjs:86 getDeviceId 完全一致。
 * 改这里 = 两端额度账分裂，属于事故级改动。
 */
export function getDeviceId() {
  try {
    const raw = `${hostname()}|${process.platform}|${process.arch}`
    return createHash('sha256').update(raw, 'utf-8').digest('hex').slice(0, 16)
  } catch {
    return 'unknown-device'
  }
}

/**
 * 配额文件路径 —— 与完整版 lib/license.mjs:52 getActivationFilePath 同目录同文件名，
 * 确保同机上 l0 与完整版读写的是**同一本**免费额度账（否则账分裂成两本）。
 * 可用 DSH_L0_QUOTA_FILE 覆盖（测试/多实例隔离用）。
 */
export function getQuotaFilePath() {
  if (process.env.DSH_L0_QUOTA_FILE) return process.env.DSH_L0_QUOTA_FILE
  const home = process.env.DSH_HOME || join(homedir(), '.dsh')
  return join(home, 'storages', 'dsh-motor-ai', 'free-quota.json')
}

/** 后端基址解析（优先级：config > env）。**未显式配置时返回空串 ⇒ 不做任何网络请求**，
 *  避免向 127.0.0.1:5000 发无意义请求。 */
function backendBase(config = {}) {
  return String(
    config.commerceBackendUrl ||
    config.backendUrl ||
    process.env.DSH_BACKEND_URL ||
    ''
  ).trim().replace(/\/+$/, '')
}

/** 本地自然日键（按机器本地时区，非 UTC，符合「按天重置」直觉） */
function localDayKey() {
  const d = new Date()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

/** 每日上限：默认 10（DEFAULT_FREE_DAILY_LIMIT），环境变量 DSH_FREE_DAILY_LIMIT 可覆盖 */
export function getDailyLimit() {
  const n = Number(process.env.DSH_FREE_DAILY_LIMIT)
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : DEFAULT_FREE_DAILY_LIMIT
}

/**
 * 读本地配额态；文件缺失/损坏/跨天 → 视为新的一天，从 0 开始。
 * 兼容完整版结构 {day, free, paid, ...} 与旧 l0 结构 {day, used}。
 */
function readLocalQuota() {
  const limit = getDailyLimit()
  const day = localDayKey()
  let free = 0
  try {
    const obj = JSON.parse(readFileSync(getQuotaFilePath(), 'utf-8'))
    if (obj && obj.day === day) free = Math.max(0, Number(obj.free ?? obj.used) || 0)
  } catch {
    /* 缺失/损坏 → 新一天，从 0 */
  }
  return { day, free, limit }
}

/** 写本地配额态；只读/损坏目录静默失败，仍按内存态强制，不崩 */
function writeLocalQuota(state) {
  const file = getQuotaFilePath()
  try {
    mkdirSync(dirname(file), { recursive: true })
    writeFileSync(
      file,
      JSON.stringify({ day: state.day, free: state.free, limit: state.limit }, null, 2),
      'utf-8'
    )
  } catch {
    /* 静默：强制逻辑不依赖写盘成功 */
  }
}

/**
 * 调用链一次性消费标记：config 对象被消费后置入 _consumed；
 * 当前 macrotask（一次工具调用）结束后的微任务里清空，确保：
 *  - 同一调用链内的顺序兄弟（buildParamMatrix → runDesignValidate）不重复扣；
 *  - 下一次独立工具调用（新 macrotask）拿到干净的 _consumed，重新计数。
 */
let _consumed = null
let _resetScheduled = false
const _scheduleReset =
  typeof queueMicrotask === 'function'
    ? queueMicrotask
    : typeof setImmediate === 'function'
      ? setImmediate
      : (cb) => setTimeout(cb, 0)

function markConsumed(config) {
  if (!config) return
  if (!_consumed) _consumed = new WeakSet()
  _consumed.add(config)
  if (!_resetScheduled) {
    _resetScheduled = true
    _scheduleReset(() => {
      _consumed = new WeakSet()
      _resetScheduled = false
    })
  }
}
function alreadyConsumed(config) {
  return !!(config && _consumed && _consumed.has(config))
}

/** 配额用尽错误：内部函数抛出，工具层捕获后转成引导文案 */
export class QuotaExceededError extends Error {
  /** @param {string} message @param {object} [info] */
  constructor(message, info = {}) {
    super(message)
    this.name = 'QuotaExceededError'
    this.quota = info
  }
}

/**
 * 硬闸（消耗型）—— 注入各 L0 模块函数/工具入口顶部。
 *  - 本地 free 计数达到 limit（默认 10）→ 抛 QuotaExceededError（fail-closed，断网也拦）。
 *  - 同一调用链（config 对象相同）只消耗一次：后续兄弟/嵌套调用见标记即跳过。
 * @param {string} [tool] 调用来源标记（仅用于审计/日志）
 * @param {object} [config] 调用链透传对象；同一用户动作各层须传同一引用
 */
export function assertFreeQuota(tool = 'l0', config = {}) {
  if (alreadyConsumed(config)) return // 本调用链已消耗：不重复扣
  const state = readLocalQuota()
  if (state.free >= state.limit) {
    throw new QuotaExceededError(
      buildQuotaExceededMessage({ limit: state.limit, used: state.free }),
      { limit: state.limit, used: state.free, remaining: 0, day: state.day, tool, allowed: false, reason: 'quota-exhausted' }
    )
  }
  writeLocalQuota({ day: state.day, free: state.free + 1, limit: state.limit })
  markConsumed(config)
}

/**
 * 旁路遥测（best-effort，**不参与放行决策、不在调用链上等待**）：
 * 后端可达时 POST /api/dsh/free-call 同步计数；若后端返 used 更大
 * （换机/多副本场景），取 max 回写本地。
 * 未配置后端地址时**直接返回，不发请求**。
 */
function syncBackendTelemetry(config, localUsed) {
  const base = backendBase(config)
  if (!base) return Promise.resolve()
  const pluginId = String(config.quotaPluginId || DEFAULT_QUOTA_PLUGIN_ID)
  let deviceId
  try { deviceId = getDeviceId() } catch { return Promise.resolve() }
  return (async () => {
    try {
      const controller = typeof AbortController !== 'undefined' ? new AbortController() : null
      let timer = null
      if (controller) {
        timer = setTimeout(() => controller.abort(), TELEMETRY_TIMEOUT_MS)
        // 不因旁路遥测把 CLI 进程/事件循环多吊 4 秒
        if (typeof timer.unref === 'function') timer.unref()
      }
      const res = await fetch(`${base}/api/dsh/free-call`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plugin_id: pluginId, device_id: deviceId }),
        ...(controller ? { signal: controller.signal } : {}),
      })
      if (timer) clearTimeout(timer)
      const body = await res.json()
      if (body && body.allowed === false && Number(body.used) > localUsed) {
        const state = readLocalQuota()
        writeLocalQuota({ day: state.day, free: Math.max(state.free, Number(body.used) || 0), limit: state.limit })
      }
    } catch {
      /* 失联/超时/非 JSON → 忽略，本地为准 */
    }
  })()
}

/**
 * 只读查询（不消耗）—— 供工具入口做友好提前拒绝 + 后端旁路遥测。
 * **放行决策 100% 以本地为准**；后端仅遥测，不决定放行、不阻塞。
 * @param {object} [config]
 * @returns {Promise<{allowed: boolean, used?: number, remaining?: number, limit?: number, reason: string}>}
 */
export async function checkFreeQuota(config = {}) {
  if (config.freeQuotaEnabled === false) {
    return { allowed: true, reason: 'disabled' }
  }
  const state = readLocalQuota()
  if (state.free >= state.limit) {
    return {
      allowed: false,
      reason: 'quota-exhausted',
      used: state.free,
      remaining: 0,
      limit: state.limit,
      day: state.day,
    }
  }
  // 旁路遥测：fire-and-forget，**不 await**（旧版在此 await 导致国内 4s 延迟陷阱）
  syncBackendTelemetry(config, state.free).catch(() => {})
  return {
    allowed: true,
    reason: 'within-quota',
    used: state.free,
    remaining: state.limit - state.free,
    limit: state.limit,
    day: state.day,
  }
}

/** 额度用尽时的引导文案（l0 无 activate 工具，引导安装完整版） */
export function buildQuotaExceededMessage(info = {}) {
  const limit = info.limit ?? getDailyLimit()
  const used = info.used ?? limit
  const contact = '13546196530@163.com'
  return (
    `今日免费额度已用尽（${used}/${limit} 次/天，按本地自然日重置）。\n` +
    `请选择：\n` +
    `1) 明天再用（额度自动重置）；\n` +
    `2) 安装完整版 dsh-motor-ai 解锁无限调用；\n` +
    `3) 购买授权请联系 ${contact}。`
  )
}

/**
 * 工具执行前的统一闸门（消耗型）：返回 null 表示放行，否则返回可直接给用户的错误对象。
 * @param {object} config
 * @returns {Promise<null | {error: string, quota: object}>}
 */
export async function enforceQuota(config = {}) {
  if (config.freeQuotaEnabled === false) return null
  try {
    assertFreeQuota('tool-gate', config)
    return null
  } catch (e) {
    if (e instanceof QuotaExceededError) return { error: e.message, quota: e.quota }
    throw e
  }
}

export default {
  checkFreeQuota,
  enforceQuota,
  assertFreeQuota,
  buildQuotaExceededMessage,
  QuotaExceededError,
  getDeviceId,
  getQuotaFilePath,
  getDailyLimit,
  DEFAULT_QUOTA_PLUGIN_ID,
  DEFAULT_FREE_DAILY_LIMIT,
}
