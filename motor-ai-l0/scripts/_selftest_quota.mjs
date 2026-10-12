/**
 * scripts/_selftest_quota.mjs —— 免费额度闸门运行时自检
 * ============================================================
 * 双通道：
 *  - 被 scripts/verify.mjs import 后顶层 await 预跑（verify 的 check() 是同步
 *    收集器，传 async fn 会静默假绿灯；顶层 await 预跑 + 同步读结果是唯一
 *    不骗人又不依赖 spawn 的通道——本机 node spawn 持续 EBUSY，子进程方案不可用）。
 *  - 也可直接 `node scripts/_selftest_quota.mjs` 独立跑（退出码 0/1）。
 *
 * 覆盖（对应完整版门禁 H 的 l0 等价项）：
 *   1) getDeviceId 算法与完整版逐字节一致（设备指纹不可分裂）
 *   2) 断网 fail-open（后端不可达 → 放行）
 *   3) freeQuotaEnabled=false 开关直接放行
 *   4) 后端显式 allowed===false 才拦截（mock fetch）
 *   5) 拦截文案含"免费额度已用尽"引导
 */
import { checkFreeQuota, enforceQuota, getDeviceId } from '../lib/quota.mjs'
import { createHash } from 'node:crypto'
import { hostname } from 'node:os'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

export async function runQuotaSelfTest() {
  const fails = []
  const lines = []
  const t = (name, cond, extra = '') => {
    lines.push(`${cond ? '✅' : '❌'} ${name}${extra ? ' | ' + extra : ''}`)
    if (!cond) fails.push(name)
  }

  // 1) 算法一致性（事故级：两端设备指纹必须相同，否则后端额度账分裂）
  const expect = createHash('sha256')
    .update(`${hostname()}|${process.platform}|${process.arch}`, 'utf-8')
    .digest('hex').slice(0, 16)
  t('getDeviceId 与完整版算法一致', getDeviceId() === expect, `${getDeviceId()} == ${expect}`)

  // 2) fail-open：指向一个必然无人监听的端口
  const r = await checkFreeQuota({ commerceBackendUrl: 'http://127.0.0.1:59999' })
  t('断网 fail-open（不可达 → 放行）', !!r && r.allowed === true, `reason=${r?.reason}`)

  // 3) 运维开关
  t('freeQuotaEnabled=false 直接放行', (await enforceQuota({ freeQuotaEnabled: false })) === null)

  // 4) 显式 allowed===false 才拦截
  const origFetch = globalThis.fetch
  globalThis.fetch = async () => ({ json: async () => ({ allowed: false, limit: 5, used: 5, remaining: 0 }) })
  let e
  try {
    e = await enforceQuota({})
  } finally {
    globalThis.fetch = origFetch
  }
  t('allowed===false 返回拦截对象', !!e && typeof e.error === 'string' && !!e.quota && e.quota.allowed === false)
  t('拦截文案含"免费额度已用尽"引导', !!e && /免费额度已用尽/.test(e.error))

  return { ok: fails.length === 0, fails, lines }
}

// 仅当作为主模块直接运行时自跑（被 import 时不执行，避免副作用）
const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)
if (isMain) {
  const r = await runQuotaSelfTest()
  r.lines.forEach(l => console.log(l))
  console.log(r.ok ? 'QUOTA_SELFTEST_ALL_OK' : `QUOTA_SELFTEST_FAIL: ${r.fails.join(', ')}`)
  process.exit(r.ok ? 0 : 1)
}
