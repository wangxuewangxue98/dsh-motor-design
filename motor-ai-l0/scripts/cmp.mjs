/**
 * 跨版本对比（--cmp）—— 防止「修复被误读为回退」
 * ---------------------------------------------------------------------------
 * 背景（2026-10-08 压测复盘教训）：v0.2.5 的 V18 会拦截旧版放行的越界频率，
 * 朴素的「可行率对比」把这类输入的可行率下降误读成「200rpm 回退 80pp」。
 * 本工具因此强制按四组归类，只有 `regression_suspect` 才算真回退：
 *
 *   both_ok                  两版都可行
 *   new_only                 仅新版可行（如 V16 断崖修复 → 改进）
 *   old_only_blocked_by_rule 仅旧版可行，且该 spec 属「旧版放行、新版按规则拦截」
 *                            的域（越界频率 / 同步转速零转差 / 非法极数等）→ 预期行为
 *   regression_suspect       仅旧版可行且不在上述拦截域 → 疑似真回退，CI 据此退出非零
 *
 * 用法：
 *   node scripts/cmp.mjs --against 0.2.5 [--cases 24]
 *   （--against 为 npm 上已发布版本；当前工作树版本直接从源码加载）
 */

import { mkdtempSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { execFileSync } from 'node:child_process'
import { resolve, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const require = createRequire(import.meta.url)

// ---- 解析参数 ----
const argv = process.argv.slice(2)
const getArg = (k, dflt) => {
  const i = argv.indexOf(`--${k}`)
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : dflt
}
const against = getArg('against')
const caseCount = parseInt(getArg('cases', '24'), 10)
if (!against) {
  console.error('用法: node scripts/cmp.mjs --against <npm 版本号，如 0.2.5> [--cases 24]')
  process.exit(1)
}

// ---- 对比用例集（确定性覆盖：常规 / VFD / 同步转速 / 越界频率 / 非法极数 / PMSM 高速）----
function buildSpecs() {
  const specs = []
  const powers = [15, 75, 200, 450]
  const stdSpeeds = [960, 1460, 2960]
  for (const p of powers) for (const s of stdSpeeds) {
    specs.push({ power_kw: p, speed_rpm: s, voltage_v: p >= 160 ? 690 : 380, count: 40, _g: 'std' })
  }
  // VFD 合法频率域（V16/V18 修复的受益域）
  for (const f of [25, 100, 200]) {
    specs.push({ power_kw: 15, speed_rpm: 1460 * f / 50, voltage_v: 380, line_freq_hz: f, count: 40, _g: 'vfd_legal' })
    specs.push({ power_kw: 450, speed_rpm: 985 * f / 50, voltage_v: 690, line_freq_hz: f, count: 40, _g: 'vfd_legal' })
  }
  // 同步转速零转差（V16 修复域：1500rpm@50Hz 旧版断崖）
  specs.push({ power_kw: 15, speed_rpm: 1500, voltage_v: 380, count: 40, _g: 'sync_speed' })
  specs.push({ power_kw: 75, speed_rpm: 1000, voltage_v: 660, count: 40, _g: 'sync_speed' })
  // 越界频率 / 非法极数（旧版放行、新版按规则拦截 —— 预期组）
  specs.push({ power_kw: 15, speed_rpm: 3600, voltage_v: 380, line_freq_hz: 800, count: 40, _g: 'oob_rule' })
  specs.push({ power_kw: 75, speed_rpm: 12000, voltage_v: 660, line_freq_hz: 1200, count: 40, _g: 'oob_rule' })
  specs.push({ power_kw: 15, speed_rpm: 1460, voltage_v: 380, poles: 3, count: 40, _g: 'oob_rule' })
  // PMSM 高速（显式类型，验证 P0 透传不回归）
  specs.push({ power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm', count: 40, _g: 'pmsm_hs' })
  return specs.slice(0, Math.max(caseCount, 12))
}

// ---- 加载某一版本的运行面 ----
function loadFace(baseDir) {
  // 动态 import 需要绝对路径 + file:// URL
  const imp = (p) => import(`file:///${join(baseDir, p).replace(/\\/g, '/')}`)
  return Promise.all([
    imp('tools/l0/param-matrix.mjs'),
    imp('tools/l0/design-validate.mjs'),
  ]).then(([pm, dv]) => ({
    buildParamMatrix: pm.buildParamMatrix,
    runDesignValidate: dv.runDesignValidate,
    version: (() => { try { return require(join(baseDir, 'package.json')).version } catch { return '?' } })(),
  }))
}

function feasibleRate(face, spec) {
  const built = face.buildParamMatrix(spec, { maxMatrixSize: 2000 })
  const rows = built.matrix ?? []
  if (!rows.length) return { rate: 0, total: 0, topEff: NaN, warnCodes: (built.warnings ?? []).map((w) => w.code) }
  const v = face.runDesignValidate({ params_list: rows }, {})
  const s = v.summary ?? {}
  const ok = (s.passed ?? 0) + (s.warning ?? 0)
  const est = null // 不做估算对比：效率跨版本口径变化（损耗标定）属预期演化，不是回归判据
  void est
  return { rate: ok / rows.length, total: rows.length, warnCodes: (built.warnings ?? []).map((w) => w.code) }
}

// ---- 主流程 ----
console.log(`cmp: 当前工作树 vs npm:${against}`)
const tmp = mkdtempSync(join(tmpdir(), 'l0cmp-'))
let oldFace
try {
  // Windows 下 spawnSync('npm') 无 shell 不解析 .cmd（ENOENT），故用当前 node
  // 显式调用 npm-cli.js；布局异常时回退 shell 模式调用 npm
  // --legacy-peer-deps：插件 peerDeps 是 DSH 宿主（本就不随包安装），对比场景无需解析
  const npmCli = join(dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js')
  const extra = ['--no-save', '--legacy-peer-deps', '--silent', '--registry=https://registry.npmjs.org']
  if (existsSync(npmCli)) {
    execFileSync(process.execPath,
      [npmCli, 'install', '--prefix', tmp, `dsh-motor-ai-l0@${against}`, ...extra],
      { stdio: 'inherit', shell: false })
  } else {
    execFileSync('npm', ['install', '--prefix', tmp, `dsh-motor-ai-l0@${against}`, ...extra],
      { stdio: 'inherit', shell: true })
  }
  oldFace = await loadFace(join(tmp, 'node_modules', 'dsh-motor-ai-l0'))
} catch (err) {
  rmSync(tmp, { recursive: true, force: true })
  console.error(`拉取 ${against} 失败: ${err.message}`)
  process.exit(2)
}
const newFace = await loadFace(ROOT)
console.log(`  new = ${newFace.version}（工作树） | old = ${oldFace.version}（npm）\n`)

const groups = { both_ok: 0, new_only: 0, old_only_blocked_by_rule: 0, regression_suspect: 0 }
const suspects = []
const specList = buildSpecs()
for (const spec of specList) {
  const o = feasibleRate(oldFace, spec)
  const n = feasibleRate(newFace, spec)
  const tag = `${spec._g}: ${spec.power_kw}kW/${spec.speed_rpm}rpm${spec.line_freq_hz ? `/f${spec.line_freq_hz}` : ''}${spec.poles ? `/p${spec.poles}` : ''}`
  if (n.rate > 0 && o.rate > 0) groups.both_ok++
  else if (n.rate > 0 && o.rate === 0) { groups.new_only++; console.log(`  [+] new_only  ${tag}（0→${(n.rate * 100).toFixed(0)}%，修复受益）`) } else if (n.rate === 0 && o.rate > 0) {
    if (spec._g === 'oob_rule') { groups.old_only_blocked_by_rule++; console.log(`  [~] old_only  ${tag}（新版按规则拦截越界输入 → 预期，非回退）`) } else {
      groups.regression_suspect++
      suspects.push(tag)
      console.log(`  [!] REGRESSION? ${tag}（${(o.rate * 100).toFixed(0)}%→0%，且不属于规则拦截域）`)
    }
  } else {
    // 两版都 0 —— 同步转速零转差等两版都拒的场景归 both（不算变化）
    groups.both_ok++
  }
}

rmSync(tmp, { recursive: true, force: true })
console.log(`\n汇总（${specList.length} 用例）: both_ok=${groups.both_ok}  new_only=${groups.new_only}（改进）  ` +
  `old_only_blocked_by_rule=${groups.old_only_blocked_by_rule}（预期拦截）  regression_suspect=${groups.regression_suspect}`)

if (groups.regression_suspect > 0) {
  console.error(`\n❌ 存在 ${groups.regression_suspect} 例疑似回退: ${suspects.join(' ; ')}`)
  console.error('   处置：先核对是否为断言口径/分组问题（压测复盘教训），确认后修复再发版。')
  process.exit(1)
}
console.log('\n✅ 无疑似回退（old_only 全部落在规则拦截域）')
