#!/usr/bin/env node
/**
 * v0.5.3 L0 四项修复行为探针
 *   P0-2 maxMatrixSize 静默截断 → 告警 MATRIX_TRUNCATED + 严格模式报错
 *   P0-1 verdict 密度敏感       → density_check 两档自检（默认 OFF，零回归）
 *   P0-4 默认极数静默           → POLES_DEFAULT_ASSUMED 建议（含更多极数候选）
 *   P1-1 冷却回退静默           → COOLING_FALLBACK 告警（water → 建议 liquid_jacket）
 *
 * 用法：node scripts/l0_fixes_probe_rc16.mjs
 * 全绿末行打印 L0_FIXES_PROBE_0.5.3_ALL_OK，否则打印失败项并 exit(1)。
 */
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildParamMatrix } from '../tools/l0/param-matrix.mjs'
import { runL0Pipeline } from '../tools/l0/pipeline.mjs'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const FAILS = []

function check(name, cond, detail = '') {
  console.log(`[${cond ? 'OK  ' : 'FAIL'}] ${name}${detail ? '  -- ' + detail : ''}`)
  if (!cond) FAILS.push(name)
}
function codes(warnings = []) {
  return warnings.map((w) => w.code)
}
const OFF = { freeQuotaEnabled: false, handoffDir: join(ROOT, '.probe-handoff') }

console.log('='.repeat(68))
console.log('v0.5.3 L0 四项修复行为探针')
console.log('='.repeat(68))

// ---------- P0-2：静默截断 ----------
console.log('\n--- P0-2 maxMatrixSize 静默截断 ---')
{
  // 旧行为：count=10000 但默认 maxMatrixSize=2000 ⇒ 只生成 2000 行且不告警
  const m = buildParamMatrix(
    { power_kw: 400, speed_rpm: 240, line_freq_hz: 12.5, motor_type: 'pmsm', count: 10000 },
    { ...OFF, handoffDir: undefined })
  check('P0-2.1 count=10000 默认上限 ⇒ truncated=true', m.truncated === true,
    `truncated=${m.truncated} returned=${m.returned}`)
  check('P0-2.2 截断进入 warnings（不再是静默）', codes(m.warnings).includes('MATRIX_TRUNCATED'),
    `codes=${codes(m.warnings).join(',') || '(空)'}`)
  const w = m.warnings.find((x) => x.code === 'MATRIX_TRUNCATED')
  check('P0-2.3 告警含 requested/maxMatrixSize/suggested',
    w && w.requested_count === 10000 && w.max_matrix_size === 2000 && w.suggested_max_matrix_size === 10000,
    w ? `requested=${w.requested_count} limit=${w.max_matrix_size} suggested=${w.suggested_max_matrix_size}` : 'n/a')

  // 显式提高上限 ⇒ 不截断、不告警
  const m2 = buildParamMatrix(
    { power_kw: 400, speed_rpm: 240, line_freq_hz: 12.5, motor_type: 'pmsm', count: 10000 },
    { ...OFF, maxMatrixSize: 10000, handoffDir: undefined })
  check('P0-2.4 显式 maxMatrixSize=10000 ⇒ 不截断、无告警',
    m2.truncated === false && !codes(m2.warnings).includes('MATRIX_TRUNCATED'),
    `truncated=${m2.truncated} returned=${m2.returned}`)

  // 请求量在限内 ⇒ 不得误报
  const m3 = buildParamMatrix(
    { power_kw: 400, speed_rpm: 240, line_freq_hz: 12.5, motor_type: 'pmsm', count: 200 },
    { ...OFF, handoffDir: undefined })
  check('P0-2.5 count=200（限内）⇒ 不误报截断',
    m3.truncated === false && !codes(m3.warnings).includes('MATRIX_TRUNCATED'),
    `truncated=${m3.truncated}`)

  // 严格模式：报错而非截断
  let threw = null
  try {
    buildParamMatrix(
      { power_kw: 400, speed_rpm: 240, line_freq_hz: 12.5, motor_type: 'pmsm', count: 10000 },
      { ...OFF, strictMaxMatrixSize: true, handoffDir: undefined })
  } catch (e) { threw = e }
  check('P0-2.6 strictMaxMatrixSize=true ⇒ 抛错（fail-fast）',
    threw !== null && /strictMaxMatrixSize/.test(String(threw?.message || '')),
    threw ? threw.message.slice(0, 60) + '…' : '未抛错')
}

// ---------- P1-1：冷却回退 ----------
console.log('\n--- P1-1 冷却方式静默回退 ---')
{
  const m = buildParamMatrix(
    { power_kw: 400, speed_rpm: 240, line_freq_hz: 12.5, motor_type: 'pmsm', count: 20, cooling: 'water' },
    { ...OFF, handoffDir: undefined })
  check('P1-1.1 cooling=water ⇒ 落 COOLING_FALLBACK 告警',
    codes(m.warnings).includes('COOLING_FALLBACK'), `codes=${codes(m.warnings).join(',') || '(空)'}`)
  const w = m.warnings.find((x) => x.code === 'COOLING_FALLBACK')
  check('P1-1.2 告警给出合法近义 liquid_jacket',
    w && w.suggested === 'liquid_jacket' && w.resolved_as === 'forced_air',
    w ? `input=${w.input} → ${w.resolved_as}, suggested=${w.suggested}` : 'n/a')
  const m2 = buildParamMatrix(
    { power_kw: 400, speed_rpm: 240, line_freq_hz: 12.5, motor_type: 'pmsm', count: 20, cooling: 'liquid_jacket' },
    { ...OFF, handoffDir: undefined })
  check('P1-1.3 合法值 liquid_jacket ⇒ 不误报',
    !codes(m2.warnings).includes('COOLING_FALLBACK') && m2.spec.cooling === 'liquid_jacket',
    `cooling=${m2.spec.cooling}`)
}

// ---------- P0-4：默认极数建议 ----------
console.log('\n--- P0-4 默认极数（只给建议，不改默认）---')
{
  const m = buildParamMatrix(
    { power_kw: 400, speed_rpm: 240, line_freq_hz: 12.5, motor_type: 'pmsm', count: 20 },
    { ...OFF, handoffDir: undefined })
  check('P0-4.1 未指定 poles ⇒ POLES_DEFAULT_ASSUMED',
    codes(m.warnings).includes('POLES_DEFAULT_ASSUMED'), `poles=${m.spec.poles}`)
  const w = m.warnings.find((x) => x.code === 'POLES_DEFAULT_ASSUMED')
  check('P0-4.2 给出更多极数候选（>当前极数）',
    w && Array.isArray(w.higher_pole_candidates) && w.higher_pole_candidates.every((p) => p > w.poles),
    w ? `poles=${w.poles} higher=${w.higher_pole_candidates.join('/')}` : 'n/a')
  const m2 = buildParamMatrix(
    { power_kw: 400, speed_rpm: 240, line_freq_hz: 12.5, motor_type: 'pmsm', count: 20, poles: 6 },
    { ...OFF, handoffDir: undefined })
  check('P0-4.3 显式 poles=6 ⇒ 不给该建议、且不改变默认逻辑',
    !codes(m2.warnings).includes('POLES_DEFAULT_ASSUMED') && m2.spec.poles === 6,
    `poles=${m2.spec.poles}`)
  check('P0-4.4 spec 暴露 poles_explicit 供调用方判定',
    m.spec.poles_explicit === false && m2.spec.poles_explicit === true,
    `默认=${m.spec.poles_explicit} 显式=${m2.spec.poles_explicit}`)
}

// ---------- P0-1：密度收敛自检 ----------
console.log('\n--- P0-1 verdict 密度敏感（收敛自检）---')
{
  const base = { power_kw: 400, speed_rpm: 240, line_freq_hz: 12.5, motor_type: 'pmsm', count: 800 }
  // 默认 OFF ⇒ density 恒 null（零回归：不改变既有输出形态）
  const off = runL0Pipeline({ ...base, write_handoff: false }, { ...OFF })
  check('P0-1.1 默认关闭 ⇒ density=null（零回归）', off.density === null, `density=${JSON.stringify(off.density)}`)

  // 开启 ⇒ 两档比对
  const on = runL0Pipeline({ ...base, write_handoff: false, density_check: true }, { ...OFF })
  check('P0-1.2 density_check=true ⇒ density 非 null', on.density !== null)
  const d = on.density || {}
  check('P0-1.3 含两档计数与漂移量',
    Number.isFinite(d.coarse_count) && d.coarse_count < d.fine_count && Number.isFinite(d.delta_pp),
    `coarse=${d.coarse_count} fine=${d.fine_count} delta_pp=${d.delta_pp} converged=${d.converged}`)
  check('P0-1.4 粗档 = 请求量的 1/4', d.coarse_count === Math.max(1, Math.floor(base.count / 4)),
    `coarse=${d.coarse_count}`)
  const hasBoth = Number.isFinite(d.coarse_top_efficiency) && Number.isFinite(d.fine_top_efficiency)
  check('P0-1.5 两档 Top1 效率均有值（读 efficiency_raw，不是被钳的 efficiency）', hasBoth,
    hasBoth ? `coarse=${d.coarse_top_efficiency} fine=${d.fine_top_efficiency}` : '缺值')
  // 未收敛时必须有告警
  if (d.converged === false) {
    check('P0-1.6 未收敛 ⇒ warnings 含 DENSITY_NOT_CONVERGED',
      codes(on.warnings).includes('DENSITY_NOT_CONVERGED'), `codes=${codes(on.warnings).join(',')}`)
  } else {
    check('P0-1.6 已收敛 ⇒ 无 DENSITY_NOT_CONVERGED 告警',
      !codes(on.warnings).includes('DENSITY_NOT_CONVERGED'), `delta_pp=${d.delta_pp}`)
  }
  // 默认 OFF 与开启时主结果不变（Top1 效率一致 ⇒ 自检不影响主链）
  const a = off.recommended?.efficiency_raw ?? off.results?.[0]?.efficiency_raw ?? null
  const b = on.recommended?.efficiency_raw ?? on.results?.[0]?.efficiency_raw ?? null
  check('P0-1.7 自检不影响主链结果（Top1 效率一致）', a != null && a === b, `off=${a} on=${b}`)
}

console.log()
if (FAILS.length) {
  console.log(`❌ 失败 ${FAILS.length} 项: ${FAILS.join(' | ')}`)
  process.exit(1)
}
console.log('L0_FIXES_PROBE_0.5.3_ALL_OK')
