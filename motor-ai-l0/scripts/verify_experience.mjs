/**
 * design_exp 回流链路的「时序级」自检（异步，由 scripts/verify.mjs 用 execFileSync 调用）。
 *
 * 存在的理由（v0.2.9 事故）：纯函数门禁全绿，但真实回流链路是坏的：
 *   ① `export { readPendingRecords } from './telemetry.mjs'` 只转发、不绑定到本地
 *      作用域 ⇒ reportExperience 内直接调用抛 ReferenceError，上报必崩；
 *   ② buildDesignExp 未写 ts ⇒ gcExperience 按 `ts || 0` 判定 ⇒ 距今远超 48h
 *      ⇒ 全批样本被当过期清空 ⇒ 上报恒为 no-pending。
 * 两者都是「单个函数测试全过、串起来才暴露」的时序/语义缺陷，必须端到端跑。
 *
 * 做法：起临时 HTTP server 当 endpoint，真实跑 落盘 → GC → 增量读 → POST，
 * 校验服务端确实收到合规样本（含 ts / plugin_version / 白名单裁剪）。
 */
import http from 'node:http'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const SRC = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..')
const imp = (rel) => import(pathToFileURL(path.join(SRC, rel)).href)

const must = (cond, msg) => { if (!cond) { console.error('❌ ' + msg); process.exit(1) } }

// 临时 DSH_HOME，绝不污染真实 storage
const tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), 'vexp_'))
process.env.DSH_HOME = tmpHome

const XU = await imp('lib/experience-upload.mjs')

// ---- ① ts 必带 + GC 不得清空（事故 ② 回归闸门）----
const row = {
  params: {
    power_kw: 15, speed: 1460, voltage: 380, poles: 4,
    stator_od: 308, stator_id: 186, core_length: 167.4, air_gap: 0.58,
    slots_stator: 24, slots_rotor: 22, cooling: 'forced_air', motor_type: 'induction',
  },
  line_freq_hz: 50, insulation_class: 'F',
  efficiency_raw: 94.2, temp_rise: 25.5, total_loss: 844.96,
  feasible: true, verdict: 'feasible', confidence: 0.5,
}
const rec = XU.buildDesignExp(row, {})
must(typeof rec.ts === 'string' && !Number.isNaN(Date.parse(rec.ts)),
  `buildDesignExp 未写合法 ts（缺 ts 会被 48h GC 全批误清空）: ${JSON.stringify(rec.ts)}`)
must(String(rec.plugin_version || '') !== '',
  'plugin_version 为空 — 服务端 by_version 画像无法按版本归因')

// ---- ② 起临时 endpoint ----
let received = null
const server = http.createServer((req, res) => {
  let body = ''
  req.on('data', (c) => { body += c })
  req.on('end', () => {
    try { received = JSON.parse(body) } catch { received = null }
    res.writeHead(202, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ accepted: true }))
  })
})
await new Promise((r) => server.listen(0, '127.0.0.1', r))
const port = server.address().port
const endpoint = `http://127.0.0.1:${port}/experience-report`

// ---- ③ 落盘 → GC → 上报（事故 ① 回归闸门：崩了就收不到）----
const config = { designExperienceUpload: true, experienceEndpoint: endpoint, experienceBatchSize: 200 }
const sink = XU.getExperienceSink(config)
must(sink?.file, 'getExperienceSink 返回 null（开关判定异常）')

XU.logExperience(sink, rec)
XU.logExperience(sink, XU.buildDesignExp({ ...row, params: { ...row.params, poles: 6 } }, {}))
const before = fs.readFileSync(sink.file, 'utf8').split('\n').filter(Boolean).length
const gc = XU.gcExperience(sink)
must(gc && gc.kept === before && gc.dropped === 0,
  `48h GC 误清空新样本（kept=${gc?.kept} dropped=${gc?.dropped} 期望 kept=${before} dropped=0）`)

const r = await XU.reportExperience(config, { pluginVersion: '0.2.10' })
must(r.reported === true && r.samples >= 1,
  `reportExperience 未成功上报: ${JSON.stringify(r)}（若 reason 异常多为 readPendingRecords 作用域问题）`)
must(received && Array.isArray(received.records) && received.records.length >= 1,
  'endpoint 未收到 records（上报链路中断）')

const r0 = received.records[0]
must(r0.rec_type === 'design_exp' && typeof r0.rec_id === 'string', '样本缺 rec_type/rec_id')
must(!Number.isNaN(Date.parse(r0.ts)), '上报样本缺合法 ts')
must(Object.keys(r0).every((k) => !/cwd|client_id|prompt|api_key|token|path/i.test(k)),
  '上报样本出现身份类键（脱敏失效）')

server.close()
try { fs.rmSync(tmpHome, { recursive: true, force: true }) } catch { /* 临时目录清理失败无害 */ }

console.log(`✅ design_exp 回流链路自检通过：上报 ${r.samples} 条，服务端收到 ${received.records.length} 条，`
  + `ts 合法、plugin_version=${JSON.stringify(r0.plugin_version)}、GC kept=${gc.kept}/dropped=0`)
