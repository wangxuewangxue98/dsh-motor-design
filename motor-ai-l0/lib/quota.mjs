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
 *     pipeline 内连调多个内部函数不重复扣。v0.5.1（P0-门1修复）起四个纯函数
 *     （runL0Pipeline / buildParamMatrix / runDesignValidate / runL0Estimate）
 *     入口全部注入 assertFreeQuota，绕过工具网关直调同样被计。
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
 *   入参 { plugin_id, device_id, day, limit }  ← v0.5.1 起附带本地自然日与本地限额
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
 * 调用链一次性消费标记（**v0.5.2 重写：显式调用链令牌**）
 * ============================================================================
 * v0.5.1 的写法是「_consumed WeakSet（key = config 对象）+ queueMicrotask 复位」，
 * 实测有两个致命缺陷（最小复现见 scripts/_repro_syncbatch.mjs）：
 *   ① **同步批量脱闸**：同步 for 循环复用同一 config 引用时，微任务在整段循环结束前
 *      不会执行 ⇒ 第 2~N 次 alreadyConsumed() 恒为 true ⇒ **整批只扣 1 次**。
 *      CI 脚本 / 压测循环正是这种写法，门禁形同虚设（P0-门1'）。
 *   ② **语义错位**：把「一次用户动作」错误等价于「config 对象引用相同」——
 *      反过来，同一 config 引用被复用于两次独立动作时又会被误判成同链。
 * v0.5.2 改为由聚合入口（runL0Pipeline）新建**唯一调用链令牌**，并派生携带令牌的
 * config 透传给子纯函数：
 *   - 同链（同一令牌）⇒ 只扣 1 次，行为不变；
 *   - 跨调用（哪怕同一 config 引用、同一 macrotask、同步循环）⇒ 令牌不同 ⇒ 各计一次。
 */
const QUOTA_CHAIN = Symbol('l0-quota-chain')
let _chainConsumed = null // WeakSet<token>；令牌随调用链结束自然可回收，无需定时清理

/** 新建一次调用链的唯一令牌（聚合入口每次调用新建一个） */
export function newQuotaChain() {
  return {}
}

/**
 * 派生「属于同一调用链」的 config（携带不可枚举令牌）。
 * 不污染调用方传入的 config 对象本身 ⇒ 用户复用同一 config 时仍各自独立计数。
 * @param {object} [config] 调用方配置
 * @param {object} token newQuotaChain() 产出的令牌
 * @returns {object} 携带令牌的派生配置
 */
export function withQuotaChain(config, token) {
  const out = Object.assign({}, config || {})
  Object.defineProperty(out, QUOTA_CHAIN, {
    value: token, enumerable: false, writable: false, configurable: false,
  })
  return out
}

function chainConsumed(token) {
  return !!(token && _chainConsumed && _chainConsumed.has(token))
}
function markChainConsumed(token) {
  if (!token) return
  if (!_chainConsumed) _chainConsumed = new WeakSet()
  _chainConsumed.add(token)
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
 * 硬闸（消耗型）—— 注入各 L0 纯函数入口/工具入口顶部。
 * v0.5.1（P0-门1修复）：四个纯函数（runL0Pipeline / buildParamMatrix /
 * runDesignValidate / runL0Estimate）**全部注入本闸**。此前闸门只装在
 * registerX.execute，脚本/CI import 直调纯函数可零限制跑完（探针实测复现）。
 *  - 本地 free 计数达到 limit（默认 10）→ 抛 QuotaExceededError（fail-closed，断网也拦）。
 *  - 同一调用链只消耗一次：判据是**调用链令牌**（v0.5.2），不是 config 对象引用。
 *    pipeline 新建令牌并用 withQuotaChain() 派生配置透传给三个子纯函数 ⇒ 整链只扣 1 次；
 *    而调用方同步循环复用同一 config 引用时，因每次顶层调用令牌不同 ⇒ **各计一次**
 *    （v0.5.1 此处整批只扣 1 次，见 scripts/_repro_syncbatch.mjs）。
 *  - config.freeQuotaEnabled === false → 直接放行（运维开关；纯函数入口同样尊重，
 *    内部离线批量工具——experience-hub capture / case-feedback 聚合——据此显式 bypass）。
 * @param {string} [tool] 调用来源标记（仅用于审计/日志）
 * @param {object} [config] 配置对象；同链子调用须传 withQuotaChain() 派生的带令牌对象
 */
export function assertFreeQuota(tool = 'l0', config = {}) {
  if (config && config.freeQuotaEnabled === false) return // 运维开关：纯函数入口同样尊重（v0.5.1）
  // v0.5.2：同链判据 = 调用链令牌（无令牌 ⇒ 视为独立顶层调用，必计一次）
  const token = config && typeof config === 'object' ? config[QUOTA_CHAIN] : null
  if (chainConsumed(token)) return // 本调用链已消耗：不重复扣
  const state = readLocalQuota()
  if (state.free >= state.limit) {
    throw new QuotaExceededError(
      buildQuotaExceededMessage({ limit: state.limit, used: state.free }),
      { limit: state.limit, used: state.free, remaining: 0, day: state.day, tool, allowed: false, reason: 'quota-exhausted' }
    )
  }
  writeLocalQuota({ day: state.day, free: state.free + 1, limit: state.limit })
  markChainConsumed(token) // v0.5.2：按调用链令牌标记，同链子调用据此跳过
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
        // v0.5.1 P1：附带本地自然日与本地限额，接收端按同一把尺计数，
        // 消「接收端 UTC+8 vs 插件本地时区」「接收端固定 10 vs 插件可覆盖」两个口径差
        body: JSON.stringify({
          plugin_id: pluginId,
          device_id: deviceId,
          day: localDayKey(),
          limit: getDailyLimit(),
        }),
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

/** 完整版获取渠道（额度用尽引导用；与 README「安装完整版」一节口径一致） */
export const FULL_VERSION_HINTS = {
  contact: '13546196530@163.com',
  npmPackage: 'dsh-motor-ai',
  market: 'DSH 插件市场（dshhub-market 口令市场，搜 dsh-motor-ai）',
}

/** 额度用尽时的引导文案（l0 无 activate 工具，引导联系制作者 + 安装完整版） */
export function buildQuotaExceededMessage(info = {}) {
  const limit = info.limit ?? getDailyLimit()
  const used = info.used ?? limit
  const { contact, npmPackage, market } = FULL_VERSION_HINTS
  return (
    `今日免费额度已用尽（${used}/${limit} 次/天，按本地自然日重置）。\n` +
    `\n` +
    `📮 联系插件制作者（使用反馈 / 问题排查 / 购买授权 / 定制开发）：\n` +
    `   邮箱：${contact}\n` +
    `\n` +
    `📦 下载完整版 dsh-motor-ai（解锁无限次调用与完整能力）：\n` +
    `   1) ${market}\n` +
    `   2) npm：npm install ${npmPackage}@next\n` +
    `\n` +
    `或者：\n` +
    `1) 明天再用（免费额度按本地自然日自动重置）；\n` +
    `2) 安装完整版 dsh-motor-ai 解锁无限调用；\n` +
    `3) 购买授权或反馈请联系 ${contact}。`
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
