/**
 * 每日免费额度（G1）：L0 免费档的调用配额闸门 —— l0 免费包专用版。
 * ============================================================
 * 三条硬规则（与完整版 lib/quota.mjs 相同，改动前请先读）：
 *  1. **l0 无激活态**：本包恒为免费档，不做激活查询（完整版才需"已激活跳过"）。
 *  2. **后端不可达 → fail-open（放行）**：断网时把免费用户锁死是绝不能犯的错，
 *     宁可少限一次，不可错杀。后端恢复后配额自然生效。
 *  3. **只在明确 allowed===false 时拦截**：后端任何异常/非预期响应都按放行处理。
 *
 * 与完整版 lib/quota.mjs 的关键差异（改前必读）：
 *  - **不 import ./license.mjs**：l0 免费包没有激活模块。
 *  - **getDeviceId 算法与完整版 lib/license.mjs:86 逐字节一致**
 *    （sha256(hostname|platform|arch).slice(0,16)）——同一台机器上 l0 与完整版
 *    必须是同一个 device_id，否则后端免费额度账会分裂成两本。
 *  - **额度用尽引导不含 activate 工具**（l0 未提供），改为引导安装完整版。
 *
 * 后端契约（Core/dsh_registry.py:free_call → POST /api/dsh/free-call）：
 *   入参 { plugin_id, device_id }
 *   成功 { allowed, used, remaining, limit, day }
 */
import { hostname } from 'node:os'
import { createHash } from 'node:crypto'

/** 插件侧上报的 plugin_id（与完整版一致：两侧共用同一本免费额度账） */
export const DEFAULT_QUOTA_PLUGIN_ID = 'dsh-motor-ai-l0'

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

function backendBase(config = {}) {
  return String(
    config.commerceBackendUrl ||
    config.backendUrl ||
    process.env.DSH_BACKEND_URL ||
    'http://127.0.0.1:5000'
  ).trim().replace(/\/+$/, '')
}

/**
 * 查询并计一次免费额度。
 * @param {object} [config]
 * @returns {Promise<{allowed: boolean, used?: number, remaining?: number, limit?: number, reason: string}>}
 */
export async function checkFreeQuota(config = {}) {
  const base = backendBase(config)
  const pluginId = String(config.quotaPluginId || DEFAULT_QUOTA_PLUGIN_ID)
  const deviceId = getDeviceId()
  const url = `${base}/api/dsh/free-call`

  let res
  try {
    const controller = (typeof AbortController !== 'undefined') ? new AbortController() : null
    const timer = controller ? setTimeout(() => controller.abort(), 4000) : null
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plugin_id: pluginId, device_id: deviceId }),
      ...(controller ? { signal: controller.signal } : {}),
    })
    if (timer) clearTimeout(timer)
  } catch (err) {
    // 规则 2：网络/超时一律 fail-open
    return { allowed: true, reason: 'backend-unreachable-fail-open' }
  }

  let body
  try { body = await res.json() }
  catch { return { allowed: true, reason: 'bad-response-fail-open' } }

  if (!body || typeof body !== 'object') {
    return { allowed: true, reason: 'bad-response-fail-open' }
  }
  // 规则 3：只有显式 allowed===false 才拦
  if (body.allowed === false) {
    return {
      allowed: false,
      used: body.used,
      remaining: body.remaining,
      limit: body.limit,
      reason: 'quota-exhausted',
    }
  }
  return {
    allowed: true,
    used: body.used,
    remaining: body.remaining,
    limit: body.limit,
    reason: 'within-quota',
  }
}

/** 额度用尽时的引导文案（l0 无 activate 工具，引导安装完整版） */
export function buildQuotaExceededMessage(info = {}) {
  const limit = info.limit ?? 5
  const contact = '13546196530@163.com'
  return (
    `今日免费额度已用尽（上限 ${limit} 次/天）。\n` +
    `请选择：\n` +
    `1) 明天再用（额度按自然日重置）；\n` +
    `2) 安装完整版 dsh-motor-ai 解锁无限调用；\n` +
    `3) 购买授权请联系 ${contact}。`
  )
}

/**
 * L0 工具执行前的统一闸门。返回 null 表示放行，否则返回可直接给用户的错误对象。
 * @param {object} config
 * @returns {Promise<null | {error: string, quota: object}>}
 */
export async function enforceQuota(config = {}) {
  if (config.freeQuotaEnabled === false) return null
  const r = await checkFreeQuota(config)
  if (r.allowed === false) {
    return { error: buildQuotaExceededMessage(r), quota: r }
  }
  return null
}

export default { checkFreeQuota, enforceQuota, buildQuotaExceededMessage, getDeviceId, DEFAULT_QUOTA_PLUGIN_ID }
