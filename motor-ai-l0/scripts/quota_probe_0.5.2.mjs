/**
 * scripts/quota_probe_0.5.2.mjs —— 有限次门禁可复现实测（针对 dsh-motor-ai-l0 v0.5.2）
 * ============================================================================
 * 背景（两级修复，勿混淆）：
 *   - v0.5.0：P0-门1 —— 闸门只装在 registerX.execute，脚本/CI import 直调四个纯函数
 *     零限制跑完（探针 quota_probe_0.5.0.mjs 复现：直调 ×12 本地 free 恒=0）。
 *   - v0.5.1：四个纯函数入口注入 assertFreeQuota；但「同链只扣 1 次」用
 *     「_consumed WeakSet(key=config 对象) + queueMicrotask 复位」实现 ⇒ **同步批量
 *     循环复用同一 config 引用时整批只扣 1 次**（P0-门1'，缺陷转移）。
 *     注：v0.5.1 探针 A 组每次新建 `{ handoffDir }` 字面量，恰好绕过了该缺陷 ⇒ 假绿灯。
 *   - v0.5.2：同链判据改为**显式调用链令牌**（newQuotaChain / withQuotaChain），
 *     聚合入口每次调用新建唯一令牌 ⇒ 同步循环各计一次，整链仍只扣 1 次。
 *
 * 本脚本逐条核验：
 *   A) 纯函数直调（每次新建 config 字面量）×12 → 10 放行 + 2 拦，free=10
 *   A-same) **同一 config 引用 + 同步循环 ×12 → 同样 10 放行 + 2 拦**（v0.5.2 回归核心，
 *           v0.5.1 此处为 12 放行 / free=1）
 *   A2) buildParamMatrix 直调同样计配额
 *   F) 防回归：pipeline 整链只扣 1 次（不能因加闸把一次调用扣成 4 次）
 *   B) 硬闸生效：enforceQuota ×N 在第 limit+1 次被拦（fail-closed）
 *   C) 运维开关：freeQuotaEnabled=false 直接放行且不计数
 *   D) 离线 fail-closed + 无 4s 阻塞
 *   E) 软限制残留：删 free-quota.json 即重置（0.5.x 诚实边界已自承）
 *   G) 指纹算法 3 字段（仅遥测用、不参与放行）
 *   H) 拦截文案含联系邮箱 + 完整版下载引导
 *
 * 隔离：全程用临时 DSH_HOME，绝不触碰用户真实 free-quota.json。
 *
 * 运行：node scripts/quota_probe_0.5.2.mjs
 * 退出码：0 = 全部符合预期；1 = 有断言失败。
 */
import { enforceQuota, getQuotaFilePath, getDeviceId } from '../lib/quota.mjs'
import { runL0Pipeline } from '../tools/l0/pipeline.mjs'
import { buildParamMatrix } from '../tools/l0/param-matrix.mjs'
import { createHash } from 'node:crypto'
import { hostname, tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { resolve, join } from 'node:path'
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync } from 'node:fs'

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
const tmpHome = mkdtempSync(join(tmpdir(), 'l0-quota-probe-'))
const restore = () => {
  for (const k of Object.keys(prev)) {
    if (prev[k] === undefined) delete process.env[k]
    else process.env[k] = prev[k]
  }
}
const todayKey = () => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
const quotaFile = () => getQuotaFilePath()
const resetQuota = (free, limit = 10) => {
  mkdirSync(join(tmpHome, 'storages', 'dsh-motor-ai'), { recursive: true })
  writeFileSync(quotaFile(), JSON.stringify({ day: todayKey(), free, limit }), 'utf-8')
}
const readUsed = () => {
  try { return Number(JSON.parse(readFileSync(quotaFile(), 'utf-8')).free ?? 0) } catch { return -1 }
}
/** macrotask 边界：确保各组之间不残留同链标记 */
const tick = () => new Promise((r) => setImmediate(r))

try {
  delete process.env.DSH_L0_QUOTA_FILE
  delete process.env.DSH_BACKEND_URL
  delete process.env.DSH_FREE_DAILY_LIMIT
  process.env.DSH_HOME = tmpHome

  // ── 环境校验 ───────────────────────────────────────────
  t('探针隔离生效：配额文件落在临时 DSH_HOME', quotaFile().startsWith(tmpHome), quotaFile())

  const handoffDir = join(tmpHome, 'l0-handoff')
  const spec = { power_kw: 15, speed_rpm: 1460, voltage_v: 380, motor_type: 'induction', count: 24 }

  // ── A) P0-门1 已修：纯函数直调被闸门捕获（每次新建 config 字面量）──
  resetQuota(0)
  let aOk = 0, aThrows = 0, aQuotaThrow = 0
  for (let i = 0; i < 12; i++) {
    try {
      const r = runL0Pipeline(spec, { handoffDir })
      if (r && typeof r.summary === 'object') aOk++
    } catch (e) {
      aThrows++
      if (/免费额度已用尽/.test(String(e?.message ?? ''))) aQuotaThrow++
      else lines.push(`   runL0Pipeline#${i} threw NON-quota: ${e.message}`)
    }
  }
  const aUsed = readUsed()
  t('A) runL0Pipeline 直调 ×12 = 前 10 次放行 + 第 11/12 次拦', aOk === 10 && aThrows === 2, `ok=${aOk} throws=${aThrows}`)
  t('A1) 第 11/12 次抛的是 QuotaExceededError（文案含"免费额度已用尽"）', aQuotaThrow === 2, `quotaThrows=${aQuotaThrow}`)
  t('A2) 直调纯函数确实计配额（本地 free=10）', aUsed === 10, `free=${aUsed}`)

  // ── A-same) v0.5.2 回归核心：同一 config 引用 + **同步循环** ──────────
  // v0.5.1 用「config 引用 + 微任务复位」判同链 ⇒ 同步循环里微任务不穿插，
  // 第 2~N 次恒判「已消费」⇒ 整批只扣 1 次、12 次全放行（scripts/_repro_syncbatch.mjs）。
  // v0.5.2 改用调用链令牌 ⇒ 每次顶层调用令牌唯一 ⇒ 必须同样在第 11 次被拦。
  await tick(); resetQuota(0)
  const sameCfg = { handoffDir }
  let sOk = 0, sThrows = 0
  for (let i = 0; i < 12; i++) {
    try {
      const r = runL0Pipeline(spec, sameCfg) // ← 同一 config 引用，同步循环
      if (r && typeof r.summary === 'object') sOk++
    } catch (e) {
      sThrows++
      if (!/免费额度已用尽/.test(String(e?.message ?? ''))) lines.push(`   sameCfg#${i} threw NON-quota: ${e.message}`)
    }
  }
  const sUsed = readUsed()
  t('A-same) 同一 config 引用 + 同步循环 ×12 同样被拦（10 放行 + 2 拦）',
    sOk === 10 && sThrows === 2, `ok=${sOk} throws=${sThrows}`)
  t("A-same2) 同步批量确实各计一次（free=10，v0.5.1 此处 free=1 ⇒ P0-门1'）",
    sUsed === 10, `free=${sUsed}`)

  // ── A3) buildParamMatrix 直调同样计配额 ────────────────
  await tick(); resetQuota(0)
  const a3 = buildParamMatrix({ power_kw: 15, speed_rpm: 1460, count: 8 }, { maxMatrixSize: 2000 })
  t('A3) buildParamMatrix 直调也计配额（+1）', a3?.matrix?.length > 0 && readUsed() === 1, `free=${readUsed()}`)

  // ── F) 防回归：pipeline 整链只扣 1 次 ──────────────────
  await tick(); resetQuota(0)
  const f1 = runL0Pipeline({ power_kw: 15, speed_rpm: 1460, voltage_v: 380, motor_type: 'induction', count: 12 }, { handoffDir })
  const fUsed = readUsed()
  t('F) 防回归：runL0Pipeline 整链只扣 1 次（三子函数同链不重复扣）',
    f1?.summary?.generated > 0 && fUsed === 1, `free=${fUsed}`)

  // ── B) 硬闸生效（证 fail-closed）──────────────────────
  await tick(); resetQuota(0, 10)
  const bPass = []
  for (let i = 0; i < 11; i++) bPass.push((await enforceQuota({})) === null)
  const blocked = (await enforceQuota({})) !== null
  t('B) 硬闸生效：前 10 次放行', bPass.slice(0, 10).every(Boolean))
  t('B2) 第 11 次（>上限 10）被拦（fail-closed，不依赖后端）', blocked)
  const lastErr = await enforceQuota({}) // 此时已超限
  t('B3) 拦截文案含"免费额度已用尽"引导', !!lastErr && /免费额度已用尽/.test(lastErr.error), lastErr?.error?.split('\n')[0] || '')

  // ── H) 拦截文案：联系制作者 + 下载完整版 ─────────────────
  t('H1) 拦截文案含联系邮箱（反馈/购买）', !!lastErr && /13546196530@163\.com/.test(lastErr.error))
  t('H2) 拦截文案含完整版下载引导（DSH 市场 + npm install）',
    !!lastErr && /完整版/.test(lastErr.error) && /npm install/.test(lastErr.error))

  // ── C) 运维开关 ────────────────────────────────────────
  await tick(); resetQuota(9, 10) // 本地已 9/10，但开关关掉应直接放行
  const c = await enforceQuota({ freeQuotaEnabled: false })
  t('C) freeQuotaEnabled=false 直接放行（即使本地已 9/10）', c === null)
  t('C2) freeQuotaEnabled=false 不计数（仍为 9）', readUsed() === 9, `free=${readUsed()}`)

  // ── D) 离线 fail-closed + 无 4s 阻塞 ──────────────────
  await tick(); resetQuota(0, 10)
  const dStart = Date.now()
  let dNulls = 0
  for (let i = 0; i < 10; i++) if ((await enforceQuota({})) === null) dNulls++
  const dBlocked = (await enforceQuota({})) !== null
  const dElapsed = Date.now() - dStart
  t('D) 离线（无后端）前 10 次放行', dNulls === 10, `nulls=${dNulls}`)
  t('D2) 离线仍在第 11 次拦截（fail-closed）', dBlocked)
  t('D3) 无后端时单次 enforceQuota 不触网（整批 11 次 <300ms，证 4s 阻塞已修）', dElapsed < 300, `11 calls elapsed=${dElapsed}ms`)

  // ── E) 软限制残留（0.5.x 诚实边界已自承）──────────────
  await tick(); resetQuota(10, 10) // 已用尽
  const eBefore = (await enforceQuota({})) !== null // 应拦
  rmSync(quotaFile(), { force: true })
  const eAfter = (await enforceQuota({})) === null // 删文件后重置 → 放行
  t('E) 软限制残留：已用尽时被拦', eBefore)
  t('E2) 软限制残留：删除 free-quota.json 即清零放行（技术用户可绕过）', eAfter)

  // ── G) 指纹算法核对（与完整版逐字节一致，3 字段）──────────
  const expect = createHash('sha256').update(`${hostname()}|${process.platform}|${process.arch}`, 'utf-8').digest('hex').slice(0, 16)
  t('G) 指纹仍为 3 字段 hostname|platform|arch（仅用于遥测同步、不参与放行）', getDeviceId() === expect, getDeviceId())
} finally {
  restore()
  try { rmSync(tmpHome, { recursive: true, force: true }) } catch { /* 清理失败无害 */ }
}

lines.forEach(l => console.log(l))
console.log('')
console.log(fails.length === 0 ? 'QUOTA_PROBE_0.5.2_ALL_OK' : `QUOTA_PROBE_FAIL: ${fails.join(' | ')}`)
process.exit(fails.length === 0 ? 0 : 1)
