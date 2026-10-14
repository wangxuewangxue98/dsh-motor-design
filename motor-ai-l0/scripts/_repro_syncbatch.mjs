/**
 * scripts/_repro_syncbatch.mjs —— P0-门1' 最小复现：同步批量调用脱闸
 * ============================================================================
 * 现象：v0.5.1 声称「纯函数入口过闸」，但对**同一 config 引用的同步批量循环**
 * 整批只扣 1 次配额，门禁形同虚设；而每次新建 config 字面量时又正常拦截。
 * 差异来源：lib/quota.mjs 的「调用链一次性消费」用
 *   _consumed(WeakSet, key=config 对象) + queueMicrotask 复位
 * 实现。同步 for 循环里微任务不穿插执行 ⇒ 第 2~N 次 alreadyConsumed() 恒 true。
 *
 * 本脚本逐形态实测（全部用临时 DSH_HOME 隔离，不触碰真实配额文件）：
 *   [A] 同一 config 引用 + 同步循环 ×12   ← CI/压测典型写法
 *   [A2] 每次新建 config 字面量 + 同步循环 ×12 ← 现役探针 quota_probe_0.5.1.mjs 的写法
 *   [B] 同一 config 引用 + 每次 await 微任务 ×12
 *   [C] freeQuotaEnabled:false 绕过
 *   [D] 真实三步链 runL0Pipeline 一次 = 1 次配额（设计意图，不能回归成 4 次）
 *
 * 运行：node scripts/_repro_syncbatch.mjs
 * 退出码：0 = 缺陷已修复（A 也被拦）；1 = 缺陷仍在（A 整批放行）
 */
import { assertFreeQuota, QuotaExceededError, getQuotaFilePath } from '../lib/quota.mjs'
import { runL0Pipeline } from '../tools/l0/pipeline.mjs'
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

const tmpHome = mkdtempSync(join(tmpdir(), 'l0-repro-syncbatch-'))
const prev = { DSH_HOME: process.env.DSH_HOME, DSH_L0_QUOTA_FILE: process.env.DSH_L0_QUOTA_FILE, DSH_BACKEND_URL: process.env.DSH_BACKEND_URL }
delete process.env.DSH_L0_QUOTA_FILE
delete process.env.DSH_BACKEND_URL
process.env.DSH_HOME = tmpHome

const qf = () => getQuotaFilePath()
const dayKey = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}` }
const reset = (free = 0, limit = 10) => {
  mkdirSync(join(tmpHome, 'storages', 'dsh-motor-ai'), { recursive: true })
  writeFileSync(qf(), JSON.stringify({ day: dayKey(), free, limit }), 'utf-8')
}
const used = () => { try { return Number(JSON.parse(readFileSync(qf(), 'utf-8')).free ?? 0) } catch { return -1 } }
/** macrotask 边界：让 quota.mjs 的微任务复位真正跑到 */
const tick = () => new Promise((r) => setImmediate(r))

const results = []
const rec = (name, blocked, pass, consumed, note) => {
  results.push({ name, blocked, pass, consumed, note })
}

try {
  // ── [A] 同一 config 引用 + 同步循环 ×12（CI/压测典型写法）──────────
  reset(0)
  {
    const cfg = { handoffDir: join(tmpHome, 'hh') }
    let pass = 0, blocked = 0
    for (let i = 0; i < 12; i++) {
      try { assertFreeQuota('pipeline', cfg); pass++ } catch (e) { if (e instanceof QuotaExceededError) blocked++; else throw e }
    }
    rec('[A] 同一 config + 同步循环 ×12', blocked, pass, used(), '期望 blocked=2（第 11/12 次拦）；blocked=0 = 脱闸')
  }

  // ── [A2] 每次新建 config 字面量 + 同步循环 ×12（现役探针写法）────────
  await tick(); reset(0)
  {
    let pass = 0, blocked = 0
    for (let i = 0; i < 12; i++) {
      try { assertFreeQuota('pipeline', { handoffDir: join(tmpHome, 'hh') }); pass++ } catch (e) { blocked++ }
    }
    rec('[A2] 新 config 字面量 + 同步循环 ×12', blocked, pass, used(), '对照：期望 blocked=2')
  }

  // ── [B] 同一 config 引用 + 每次 await 微任务 ×12 ────────────────────
  await tick(); reset(0)
  {
    const cfg = { handoffDir: join(tmpHome, 'hh') }
    let pass = 0, blocked = 0
    for (let i = 0; i < 12; i++) {
      try { assertFreeQuota('pipeline', cfg); pass++ } catch (e) { blocked++ }
      await Promise.resolve() // 微任务边界 → 触发 _consumed 复位
    }
    rec('[B] 同一 config + 每次 await 微任务 ×12', blocked, pass, used(), '期望 blocked≈2')
  }

  // ── [C] freeQuotaEnabled:false 绕过 ────────────────────────────────
  await tick(); reset(9)
  {
    let pass = 0, blocked = 0
    for (let i = 0; i < 12; i++) {
      try { assertFreeQuota('pipeline', { freeQuotaEnabled: false }); pass++ } catch (e) { blocked++ }
    }
    rec('[C] freeQuotaEnabled:false ×12', blocked, pass, used(), '期望 blocked=0 且 consumed=0')
  }

  // ── [D] 真实三步链一次 = 1 次配额（防回归：不能扣成 4 次）────────────
  await tick(); reset(0)
  {
    const r = runL0Pipeline({ power_kw: 15, speed_rpm: 1460, voltage_v: 380, motor_type: 'induction', count: 8 },
      { handoffDir: join(tmpHome, 'hh') })
    rec('[D] runL0Pipeline 整链一次', 0, r?.summary?.generated > 0 ? 1 : 0, used(), '期望 consumed=1')
  }
} finally {
  for (const k of Object.keys(prev)) { if (prev[k] === undefined) delete process.env[k]; else process.env[k] = prev[k] }
  try { rmSync(tmpHome, { recursive: true, force: true }) } catch { /* noop */ }
}

console.log('')
console.log('形态                                      blocked  pass  consumed   期望')
console.log('----------------------------------------  -------  ----  --------  ----')
for (const r of results) {
  console.log(`${r.name.padEnd(40)}  ${String(r.blocked).padStart(7)}  ${String(r.pass).padStart(4)}  ${String(r.consumed).padStart(8)}   ${r.note}`)
}
const a = results[0]
const defect = a.blocked === 0
console.log('')
console.log(defect
  ? `REPRO_CONFIRMED: [A] 同步批量整批放行（pass=${a.pass}, consumed=${a.consumed}）⇒ P0-门1' 缺陷成立`
  : `REPRO_FIXED: [A] 同步批量已被拦（blocked=${a.blocked}, consumed=${a.consumed}）`)
process.exit(defect ? 1 : 0)
