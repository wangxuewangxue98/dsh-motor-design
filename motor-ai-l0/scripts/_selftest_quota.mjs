/**
 * scripts/_selftest_quota.mjs —— 免费额度闸门运行时自检（v0.5.0 语义升级版）
 * ============================================================
 * 双通道：
 *  - 被 scripts/verify.mjs import 后顶层 await 预跑（verify 的 check() 是同步
 *    收集器，传 async fn 会静默假绿灯；顶层 await 预跑 + 同步读结果是唯一
 *    不骗人又不依赖 spawn 的通道——本机 node spawn 持续 EBUSY，子进程方案不可用）。
 *  - 也可直接 `node scripts/_selftest_quota.mjs` 独立跑（退出码 0/1）。
 *
 * v0.5.0 语义反转：闸门从「后端决定 + 断网放行」改为「本地为准的硬闸 + 非阻塞旁路遥测」。
 * 自检的**断言方向随之反转**，并新增延迟回归项：
 *   1) getDeviceId 算法与完整版逐字节一致（设备指纹不可分裂）—— 不变
 *   2) 未配置后端地址 → 不发任何网络请求
 *   3) 后端不可达 → 本地仍放行（本地为准）
 *   4) 后端返回 allowed:false → **不再拦截**（旧版会拦，这是本次要修的语义）
 *   5) freeQuotaEnabled=false 开关直接放行
 *   6) 本地额度耗尽 → 拦截（fail-closed，断网也拦）
 *   7) 拦截文案含"免费额度已用尽"引导
 *   8) 同一调用链（同一 config 对象）只计 1 次
 *   9) 不同 config 对象分别计数
 *  10) 跨天自动重置
 *  11) 非阻塞：后端挂死（fetch 永不 resolve）时 checkFreeQuota 不得等待 —— 延迟回归
 *  12) 配额文件路径与完整版激活态同目录同文件（两端共用一本账）
 *
 * v0.5.1（P0-门1修复）新增：
 *  13) 纯函数入口过闸：直调 buildParamMatrix 也计配额（旧版脱闸）
 *  14) runL0Pipeline 整链只扣 1 次（三个子纯函数同链不重复扣）
 *  15) freeQuotaEnabled=false 在纯函数入口同样生效（内部离线批量工具 bypass 依据）
 *  16) 拦截文案含联系邮箱 + 完整版下载引导（市场/npm）
 *
 * 隔离：全程使用临时 DSH_HOME，绝不污染用户真实配额文件。
 */
import {
  checkFreeQuota,
  enforceQuota,
  assertFreeQuota,
  newQuotaChain,
  withQuotaChain,
  getDeviceId,
  getQuotaFilePath,
} from '../lib/quota.mjs'
import { buildParamMatrix } from '../tools/l0/param-matrix.mjs'
import { runL0Pipeline } from '../tools/l0/pipeline.mjs'
import { createHash } from 'node:crypto'
import { hostname, tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { resolve, join } from 'node:path'
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync, existsSync } from 'node:fs'

export async function runQuotaSelfTest() {
  const fails = []
  const lines = []
  const t = (name, cond, extra = '') => {
    lines.push(`${cond ? '✅' : '❌'} ${name}${extra ? ' | ' + extra : ''}`)
    if (!cond) fails.push(name)
  }

  // ── 环境隔离：临时 DSH_HOME ──────────────────────────────
  const prev = {
    DSH_HOME: process.env.DSH_HOME,
    DSH_L0_QUOTA_FILE: process.env.DSH_L0_QUOTA_FILE,
    DSH_FREE_DAILY_LIMIT: process.env.DSH_FREE_DAILY_LIMIT,
    DSH_BACKEND_URL: process.env.DSH_BACKEND_URL,
  }
  const tmpHome = mkdtempSync(join(tmpdir(), 'l0-quota-selftest-'))
  const quotaFile = join(tmpHome, 'storages', 'dsh-motor-ai', 'free-quota.json')
  const origFetch = globalThis.fetch
  const restore = () => {
    globalThis.fetch = origFetch
    for (const k of Object.keys(prev)) {
      if (prev[k] === undefined) delete process.env[k]
      else process.env[k] = prev[k]
    }
  }
  /** 把配额态写成「今天、已用 free 次」（跨天/污染清理用） */
  const todayKey = () => {
    const d = new Date()
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  }
  const resetQuota = (free, limit = 10) => {
    mkdirSync(join(tmpHome, 'storages', 'dsh-motor-ai'), { recursive: true })
    writeFileSync(quotaFile, JSON.stringify({ day: todayKey(), free, limit }), 'utf-8')
  }
  /** 让 fire-and-forget 的旁路遥测落定，避免异步回写污染后续用例 */
  const flush = () => new Promise((r) => setTimeout(r, 30))

  try {
    delete process.env.DSH_L0_QUOTA_FILE
    delete process.env.DSH_BACKEND_URL
    delete process.env.DSH_FREE_DAILY_LIMIT
    process.env.DSH_HOME = tmpHome

    // 1) 算法一致性（事故级：两端设备指纹必须相同，否则后端额度账分裂）
    const expect = createHash('sha256')
      .update(`${hostname()}|${process.platform}|${process.arch}`, 'utf-8')
      .digest('hex').slice(0, 16)
    t('getDeviceId 与完整版算法一致', getDeviceId() === expect, `${getDeviceId()} == ${expect}`)

    // 12) 配额文件路径约定：与完整版 activation.json 同目录同文件名
    t(
      '配额文件路径与完整版激活态同目录（两端共用一本账）',
      getQuotaFilePath() === quotaFile && /storages[/\\]dsh-motor-ai[/\\]free-quota\.json$/.test(getQuotaFilePath()),
      getQuotaFilePath()
    )

    resetQuota(0)

    // 2) 未配置后端 → 零网络请求
    let fetchCalls = 0
    globalThis.fetch = async (...a) => { fetchCalls++; return origFetch(...a) }
    const rNoUrl = await checkFreeQuota({})
    t('未配置后端地址 → 不发网络请求', fetchCalls === 0, `fetchCalls=${fetchCalls}`)
    t('未配置后端时仍正常放行', rNoUrl.allowed === true && rNoUrl.used === 0, JSON.stringify(rNoUrl))

    // 3) 后端不可达（配了但没人监听）→ 本地为准，仍放行
    globalThis.fetch = origFetch
    const rDead = await checkFreeQuota({ commerceBackendUrl: 'http://127.0.0.1:59999' })
    t('后端不可达 → 本地仍放行（本地为准）', !!rDead && rDead.allowed === true, `reason=${rDead?.reason}`)

    // 4) 语义反转：后端 allowed:false 不再决定放行
    //    本地已用 5，后端也报 used=5 → 即便 allowed:false，也不该拦（且回写为 no-op）
    resetQuota(5)
    globalThis.fetch = async () => ({ json: async () => ({ allowed: false, limit: 10, used: 5, remaining: 5 }) })
    const rBackendDeny = await checkFreeQuota({ commerceBackendUrl: 'http://127.0.0.1:59998' })
    t('后端 allowed:false 不再拦截（本地为准，语义反转）', rBackendDeny.allowed === true, JSON.stringify(rBackendDeny))
    await flush()
    globalThis.fetch = origFetch

    // 5) 运维开关
    t('freeQuotaEnabled=false 直接放行', (await enforceQuota({ freeQuotaEnabled: false })) === null)

    // 6/7) 本地额度耗尽 → 拦截
    resetQuota(0, 2)
    process.env.DSH_FREE_DAILY_LIMIT = '2'
    const o1 = await enforceQuota({})          // 1/2
    const o2 = await enforceQuota({})          // 2/2
    const o3 = await enforceQuota({})          // 超限
    t('额度未耗尽时放行（1/2、2/2）', o1 === null && o2 === null)
    t(
      '本地额度耗尽（第 3 次 > 上限 2）→ 拦截（fail-closed，不依赖后端）',
      !!o3 && typeof o3.error === 'string' && o3.quota && o3.quota.allowed === false,
      JSON.stringify(o3)
    )
    t('拦截文案含"免费额度已用尽"引导', !!o3 && /免费额度已用尽/.test(o3.error))
    t('拦截文案显示实际用量 2/2', !!o3 && /2\/2/.test(o3.error), o3?.error?.split('\n')[0])
    delete process.env.DSH_FREE_DAILY_LIMIT

    // 8) 同一调用链只计 1 次（同步兄弟调用，未让出事件循环）
    //    v0.5.2：同链判据是**调用链令牌**（withQuotaChain 派生），不再看 config 对象引用
    resetQuota(0)
    const chainTok = newQuotaChain()
    assertFreeQuota('l0-estimate', withQuotaChain({}, chainTok))
    assertFreeQuota('design-validate', withQuotaChain({}, chainTok))
    assertFreeQuota('pipeline', withQuotaChain({}, chainTok))
    const after8 = await checkFreeQuota({})
    t('同一调用链（同令牌）只计 1 次（3 次兄弟调用 = +1）', after8.used === 1, `used=${after8.used}`)

    // 8b) P0-门1' 回归：同一 config 引用 + **同步循环** ⇒ 必须各计一次
    //     v0.5.1 用「config 引用 + 微任务复位」，同步循环里微任务不穿插 ⇒ 整批只扣 1 次。
    resetQuota(0)
    const sameCfg = {}
    for (let i = 0; i < 3; i++) assertFreeQuota('pipeline', sameCfg)
    const after8b = await checkFreeQuota({})
    t("P0-门1' 回归：同一 config 引用 + 同步循环 ×3 = +3（不得整批只扣 1 次）",
      after8b.used === 3, `used=${after8b.used}`)

    // 9) 不同 config 对象分别计数
    resetQuota(0)
    assertFreeQuota('l0-estimate', {})
    assertFreeQuota('l0-estimate', {})
    const a9 = await checkFreeQuota({})
    t('不同 config 对象分别计数（2 次独立调用 = +2）', a9.used === 2, `used=${a9.used}`)

    // 10) 跨天自动重置
    mkdirSync(join(tmpHome, 'storages', 'dsh-motor-ai'), { recursive: true })
    writeFileSync(quotaFile, JSON.stringify({ day: '2000-01-01', free: 9, limit: 10 }), 'utf-8')
    const rCross = await checkFreeQuota({})
    t('跨天自动重置（昨日 9/10 → 今日 0/10 放行）', rCross.allowed === true && rCross.used === 0, JSON.stringify(rCross))

    // 11) 延迟回归（本次修复核心）：后端挂死不得阻塞调用链
    //    旧版 checkFreeQuota 是 `await fetch` + 4s 超时 ⇒ 国内不可达后端会让每次调用白等 4s。
    globalThis.fetch = () => new Promise(() => { /* 永不 resolve：模拟 DNS 污染黑洞 */ })
    const t0 = Date.now()
    const r11 = await checkFreeQuota({ commerceBackendUrl: 'https://blackhole.invalid' })
    const dt11 = Date.now() - t0
    t('后端挂死时 checkFreeQuota 立即返回（<300ms，不等待旁路遥测）', r11.allowed === true && dt11 < 300, `elapsed=${dt11}ms`)
    const t2 = Date.now()
    const e11 = await enforceQuota({ commerceBackendUrl: 'https://blackhole.invalid' })
    const dt2 = Date.now() - t2
    t('enforceQuota 完全不触网（<50ms）', e11 === null && dt2 < 50, `elapsed=${dt2}ms`)
    globalThis.fetch = origFetch

    // ── v0.5.1 P0-门1 修复项 ──────────────────────────────
    // 13) 纯函数入口过闸：直调 buildParamMatrix 也计配额（此前脱闸，探针实测复现）
    resetQuota(0)
    const m13 = buildParamMatrix(
      { power_kw: 15, speed_rpm: 1460, voltage_v: 380, motor_type: 'induction', count: 8 },
      { maxMatrixSize: 2000 }
    )
    const u13 = (await checkFreeQuota({})).used
    t('P0-门1：直调纯函数 buildParamMatrix 也计配额（不再脱闸）',
      m13?.matrix?.length > 0 && u13 === 1, `used=${u13}`)

    // 14) pipeline 整链只扣 1 次（三个子纯函数同链，靠一次性消费标记不重复扣）
    resetQuota(0)
    const p14 = runL0Pipeline(
      { power_kw: 15, speed_rpm: 1460, voltage_v: 380, motor_type: 'induction', count: 8 },
      { handoffDir: join(tmpHome, 'handoff-selftest') }
    )
    const u14 = (await checkFreeQuota({})).used
    t('P0-门1：runL0Pipeline 整链只扣 1 次（三子函数同链不重复扣）',
      p14?.summary?.generated > 0 && u14 === 1, `used=${u14}`)

    // 15) 运维开关在纯函数入口同样生效（内部离线批量工具 experience-hub/case-feedback 据此 bypass）
    resetQuota(0)
    buildParamMatrix(
      { power_kw: 15, speed_rpm: 1460, count: 8 },
      { maxMatrixSize: 2000, freeQuotaEnabled: false }
    )
    const u15 = (await checkFreeQuota({})).used
    t('freeQuotaEnabled=false 在纯函数入口同样放行不计数', u15 === 0, `used=${u15}`)

    // 16) 额度用尽文案：联系邮箱 + 完整版下载引导（用户核心诉求）
    resetQuota(0)
    process.env.DSH_FREE_DAILY_LIMIT = '1'
    await enforceQuota({})             // 1/1 放行
    const o16 = await enforceQuota({}) // 超限 → 拦截
    delete process.env.DSH_FREE_DAILY_LIMIT
    t('额度用尽文案含联系邮箱（反馈/购买）',
      !!o16 && /13546196530@163\.com/.test(o16.error), o16?.error?.split('\n')[0] || '')
    t('额度用尽文案含完整版下载引导（DSH 市场 + npm install）',
      !!o16 && /完整版/.test(o16.error) && /npm install/.test(o16.error))

    // 收尾：确认确实落盘（而非只写内存）
    t(
      '配额状态确实落盘到 free-quota.json',
      existsSync(quotaFile) && !!JSON.parse(readFileSync(quotaFile, 'utf-8')).day
    )
  } finally {
    restore()
    try { rmSync(tmpHome, { recursive: true, force: true }) } catch { /* 清理失败无害 */ }
  }

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
