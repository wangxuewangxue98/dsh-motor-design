/**
 * scripts/release.mjs —— W6 发布门禁（三重一致性）
 * =====================================================================
 * 「三重一致性」定义（v3 定案① P1 真源政策）：
 *   真源在 Python 侧，JS 侧 lib/motor-constants.mjs 是构建期生成产物，
 *   构建产物 knowledge-sync/constants.json 由 sync-constants.py 导出。
 *   三者必须两两一致、且构建产物新鲜（非 stale）：
 *     [1] Python    physics_kernel.py  —— 真源
 *     [2] JS        lib/motor-constants.mjs —— 运行时
 *     [3] 构建产物  knowledge-sync/constants.json —— 导出门禁凭证
 *
 * 门禁步骤：
 *   G1  P1 同步漂移检测：python sync-constants.py --check（[1]↔[2]）
 *   G2  构建产物新鲜度：emit 后重读 fingerprint，与当前指纹比对（[3] 由 [1] 现导）
 *   G3  版本三重一致性：package.json.version = SKILL.md metadata.version = CHANGELOG 最新条目
 *   G4  发布前置：node scripts/verify.mjs 全过 + node scripts/regression.mjs 非阻断通过
 *
 * 退出码：默认打印报告并 exit 0；--check 模式下 triple_ok=False 则 exit 1。
 *
 * 用法：
 *   node scripts/release.mjs                 # 打印发布门禁报告
 *   node scripts/release.mjs --check         # 门禁未过则 exit 1（CI / 发布卡口）
 *   node scripts/release.mjs --plugin motor-ai-l0
 *        # 门禁通过后顺带把 triple_ok 回写注册表（需 Core/dsh_registry.py 可用）
 */
import { readFileSync, existsSync } from 'node:fs'
import { resolve, dirname, join } from 'node:path'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const results = []
let failed = 0

function check(name, ok, detail) {
  if (!ok) failed += 1
  results.push({ name, ok, detail: detail || '' })
}

function sh(cmd, args, opts = {}) {
  return execFileSync(cmd, args, { encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'], ...opts })
}

function findPython() {
  for (const c of ['python3', 'python']) {
    try {
      sh(c, ['--version'])
      return c
    } catch { /* try next */ }
  }
  return 'python'
}

const PY = findPython()
const NODE = process.execPath

// ------------------------------------------------------------------ G1
function gateSyncDrift() {
  try {
    const out = sh(PY, [join('knowledge-sync', 'sync-constants.py'), '--check', '--json'],
      { cwd: ROOT })
    const report = JSON.parse(out)
    const ok = (report.drift_count || 0) === 0 && (report.missing || []).length === 0
    return {
      ok,
      detail: ok
        ? `drift ${report.drift_count} / missing ${report.missing?.length || 0}`
        : `drift ${report.drift_count} / missing ${(report.missing || []).join(', ')}`,
      fingerprint: report.fingerprint,
    }
  } catch (e) {
    return { ok: false, detail: `sync-constants.py 失败: ${String(e.stdout || e.message).slice(0, 200)}` }
  }
}

const g1 = gateSyncDrift()
check('G1 P1 同步漂移检测（Python↔JS 0 diff）', g1.ok, g1.detail)

// ------------------------------------------------------------------ G2
function gateArtifactFresh() {
  try {
    // 重新 emit，确保 constants.json 由当前真源现导
    sh(PY, [join('knowledge-sync', 'sync-constants.py'), '--emit', '--json'],
      { cwd: ROOT })
    const emitted = JSON.parse(readFileSync(join('knowledge-sync', 'constants.json'), 'utf8'))
    const cur = JSON.parse(sh(PY, [join('knowledge-sync', 'sync-constants.py'), '--json'],
      { cwd: ROOT }))
    const ok = emitted.fingerprint === cur.fingerprint
    return { ok, detail: ok ? `fingerprint ${emitted.fingerprint}` : 'constants.json 与当前真源指纹不一致（stale）' }
  } catch (e) {
    return { ok: false, detail: `构建产物新鲜度检测失败: ${String(e.message).slice(0, 200)}` }
  }
}
const g2 = gateArtifactFresh()
check('G2 构建产物新鲜度（constants.json 由当前真源现导）', g2.ok, g2.detail)

// ------------------------------------------------------------------ G3
function gateVersionTriple() {
  const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'))
  const v = pkg.version
  let skillV = null
  const skillPath = join(ROOT, 'skills', 'motor-l0-estimate', 'SKILL.md')
  if (existsSync(skillPath)) {
    skillV = readFileSync(skillPath, 'utf8').match(/version:\s*([\d.]+)/)?.[1] || null
  }
  const cl = readFileSync(join(ROOT, 'CHANGELOG.md'), 'utf8')
  const clV = cl.match(/##\s*\[([\d.]+)\]/)?.[1] || null
  const ok = v === skillV && v === clV && skillV !== null
  const parts = [`pkg=${v}`, `skill=${skillV ?? '—'}`, `changelog=${clV ?? '—'}`]
  return { ok, detail: ok ? parts.join(' ') : `不一致: ${parts.join(' ')}` }
}
const g3 = gateVersionTriple()
check('G3 版本三重一致性（package.json = SKILL.md = CHANGELOG）', g3.ok, g3.detail)

// ------------------------------------------------------------------ G4
function gatePreRelease() {
  const log = []
  try {
    sh(NODE, [join('scripts', 'verify.mjs')], { cwd: ROOT })
    log.push('verify ✅')
  } catch (e) {
    log.push(`verify ❌: ${String(e.stdout || '').split('\n').filter(l => l.includes('❌')).slice(-2).join('; ')}`)
  }
  try {
    sh(NODE, [join('scripts', 'regression.mjs')], { cwd: ROOT })
    log.push('regression ✅')
  } catch (e) {
    log.push(`regression ❌: ${String(e.stdout || '').slice(0, 120)}`)
  }
  const ok = log.every((s) => s.includes('✅'))
  return { ok, detail: log.join(' | ') }
}
const g4 = gatePreRelease()
check('G4 发布前置（verify + regression 全过）', g4.ok, g4.detail)

// ------------------------------------------------------------------ 汇总
const tripleOk = g1.ok && g2.ok && g3.ok && g4.ok
const summary = `G1=${g1.ok ? 'OK' : 'FAIL'} G2=${g2.ok ? 'OK' : 'FAIL'} G3=${g3.ok ? 'OK' : 'FAIL'} G4=${g4.ok ? 'OK' : 'FAIL'}`

const report = {
  schema: 'l0-release-gate/1',
  triple_ok: tripleOk,
  generated_at: new Date().toISOString(),
  gates: { g1, g2, g3, g4 },
  summary,
}
const outPath = join(ROOT, 'knowledge-sync', 'release-report.json')
import('node:fs').then((fs) => fs.writeFileSync(outPath,
  JSON.stringify(report, null, 2), 'utf8'))

// 可选：门禁通过后回写注册表 release_gate.triple_ok
const pluginArg = process.argv.includes('--plugin')
  ? process.argv[process.argv.indexOf('--plugin') + 1]
  : null
if (tripleOk && pluginArg) {
  try {
    const reg = sh(PY, ['-c',
      `import sys; sys.path.insert(0,'Core'); import dsh_registry as r; ` +
      `print(r.set_release_gate(${JSON.stringify(pluginArg)}, True, ${JSON.stringify(summary)}).get('release_gate'))`],
      { cwd: resolve(ROOT, '..') })
    results.push({ name: '回写注册表 release_gate.triple_ok', ok: true, detail: reg.trim().slice(0, 120) })
  } catch (e) {
    results.push({ name: '回写注册表 release_gate.triple_ok', ok: false,
      detail: `注册表回写失败（不影响门禁结论）: ${String(e.stdout || e.message).slice(0, 160)}` })
  }
}

console.log('')
console.log('══════ motor-ai-l0 W6 发布门禁（三重一致性） ══════')
for (const r of results) {
  console.log(`${r.ok ? '✅' : '❌'} ${r.name.padEnd(42, ' ')} | ${r.detail}`)
}
console.log('──────────────────────────────────────────────────')
console.log(tripleOk ? `✅ triple_ok = true ｜ 可发布` : `❌ triple_ok = false ｜ 禁止发布（先消 drift / 对齐版本 / 过前置）`)
if (g1.fingerprint) console.log(`指纹 ${g1.fingerprint}`)
console.log(`报告已写出: ${outPath}`)
console.log('')

process.exit(process.argv.includes('--check') && !tripleOk ? 1 : 0)
