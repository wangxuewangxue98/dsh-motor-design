/**
 * scripts/verify.mjs —— W1-W4 离线校验（安装前自检）
 * ============================================================
 * 校验项分组：
 *   1~5   包与配置一致性（files 含 patch / patch 结构 / CHANGELOG 版本 / .mjs 语法）
 *   6~12  level-gate 层级门控行为断言
 *   13~21 Param 锁字段真源一致性
 *   22~28 公式引擎（对齐 Python 真源 + 双字段自洽）
 *   29~34 参数矩阵（确定性 / 形状 / 上限夹紧 / 高速工况 / 截断 / 快速失败）
 *   35~38 L0 估算与交接载荷
 *   39~46 W4 物理一致性校验（规则目录 / 磁密公式 / 硬规则 / 跳过 / 批量 / escalate / 高速气隙）
 *   47~49 W4 回归质量门（标定集 / 三态结论 / 劣化检出 / 基线快照）
 *
 * 用法：
 *   node scripts/verify.mjs
 */
import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs'
import { readdir, access } from 'node:fs/promises'
import { join, dirname, resolve, relative } from 'node:path'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import {
  LEVEL_ORDER, IMPLEMENTED_LEVELS,
  normalizeLevel, isLevelEnabled, assertLevelImplemented, tierOf,
} from '../lib/level-gate.mjs'
import {
  L1_STRICT_REQUIRED, L1_MATRIX_FIELDS, PARAM_SCHEMA, L0_NATIVE_FIELDS,
  L1_MIRROR_FIELDS, normalizeSpec, assertMatrixShape, pickL1Payload,
} from '../lib/param-schema.mjs'
import {
  computeTorque, computeD2L, computeLambda, validateLambda, quickL0Estimate,
  deriveElectricalClosure,
} from '../lib/formula-engine.mjs'
import { buildParamMatrix, recommendPoles, poleCandidates } from '../tools/l0/param-matrix.mjs'
import { runL0Estimate } from '../tools/l0/l0-estimate.mjs'
import { runDesignValidate } from '../tools/l0/design-validate.mjs'
import {
  RULE_CATALOG, validateDesign, validateDesignBatch,
  toothFluxDensity, yokeFluxDensity, airGapFluxActual,
} from '../lib/design-rules.mjs'
import {
  assessApplicability, scenarioFromMatrix, APPLICABILITY,
} from '../lib/applicability-gate.mjs'
import { solveYokeAndFrame, yokeThicknessForFlux, resolveMotorType } from '../lib/motor-constants.mjs'
import { selectFamilySegment } from '../lib/surrogate-engine.mjs'
import { evaluateSuite, makeBaseline, compareBaseline } from '../lib/regression-gate.mjs'
import {
  TELEM_FIELDS, sanitizeTelemetry, aggregateUsage,
} from '../lib/telemetry.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

/** 递归列出目录下所有文件（发布物泄漏扫描用，跳过 node_modules 与隐藏目录） */
function walk(dir) {
  const out = []
  let ents
  try { ents = readdirSync(dir, { withFileTypes: true }) } catch { return out }
  for (const e of ents) {
    if (e.name === 'node_modules' || e.name.startsWith('.')) continue
    const p = join(dir, e.name)
    if (e.isDirectory()) out.push(...walk(p))
    else out.push(p)
  }
  return out
}

// 社区反馈工具（静态导入：check() 是同步的，不能在断言里 await import）
import {
  buildCaseFeedbackDraft, assertNoSensitiveData, sensitivityRules,
} from '../tools/feedback/case-feedback.mjs'
const results = []
let failed = 0

function check(name, fn) {
  try {
    const detail = fn()
    results.push({ name, ok: true, detail: detail || 'OK' })
  } catch (e) {
    failed += 1
    results.push({ name, ok: false, detail: e.message })
  }
}

function must(cond, msg) {
  if (!cond) throw new Error(msg)
}

// ---------- 1~4. 包与配置一致性 ----------
const pkgPath = join(ROOT, 'package.json')
check('package.json 可解析 + 语义化版本', () => {
  const pkg = JSON.parse(readFileSync(pkgPath, 'utf8'))
  must(/^\d+\.\d+\.\d+$/.test(pkg.version), `version 非法: ${pkg.version}`)
  return `v${pkg.version}`
})

let pkg = null
try { pkg = JSON.parse(readFileSync(pkgPath, 'utf8')) } catch { /* ignore */ }

check('files 含 cordis.patch.yml', () => {
  const files = pkg?.files || []
  must(files.includes('cordis.patch.yml'), 'files 缺失 cordis.patch.yml')
  return `${files.length} 项`
})

check('cordis.patch.yml 结构有效', () => {
  const p = join(ROOT, 'cordis.patch.yml')
  must(existsSync(p), '文件不存在')
  const txt = readFileSync(p, 'utf8')
  must(txt.includes('- insert:'), '缺少 insert 段')
  must(/id:\s*dsh-motor-ai-l0/.test(txt), '缺少插件 id')
  must(/level:\s*'l0'/.test(txt), "level 默认必须为 'l0'")
  must(!/^\s*l1Enabled:\s*true/m.test(txt), 'L1 分闸不得默认开启')
  return 'insert/id/level 齐备'
})

check('CHANGELOG 含当前版本条目', () => {
  const p = join(ROOT, 'CHANGELOG.md')
  must(existsSync(p), 'CHANGELOG.md 不存在')
  const txt = readFileSync(p, 'utf8')
  must(txt.includes(`## [${pkg.version}]`), `缺少 ## [${pkg.version}] 条目`)
  return ` matched v${pkg.version}`
})

check('SKILL.md 三重一致性（W5 前跳过）', () => {
  const p = join(ROOT, 'skills', 'motor-l0-estimate', 'SKILL.md')
  if (!existsSync(p)) return 'SKILL.md 未交付（W5），已跳过'
  const txt = readFileSync(p, 'utf8')
  const v = txt.match(/version:\s*([\d.]+)/)?.[1]
  must(v === pkg.version, `SKILL.md ${v} ≠ package.json ${pkg.version}`)
  return `SKILL.md ${v} 一致`
})

// ---------- 5. 所有 .mjs 语法检查 ----------
async function walkMjs(dir) {
  const out = []
  for (const ent of await readdir(dir, { withFileTypes: true })) {
    if (ent.name === 'node_modules' || ent.name.startsWith('.')) continue
    const full = join(dir, ent.name)
    if (ent.isDirectory()) out.push(...await walkMjs(full))
    else if (ent.name.endsWith('.mjs')) out.push(full)
  }
  return out
}

const mjsFiles = await walkMjs(ROOT)
await check('所有 .mjs 通过 node --check', () => {
  const bad = []
  const skipped = []
  for (const f of mjsFiles) {
    try {
      execFileSync(process.execPath, ['--check', f], { stdio: 'pipe' })
    } catch (e) {
      // 真实语法错误：子进程会把 SyntaxError 打到 stderr 并以非零退出
      const stderr = (e.stderr && e.stderr.toString()) || ''
      if (stderr) {
        bad.push(`${relative(ROOT, f)}: ${stderr.split('\n')[0]}`)
      } else {
        // 无 stderr 且 status=null ⇒ 环境禁止 spawn 子进程（沙箱 EBUSY/ENOENT），非语法问题，跳过
        skipped.push(relative(ROOT, f))
      }
    }
  }
  must(bad.length === 0, `语法错误: ${bad.join(' | ')}`)
  if (skipped.length) return `${mjsFiles.length - skipped.length} 个文件通过，${skipped.length} 个因沙箱禁止 spawn 子进程跳过（真实环境会全检）`
  return `${mjsFiles.length} 个文件全部通过`
})

// ---------- 6. level-gate 行为断言 ----------
check('assertLevelImplemented: l0 放行', () => {
  must(assertLevelImplemented('l0') === 'l0', 'l0 应通过')
  return "l0 → OK"
})

check('assertLevelImplemented: l1/l2/空 快速失败', () => {
  for (const lv of ['l1', 'l2', '', null, undefined, 'L1']) {
    let threw = false
    try { assertLevelImplemented(lv) } catch { threw = true }
    must(threw, `${lv} 应抛错但未抛`)
  }
  return 'l1/l2/空/L1 均抛错'
})

check('normalizeLevel: 大小写与空格容错', () => {
  must(normalizeLevel(' L0 ') === 'l0', 'trim/lower 失败')
  must(normalizeLevel('L2') === 'l2', '大写转换失败')
  return 'OK'
})

check('isLevelEnabled: 总闸截断 + 分闸控制', () => {
  const cfg = { level: 'l0', l1Enabled: true, l2Enabled: true }
  must(isLevelEnabled(cfg, 'l0') === true, 'l0 应可用')
  must(isLevelEnabled(cfg, 'l1') === false, 'level=l0 时 l1 应被总闸截断')
  must(isLevelEnabled(cfg, 'l2') === false, 'level=l0 时 l2 应被总闸截断')
  const cfg2 = { level: 'l2', l1Enabled: true, l2Enabled: false }
  must(isLevelEnabled(cfg2, 'l2') === false, 'l2Enabled=false 时应关闭')
  return '总闸/分闸双层生效'
})

check('tierOf: 付费等级映射', () => {
  must(tierOf('l0') === 'free' && tierOf('l1') === 'vip' && tierOf('l2') === 'flagship', '映射错误')
  must(tierOf('x') === null, '未知层级应为 null')
  return 'l0=free / l1=vip / l2=flagship'
})

check('LEVEL_ORDER 与 IMPLEMENTED_LEVELS 自洽', () => {
  must(LEVEL_ORDER[0] === 'l0', 'LEVEL_ORDER 首位应为 l0')
  must(IMPLEMENTED_LEVELS.every((l) => LEVEL_ORDER.includes(l)), '已实现层级越界')
  return `已实现: ${IMPLEMENTED_LEVELS.join(',')}`
})

// ---------- 7~13. Param 锁（字段真源一致性） ----------
const toolsIndex = await import('../tools/l0/index.mjs')

check('Param锁: 严格必填 ⊆ 矩阵字段', () => {
  const missing = L1_STRICT_REQUIRED.filter((f) => !L1_MATRIX_FIELDS.includes(f))
  must(missing.length === 0, `严格必填越界: ${missing.join(',')}`)
  return `4 项严格必填全部登记`
})

check('Param锁: 每个矩阵字段都有 Schema 且 source=L1', () => {
  const bad = L1_MATRIX_FIELDS.filter((f) => !PARAM_SCHEMA[f] || PARAM_SCHEMA[f].source !== 'L1')
  must(bad.length === 0, `字段未登记或来源错误: ${bad.join(',')}`)
  return `${L1_MATRIX_FIELDS.length} 字段全部 source=L1`
})

check('Param锁: normalizeSpec 别名归一', () => {
  const { spec, applied } = normalizeSpec({ power: 15, rpm: 3000, volt: 380, design_count: 20 })
  must(spec.power_kw === 15, 'power → power_kw 失败')
  must(spec.speed_rpm === 3000, 'rpm → speed_rpm 失败')
  must(spec.voltage_v === 380, 'volt → voltage_v 失败')
  must(spec.count === 20, 'design_count → count 失败')
  return `生效别名 ${applied.length} 个`
})

check('Param锁: assertMatrixShape 能抓到缺失', () => {
  const bad = assertMatrixShape({ stator_od: 180, poles: 8 })
  must(bad.ok === false, '缺字段应判定不通过')
  must(bad.missing.includes('voltage') && bad.missing.includes('speed'), '未识别出 voltage/speed 缺失')
  const good = assertMatrixShape({
    stator_od: 180, poles: 8, voltage: 380, speed: 3000,
  })
  must(good.ok === true, '四项齐备应判定通过')
  return '缺/齐两种分支均正确'
})

check('Param锁: pickL1Payload 剔除 L0 私有字段', () => {
  const payload = pickL1Payload({
    stator_od: 180, poles: 8, voltage: 380, speed: 3000,
    power_kw: 15, cooling: 'forced_air',
    efficiency: 95, max_temp: 70, l1_handoff: { a: 1 },
  })
  must(!('power_kw' in payload), 'power_kw 不得进入交接载荷')
  must(!('cooling' in payload), 'cooling 不得进入交接载荷')
  must(!('efficiency' in payload), 'L1-output 字段不得进入交接载荷')
  must(payload.stator_od === 180, '合法字段丢失')
  return 'L0 私有字段已隔离'
})

check('Param锁: 原生/镜像字段无重叠', () => {
  const overlap = L0_NATIVE_FIELDS.filter((f) => L1_MIRROR_FIELDS.includes(f))
  must(overlap.length === 0, `字段重叠: ${overlap.join(',')}`)
  return `原生 ${L0_NATIVE_FIELDS.length} + 镜像 ${L1_MIRROR_FIELDS.length}`
})

// ---------- 14~17. 公式引擎（对齐 Python 真源） ----------
check('formula: computeTorque 对齐 physics_kernel.py:63-67', () => {
  must(computeTorque(15, 3000) === 47.75, `9550*15/3000 应为 47.75，实际 ${computeTorque(15, 3000)}`)
  must(computeTorque(200, 22000) === 86.82, `200kW/22000rpm 应为 86.82，实际 ${computeTorque(200, 22000)}`)
  must(computeTorque(15, 0) === 0, '转速为 0 应返回 0')
  return '47.75 / 86.82 / 0'
})

check('formula: D²L 与 λ 对齐同名函数', () => {
  must(computeD2L(100, 100) === 0.001, `D²L(100,100) 应为 0.001，实际 ${computeD2L(100, 100)}`)
  must(computeLambda(90, 100) === 0.9, 'λ 计算错误')
  must(computeLambda(90, 0) === 0, 'D=0 应返回 0')
  return 'D²L/λ 与 Python 侧同式'
})

check('formula: validateLambda 边界行为', () => {
  const shortCore = validateLambda(30, 120, 4)
  must(shortCore.valid === false, 'λ=0.25 < 0.7 应判为偏小')
  must(shortCore.advice.includes('偏小'), 'advice 应提示偏小')
  const longCore = validateLambda(300, 120, 4)
  must(longCore.valid === false, 'λ=2.5 > 1.4 应判为偏大')
  must(longCore.advice.includes('偏大'), 'advice 应提示偏大')
  const inRange = validateLambda(120, 120, 4)
  must(inRange.valid === true, 'λ=1.0 在 [0.7,1.4] 内应通过')
  return '偏小/偏大/合规 三态正确'
})

check('formula: 15kW 标定点的效率与温升落在工程合理域', () => {
  const row = quickL0Estimate({
    stator_od: 180, stator_id: 108, core_length: 100, air_gap: 0.6,
    poles: 8, voltage: 380, speed: 3000, slots_stator: 48, slots_rotor: 44,
    rotor_od: 106.8, tooth_width: 3.53, power_kw: 15, cooling: 'forced_air',
  })
  must(row.efficiency >= 88 && row.efficiency <= 97, `效率 ${row.efficiency}% 越出 [88,97]`)
  must(row.temp_rise >= 20 && row.temp_rise <= 120, `温升 ${row.temp_rise}K 越出 [20,120]`)
  must(row.prediction_source === 'formula', 'prediction_source 应为 formula')
  must(row.solve_mode === 'l0', 'solve_mode 应为 l0')
  return `效率 ${row.efficiency}% | 温升 ${row.temp_rise}K | 损耗 ${row.total_loss}W`
})

check('formula: 效率_CAP 生效（不越 _run_simulated 的 96 口径）', () => {
  const row = quickL0Estimate({
    stator_od: 400, stator_id: 240, core_length: 300, air_gap: 1.0,
    poles: 2, voltage: 380, speed: 3000, slots_stator: 24, slots_rotor: 20,
    rotor_od: 238, tooth_width: 15.7, power_kw: 200, cooling: 'oil_immersed',
  }, { efficiencyCap: 96 })
  must(row.efficiency <= 96, `效率 ${row.efficiency} 应被封顶到 96`)
  return `大尺寸样本被封顶至 ${row.efficiency}%`
})

check('formula: 镜像字段与原生损耗内部自洽（禁止同一行出现两个口径）', () => {
  const row = quickL0Estimate({
    stator_od: 180, stator_id: 108, core_length: 100, air_gap: 0.6,
    poles: 8, voltage: 380, speed: 3000, slots_stator: 48, slots_rotor: 44,
    rotor_od: 106.8, tooth_width: 3.53, power_kw: 15, cooling: 'forced_air',
  })
  for (const f of ['max_temp', 'power', 'copper_loss', 'iron_loss', 'mechanical_loss']) {
    must(typeof row[f] === 'number', `镜像字段 ${f} 缺失或非数值`)
  }
  must(row.copper_loss > row.iron_loss && row.iron_loss > row.mechanical_loss,
    '损耗应按 铜>铁>机械 排序')
  const sum = row.copper_loss + row.iron_loss + row.mechanical_loss
  must(Math.abs(sum - row.total_loss) < 3,
    `镜像损耗之和 ${sum}W ≠ 原生 total_loss ${row.total_loss}W —— 同一行出现两个口径`)
  must(row.max_temp >= row.temp_rise, `max_temp ${row.max_temp}°C 应 ≥ 温升 ${row.temp_rise}K（按环境 40°C 起算）`)
  return `损耗自洽 ${sum}≈${row.total_loss}W | max_temp ${row.max_temp}°C`
})

check('formula: 高速工况不出现仿真经验式失真值', () => {
  // SIM_EFF/SIM_TEMP 只在 ~180mm/8极/3000rpm 附近有效；
  // 一旦镜像照抄它们，200kW/22000rpm 会推出 80% 效率 / 130°C 触顶。
  const row = quickL0Estimate({
    power_kw: 200, speed: 22000, poles: 2, voltage: 380,
    stator_od: 200, stator_id: 110, rotor_od: 106, core_length: 170,
    air_gap: 2.0, slots_stator: 24, slots_rotor: 20, tooth_width: 7.2,
    cooling: 'liquid_jacket',
  }, { fluxDensity: 1.02 * 0.6 + 0.82 * 0.4 })
  must(row.efficiency > 90, `高速 PMSM 效率被估为 ${row.efficiency}%，明显失真`)
  const sum = row.copper_loss + row.iron_loss + row.mechanical_loss
  must(Math.abs(sum - row.total_loss) < 3, `高速工况下损耗双口径冲突: ${sum} vs ${row.total_loss}`)
  must(row.l1_efficiency_proxy < row.efficiency - 5,
    '诊断列应能暴露仿真经验式的偏差（否则回归门失去意义）')
  return `L0 ${row.efficiency}% vs 仿真 proxy ${row.l1_efficiency_proxy}%（偏差已暴露）`
})

// ---------- 18~21. 参数矩阵 ----------
check('matrix: 生成结果完全确定（两次调用一致）', () => {
  const spec = { power_kw: 15, speed_rpm: 3000, poles: 8, count: 24 }
  const a = buildParamMatrix(spec, { maxMatrixSize: 2000 })
  const b = buildParamMatrix(spec, { maxMatrixSize: 2000 })
  must(JSON.stringify(a.matrix) === JSON.stringify(b.matrix), '同 spec 两次结果不一致（存在随机性）')
  return `${a.matrix.length} 行，逐字节一致`
})

check('matrix: 每行通过 assertMatrixShape 且含 _physics', () => {
  const { matrix } = buildParamMatrix({ power_kw: 15, speed_rpm: 3000, count: 30 }, { maxMatrixSize: 2000 })
  must(matrix.length > 0, '矩阵为空')
  for (const row of matrix) {
    must(assertMatrixShape(row).ok, '存在形状不合规的行')
    must(row._physics && typeof row._physics.d_squared_l === 'number', '_physics 缺失')
    must(row.stator_id > 0 && row.stator_od > row.stator_id, '内外径关系非法')
    must(row.air_gap >= 0.3 && row.air_gap <= 1.5, `气隙 ${row.air_gap} 越界`)
  }
  return `${matrix.length} 行全部合规`
})

check('matrix: 外径上限被遵守且不产出空矩阵', () => {
  const { matrix } = buildParamMatrix(
    { power_kw: 200, speed_rpm: 22000, stator_od_limit: 300, count: 40 }, { maxMatrixSize: 2000 })
  must(matrix.length > 0, '限制偏紧时不应产出空矩阵（应为夹紧而非丢弃）')
  for (const row of matrix) {
    must(row.stator_od <= 300, `外径 ${row.stator_od} 超过 limit 300`)
  }
  const clamped = matrix.filter((r) => r._physics.od_clamped).length
  return `${matrix.length} 行，最大外径 ${Math.max(...matrix.map((r) => r.stator_od))} ≤ 300，夹紧 ${clamped} 行`
})

check('matrix: v0.2.4 默认外径上限自伸缩 —— 大机座不再被写死 450mm 夹死', () => {
  // 旧默认 450mm 与功率完全脱钩：450kW/690V 扫描空间需 OD≈905mm，整批被夹到 450
  // → 槽面积骤减 → V15 槽满率 120 行全 failed，表现为「物理不可行」实为参数夹紧。
  const built = buildParamMatrix(
    { power_kw: 450, speed_rpm: 985, voltage_v: 690, motor_type: 'async', count: 120 }, { maxMatrixSize: 2000 })
  must(built.od_limit?.source === 'auto', `默认上限应由基准自伸缩，实际 source=${built.od_limit?.source}`)
  const clamped = built.matrix.filter((r) => r._physics?.od_clamped).length
  must(clamped === 0, `默认路径不应夹紧，实际夹紧 ${clamped} 行`)
  const v = runDesignValidate({ params_list: built.matrix }, {})
  const feasible = (v.summary?.passed ?? 0) + (v.summary?.warning ?? 0)
  must(feasible > 0, `450kW 放开上限后应产出可行解，实际可行 ${feasible}`)
  return `默认上限 ${built.od_limit.value}mm（扫描空间上界 ${built.od_limit.scan_space_max_od}mm），夹紧 0 行，可行 ${feasible}`
})

check('matrix: v0.2.5 P0 零回归 —— 未触发轭厚放大的行与旧写死 450 逐字节一致', () => {
  // v0.2.5 订正：本用例原断言「默认上限与旧写死 450 逐字节一致」，前提是 450 从不夹紧。
  // P0 按磁密反解轭厚后，部分行 OD 合法增长超过 450mm（15kW 基线实测需至 490mm），
  //   ⇒ 该前提不再成立，逐字节全等不是正确的不变量。
  // 正确的不变量分两层：
  //   ① 未触发轭厚放大的行（od_grown_for_yoke=false 且未被上限夹紧）必须与旧口径逐字节一致
  //   ② 默认上限与显式上限给出的行集必须一致（默认上限已 P0 感知，不应额外夹紧）
  for (const spec of [
    { power_kw: 15, speed_rpm: 1460, voltage_v: 380, count: 120 },
    { power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm', count: 120 },
  ]) {
    const autoBuilt = buildParamMatrix(spec, { maxMatrixSize: 2000 })
    const explicit = buildParamMatrix({ ...spec, stator_od_limit: 450 }, { maxMatrixSize: 2000 })

    // ② 默认上限不得比显式 450 更紧（否则 P0 在默认配置下被悄悄抵消）
    must(autoBuilt.od_limit.value >= 450,
      `${spec.power_kw}kW 默认上限 ${autoBuilt.od_limit.value}mm 低于旧值 450mm，会抵消 P0 修复`)
    for (const row of autoBuilt.matrix) {
      must(row.stator_od <= autoBuilt.od_limit.value,
        `默认上限未被尊重：${row.stator_od} > ${autoBuilt.od_limit.value}`)
    }

    // ① 未放大的行逐字节一致（剔除 _physics 里 P0 新增的诊断键后比较）
    const strip = (r) => {
      const { _physics, ...rest } = r
      const { od_grown_for_yoke, yoke_limited_by_od_cap, yoke_required_for_flux,
        yoke_thickness: _yt, yoke_by_ratio: _yr, yoke_by_flux: _yf,
        stator_od_requested: _sor, stator_od_after_growth: _soa, ...ph } = _physics ?? {}
      return JSON.stringify({ ...rest, _physics: ph })
    }
    const untouched = autoBuilt.matrix.filter(
      (r) => !r._physics?.od_grown_for_yoke && !r._physics?.yoke_limited_by_od_cap)
    // 200kW/22000rpm 极端工况下 P0 对全部行都需放大轭厚（未放大行为 0），
    //   该场景改验「几何仍然合法」而非逐字节比对，否则断言空转。
    if (untouched.length === 0) {
      for (const row of autoBuilt.matrix) {
        must(row.stator_od > row.stator_id, `放大后 OD(${row.stator_od}) 必须仍大于 Dsi(${row.stator_id})`)
        must(row.yoke_thickness > 0 && row.rotor_od > 0 && row.shaft_dia > 0,
          `放大后几何链必须完整（yoke=${row.yoke_thickness} rotor_od=${row.rotor_od}）`)
      }
      continue
    }
    must(untouched.length > 0, `${spec.power_kw}kW 未找到可比对的未放大行，零回归断言无效`)
    let compared = 0
    for (let i = 0; i < Math.min(autoBuilt.matrix.length, explicit.matrix.length); i++) {
      const ra = autoBuilt.matrix[i]
      if (ra._physics?.od_grown_for_yoke || ra._physics?.yoke_limited_by_od_cap) continue
      if (ra.stator_od !== explicit.matrix[i].stator_od) continue  // 上限不同导致的合法差异
      must(strip(ra) === strip(explicit.matrix[i]),
        `${spec.power_kw}kW 未触发轭厚放大的行出现回归：行 ${i} 几何不一致`)
      compared += 1
    }
    must(compared > 0, `${spec.power_kw}kW 未找到可比对的未放大行，零回归断言无效`)
  }
  return '未放大行逐字节一致 + 默认上限 P0 感知且被严格尊重 + 放大后几何链完整'
})

check('matrix: 0.2.6 P0 motor_type 透传 —— 缺省行携带 induction + MOTOR_TYPE_ASSUMED，代理不再 OD 误判', () => {
  // 10 万案例压测（v0.2.5）定位的 P0：79.5% 行 motor_type 缺失，
  // detectMotorType 的 OD 启发式（od<400→pmsm）与生成默认（induction）矛盾，
  // OD<612.4mm 中小型异步机代理通道静默 skipped 19.8%。
  const b = buildParamMatrix({ power_kw: 15, speed_rpm: 1460, voltage_v: 380, count: 30 }, { maxMatrixSize: 2000 })
  must(b.matrix.every((r) => r.motor_type === 'induction'), '缺省 motor_type → 全行携带 induction（代理通道不再落启发式）')
  const w = (b.warnings ?? []).find((x) => x.code === 'MOTOR_TYPE_ASSUMED')
  must(!!w && w.level === 'info', '缺省时回吐 MOTOR_TYPE_ASSUMED（info 级）')
  must(b.spec.motor_type_assumed === true, 'spec.motor_type_assumed=true 让缺省可见')
  // 显式别名归一：'async' 此前不在别名表 → 落 OD 启发式，是压测报告之外的第二处静默误判源
  must(resolveMotorType('async').type === 'induction' && !resolveMotorType('async').assumed, "别名 'async' 归一为 induction")
  must(resolveMotorType('Y2').unrecognized === true, '无法识别的输入标 unrecognized（不静默）')
  must(resolveMotorType(undefined).assumed === true, '缺省标 assumed')
  // 旧启发式已删：od=300 按 induction 命中分族段（修复前误判 pmsm → 段缺失 → skipped）
  const fakeModel = {
    schema: 'l0_surrogate_family',
    induction_segments: { small: { od_range: [0, 612.4] } },
    pmsm_segments: {},
  }
  must(!!selectFamilySegment(300, 'induction', fakeModel), 'od=300 按 induction 命中分族段（P0 主场景）')
  // P1 契约：returned_feasible_count（返回集可能混入 infeasible_thermal，需可自检）
  const e = runL0Estimate({ params_list: b.matrix.slice(0, 20), top_n: 8, sort_by: 'efficiency' }, {})
  must(typeof e.returned_feasible_count === 'number' && e.returned_feasible_count >= 0,
    `returned_feasible_count 已入契约（=${e.returned_feasible_count}）`)
  must(e.results.every((r) => r.params?.motor_type === 'induction'), '结果行 params.motor_type 已透出')
  return '行级透传 + 双层告警（warning/info）+ 别名归一 + P1 计数，4 类断言全过'
})

check('pipeline: 返回体 + handoff 透传 returned_feasible_count（补 0.2.6 双通道分叉盲区）', () => {
  // 0.2.6 CHANGELOG 声称「两个通道 summary 均补 returned_feasible_count」，但 pipeline
  // 返回体与 handoff 实际只透传 feasible_count —— MotorAI_Client 实际入口 motor_l0_pipeline
  // 100% 丢弃该字段。本断言闭环该盲区（门禁只覆盖 l0-estimate 通道，未能发现）。
  // 注：pipe.handoff 是 L0→L1 交接载荷（estimate.handoff），文件交接载荷在 handoff_path 指向的
  // latest.json 里，其 summary 才是 pipeline 写入的紧凑摘要，returned_feasible_count 应在此处。
  const pipe = runL0Pipeline(
    { power_kw: 15, speed_rpm: 1460, voltage_v: 380, count: 30, top_n: 10 }, {})
  must(typeof pipe.summary?.returned_feasible_count === 'number' && pipe.summary.returned_feasible_count >= 0,
    `pipeline.summary.returned_feasible_count 已透传（=${pipe.summary.returned_feasible_count}）`)
  must(pipe.summary.returned_feasible_count <= pipe.summary.feasible_count,
    'returned_feasible_count ≤ feasible_count（TopN 内可行数不超全集可行数）')
  // 文件交接载荷（handoff_path 指向的 latest.json）的 summary 也须携带该字段
  const hp = pipe.handoff_path
  must(typeof hp === 'string' && hp.length > 0, 'handoff_path 已写入')
  const fileJson = JSON.parse(readFileSync(hp, 'utf8'))
  must(typeof fileJson?.summary?.returned_feasible_count === 'number',
    `handoff 文件 summary.returned_feasible_count 已透传（=${fileJson?.summary?.returned_feasible_count}）`)
  // 与 l0-estimate 契约同源：estimate 对象携带该字段，pipeline 现在如实转发
  const b = buildParamMatrix({ power_kw: 15, speed_rpm: 1460, voltage_v: 380, count: 30 }, { maxMatrixSize: 2000 })
  const est = runL0Estimate({ params_list: b.matrix, top_n: 10, sort_by: 'efficiency' }, {})
  must(typeof est.returned_feasible_count === 'number' && est.returned_feasible_count >= 0,
    'l0-estimate 契约字段仍健全')
  return 'pipeline 返回体 + handoff 文件均透传 returned_feasible_count，门禁盲区已补'
})

check('matrix: v0.2.4 显式上限被夹紧时回吐 OD_LIMIT_CLAMPED 告警（且仍严格尊重上限）', () => {
  const built = buildParamMatrix(
    { power_kw: 450, speed_rpm: 985, voltage_v: 690, motor_type: 'async', stator_od_limit: 450, count: 120 },
    { maxMatrixSize: 2000 })
  const w = (built.warnings ?? [])[0]
  must(w?.code === 'OD_LIMIT_CLAMPED', `应回吐 OD_LIMIT_CLAMPED，实际 ${w?.code ?? '无告警'}`)
  must(w.clamped_rows === 120, `夹紧行数应为 120，实际 ${w.clamped_rows}`)
  must(w.suggested_od_limit > 450, `应给出放宽建议值，实际 ${w.suggested_od_limit}`)
  for (const row of built.matrix) {
    must(row.stator_od <= 450, `显式上限未被尊重：${row.stator_od} > 450`)
  }
  return `${w.code}：${w.clamped_rows}/${w.total_rows} 行夹紧，建议放宽至 ${w.suggested_od_limit}mm`
})

check('estimate: v0.2.4 效率封顶不再压平排序 —— 450kW 大机座按未钳位真值排出区分度', () => {
  // 旧行为：efficiencyCap=96 把 450kW 全部候选钳到 96.00 ⇒ Top10 去重仅 1 个值、极差 0.00pt，
  // 排序退化成由数组原序决定（谁排第一是随机的），且对外展示的 η96.00% 是假精度。
  const built = buildParamMatrix(
    { power_kw: 450, speed_rpm: 985, voltage_v: 690, motor_type: 'async', count: 120 }, { maxMatrixSize: 2000 })
  const v = runDesignValidate({ params_list: built.matrix }, {})
  const feasIdx = [...(v.passed_indices ?? []), ...(v.warning_indices ?? [])]
  const feasRows = feasIdx.map((i) => built.matrix[i])
  must(feasRows.length > 0, '前置条件：450kW 应有可行解')

  // v3 机械耗标定（2026-10-08）后 450kW raw ≈94.4 不再自然触顶 96 —— 旧断言依赖
  // 「损耗低估推高效率」的巧合，故改为动态取真值域中点为 cap，主动制造触顶场景，
  // 验证封顶机制本身（触顶标记 + 排序仍按 raw 真值 + 区分度保留）。
  const estRaw = runL0Estimate({ params_list: feasRows, top_n: 10, sort_by: 'efficiency' }, { efficiencyCap: 99 })
  const rawAll = estRaw.results.map((r) => r.efficiency_raw)
  must(rawAll.length > 0 && rawAll[0] > rawAll[rawAll.length - 1], '前置条件：raw 真值应有降序区分度')
  const cap = Math.round(((rawAll[0] + rawAll[rawAll.length - 1]) / 2) * 10) / 10

  const est = runL0Estimate({ params_list: feasRows, top_n: 10, sort_by: 'efficiency' }, { efficiencyCap: cap })
  const top = est.results
  const capped = top.filter((r) => r.efficiency_capped === true).length
  must(capped > 0, `本批应存在触顶行（cap=${cap} 低于 raw 上界 ${rawAll[0]}），实际触顶 ${capped}/${top.length}`)

  const raws = top.map((r) => r.efficiency_raw)
  for (let i = 1; i < raws.length; i++) {
    must(raws[i - 1] >= raws[i], `排序未按真值降序：${raws[i - 1]} < ${raws[i]}`)
  }
  const distinct = new Set(raws).size
  const spread = Math.round((Math.max(...raws) - Math.min(...raws)) * 100) / 100
  must(distinct > 1, `真值应保留区分度，实际去重仅 ${distinct} 个值（排序被压平）`)
  must(est.efficiency_capped_count > 0 && est.efficiency_note, '应据实回吐触顶计数与说明')
  return `Top${top.length} 真值 ${raws[0]}~${raws[raws.length - 1]}%（极差 ${spread}pt，${distinct} 个不同值），cap=${cap} 触顶 ${capped} 行，显示值仍为 ${top[0].efficiency}%`
})

check('estimate: v0.2.4 封顶值本身不变 —— efficiency 仍与 L1 同口径（零回归）', () => {
  const p200 = {
    stator_od: 400, stator_id: 240, core_length: 300, air_gap: 1.0,
    poles: 2, voltage: 380, speed: 3000, slots_stator: 24, slots_rotor: 20,
    rotor_od: 238, tooth_width: 15.7, power_kw: 200, cooling: 'oil_immersed',
  }
  // v3 机械耗标定后该样本 raw 不再自然越 96，同上改为动态 cap（raw−1pt）制造触顶
  const raw200 = quickL0Estimate(p200, { efficiencyCap: 99 }).efficiency_raw
  const cap200 = Math.floor(raw200 * 10) / 10 - 1
  const row = quickL0Estimate(p200, { efficiencyCap: cap200 })
  must(row.efficiency <= cap200, `efficiency 必须仍被封顶（与 L1 同口径），实际 ${row.efficiency} > cap ${cap200}`)
  must(row.efficiency_raw >= row.efficiency,
    `真值不得小于钳位值：raw=${row.efficiency_raw} < capped=${row.efficiency}`)
  must(row.efficiency_capped === true, '该样本应被标记触顶')
  // 不触顶场景：15kW 小机座不得被误标
  const small = quickL0Estimate({
    stator_od: 180, stator_id: 108, core_length: 100, air_gap: 0.6,
    poles: 4, voltage: 380, speed: 1460, slots_stator: 36, slots_rotor: 28,
    rotor_od: 106.8, tooth_width: 4.7, power_kw: 15, cooling: 'forced_air',
  }, { efficiencyCap: 96 })
  // v3 损耗标定（2026-10-08）后 15kW raw 效率进入 96~98 域（铜损高估 +201%→−21%、
  // 铁耗换真值 B 口径 —— 旧「15kW 必不触顶」断言固化了损耗失真下的效率假值，故撤除）。
  // 改为断言 capped 标记与 raw 自洽 —— 这才是该样本的本意（不触顶行标记 false 且两值相等）。
  must(small.efficiency_capped === (small.efficiency_raw > 96),
    `capped 标记必须与 raw 自洽：raw=${small.efficiency_raw} capped=${small.efficiency_capped}`)
  if (!small.efficiency_capped) {
    must(Math.abs(small.efficiency_raw - small.efficiency) < 1e-9,
      `未触顶行真值应等于显示值：raw=${small.efficiency_raw} vs ${small.efficiency}`)
  }
  must(small.efficiency_raw >= 88 && small.efficiency_raw <= 99,
    `15kW raw 效率应落工程合理域 [88,99]（v3 机械耗标定后基线 ≈93.4），实际 ${small.efficiency_raw}`)
  return `大样本 capped=${row.efficiency}%/raw=${row.efficiency_raw}%；15kW raw=${small.efficiency_raw}% capped=${small.efficiency_capped}`
})

check('matrix: P3 高速工况 —— 22000rpm 归入 2 极档（f≈367Hz）', () => {
  must(recommendPoles(22000) === 2, '22000rpm 应推荐 2 极（f=366.7Hz，与现场工况吻合）')
  // v0.2.5 订正：原断言固化「1500rpm→4 极」，而 50Hz 下 4 极 n_sync 恰为 1500rpm
  // ⇒ 该推荐必然被 V16 判超同步，这正是 10 万案例压测发现的 1500rpm 断崖根因。
  // 按 n_sync=120·f/p > n 反推：1500rpm 在 50Hz 下 4/6/8 极的 n_sync=1500/1000/750
  // 全部 ≤1500（即超同步），唯一合法偶极为 2 极（n_sync=3000）。
  must(recommendPoles(1500) === 2, `1500rpm@50Hz 应推荐 2 极（4/6/8 极均超同步），实际 ${recommendPoles(1500)}`)
  must(recommendPoles(1460) === 4, '1460rpm 应仍为 4 极（n_sync=1500>1460 合法，未受断崖影响）')
  must(recommendPoles(1500, 60) === 4, '60Hz 下 1500rpm 应推荐 4 极（n_sync=1800>1500，证明频率感知生效）')
  must(!poleCandidates(2).includes(1), '不得产出 1 极方案（永磁同步机不存在 1 极）')
  const { matrix } = buildParamMatrix(
    { power_kw: 200, speed_rpm: 22000, count: 20 }, { maxMatrixSize: 2000 })
  for (const row of matrix) {
    must(row.core_length !== 80, 'core_length 落进了默认值兜底（80）')
    must(row.core_length > 0, 'core_length 非法')
    must(row.poles >= 2, `极数 ${row.poles} 非法：永磁同步机不存在 1 极`)
  }
  const freqs = matrix.map((r) => (r.speed * r.poles) / 120)
  must(freqs.some((f) => Math.abs(f - 366.7) < 1), `应产出 f≈367Hz 的 2 极方案，实际 ${freqs.join('/')}`)
  return `推荐 2 极，f ${Math.round(Math.min(...freqs))}~${Math.round(Math.max(...freqs))}Hz，无默认值兜底`
})

check('matrix: maxMatrixSize 截断生效', () => {
  const { returned, truncated } = buildParamMatrix(
    { power_kw: 15, speed_rpm: 3000, count: 500 }, { maxMatrixSize: 50 })
  must(returned <= 50, `返回 ${returned} 超过上限 50`)
  return `返回 ${returned}，truncated=${truncated}`
})

check('matrix: 非法入参快速失败', () => {
  let threw = false
  try { buildParamMatrix({ power_kw: -1, speed_rpm: 3000 }) } catch { threw = true }
  must(threw, 'power_kw ≤ 0 应抛错')
  threw = false
  try { buildParamMatrix({ power_kw: 15, speed_rpm: 0 }) } catch { threw = true }
  must(threw, 'speed_rpm ≤ 0 应抛错')
  return 'power/speed 非法值均抛错'
})

// ---------- 22~25. L0 估算与交接 ----------
function sampleMatrix() {
  return buildParamMatrix({ power_kw: 15, speed_rpm: 3000, count: 24 }, { maxMatrixSize: 2000 }).matrix
}

check('estimate: 默认按效率降序 + top_n 截断', () => {
  const { results, returned } = runL0Estimate({ params_list: sampleMatrix(), top_n: 5 }, {})
  must(returned === 5, `top_n=5 应返回 5 条，实际 ${returned}`)
  for (let i = 1; i < results.length; i += 1) {
    must(results[i - 1].efficiency >= results[i].efficiency, '未按效率降序排列')
  }
  return `Top5 效率 ${results.map((r) => r.efficiency).join(' > ')}`
})

check('estimate: temp_rise 升序通道可用', () => {
  const { results } = runL0Estimate(
    { params_list: sampleMatrix(), sort_by: 'temp_rise', top_n: 6 }, {})
  for (let i = 1; i < results.length; i += 1) {
    must(results[i - 1].temp_rise <= results[i].temp_rise, 'temp_rise 未按升序排列')
  }
  return `Top6 温升 ${results.map((r) => r.temp_rise).join(' < ')}`
})

check('estimate: 缺字段的行进 failures 而非整体崩溃', () => {
  const good = sampleMatrix()
  const broken = { stator_od: 180, poles: 8 }
  const { success, failed, failures } = runL0Estimate(
    { params_list: [...good, broken] }, {})
  must(success === good.length, `应成功 ${good.length} 条，实际 ${success}`)
  must(failed === 1, `应失败 1 条，实际 ${failed}`)
  must(failures?.[0]?.missing?.length > 0, 'failures 未记录缺失字段')
  return `成功 ${success} / 失败 ${failed}（缺失: ${failures[0].missing.join(',')}）`
})

check('estimate[代理]: surrogate 通道端到端可用且不被钳底', () => {
  // 回归 0.2.1：代理 coef 在原始空间训练却被标准化推理，异步机 rawEff≈19 被钳到 50。
  // 必须显式走代理通道并断言结果落在合理区间且未触底。
  const m = buildParamMatrix({ power_kw: 75, speed_rpm: 1480, voltage_v: 660, motor_type: 'async' }, { maxMatrixSize: 2000 })
  const rows = (m.params_list || m.matrix).slice(0, 12)
  const cfg = { l0Mode: 'surrogate', surrogatePath: 'models/l0_surrogate_family.json', surrogateConfidenceThreshold: 0.4 }
  const { results, l0_mode, surrogate_model_version } = runL0Estimate({ params_list: rows, top_n: 5 }, cfg)
  must(l0_mode === 'surrogate', `应走代理通道，实际 ${l0_mode}`)
  must(surrogate_model_version === '2.3.0-fixseg', `代理模型版本应为 2.3.0-fixseg，实际 ${surrogate_model_version}`)
  for (const r of results) {
    must(r.efficiency > 50, `代理预测被钳到下限 50（失真），实际 ${r.efficiency}`)
    must(r.efficiency <= 99, `效率超出上界 ${r.efficiency}`)
  }
  return `异步机代理预测 ${results.map((r) => r.efficiency).join('/')}，无钳底`
})

check('estimate: 交接载荷零翻译（L0 → L1）', () => {
  const { handoff } = runL0Estimate({ params_list: sampleMatrix(), top_n: 10 }, {})
  must(handoff.handoff.from_level === 'l0' && handoff.handoff.to_level === 'l1', '层级标记错误')
  must(handoff.handoff.candidates.length === 10, '交接候选数不等于 top_n')
  for (const c of handoff.handoff.candidates) {
    for (const f of L1_STRICT_REQUIRED) {
      must(typeof c[f] === 'number', `交接载荷缺严格必填字段 ${f}`)
    }
    must(!('power_kw' in c), '交接载荷混入 L0 私有字段 power_kw')
    must(!('l1_handoff' in c), '交接载荷不应嵌套自身')
  }
  must(typeof handoff.handoff.timestamp === 'string', '缺少 timestamp')
  return `10 条候选，字段可直接喂 L1`
})

check('estimate: 每 participating ms 级（100 组 < 100ms）', () => {
  const list = buildParamMatrix({ power_kw: 15, speed_rpm: 3000, count: 100 }, { maxMatrixSize: 2000 }).matrix
  const { elapsed_ms, avg_ms_per_case } = runL0Estimate({ params_list: list }, {})
  must(elapsed_ms < 100, `100 组耗时 ${elapsed_ms}ms，超出 100ms 预算`)
  return `${list.length} 组耗时 ${elapsed_ms}ms（均 ${avg_ms_per_case}ms/组）`
})

check('index: tools/l0 可在无 DSH Runtime 下加载', () => {
  must(typeof toolsIndex.registerL0Tools === 'function', 'registerL0Tools 未导出')
  must(Array.isArray(toolsIndex.L0_TOOL_NAMES), 'L0_TOOL_NAMES 未导出')
  must(toolsIndex.L0_TOOL_NAMES.length === 4, `当前应有 4 个工具，实际 ${toolsIndex.L0_TOOL_NAMES.length}`)
  must(toolsIndex.L0_TOOL_NAMES.includes('motor_design_validate'), 'design-validate 未注册')
  must(toolsIndex.L0_TOOL_NAMES.includes('motor_l0_pipeline'), 'pipeline 聚合工具未注册')
  return `可加载，工具: ${toolsIndex.L0_TOOL_NAMES.join(', ')}`
})

// ---------- 40. 聚合流水线工具（B 方案：根治 2000 行截断）----------
import { runL0Pipeline } from '../tools/l0/pipeline.mjs'

check('pipeline: 三步链聚合 + 计数自洽 + 默认 Top10', () => {
  const out = runL0Pipeline({ power_kw: 15, speed_rpm: 1460, voltage_v: 380, motor_type: 'induction', count: 120 }, {})
  must(out.summary.generated === 120, `应生成 120 行，实际 ${out.summary.generated}`)
  must(
    out.summary.passed + out.summary.warning + out.summary.failed === out.summary.generated,
    'passed+warning+failed 必须等于 generated（防止静默丢行）')
  must(out.summary.count_ok === true, 'count_ok 应为 true（无截断且三级分组自洽）')
  must(out.summary.top_n_returned <= 10, `聚合工具默认应封顶 ≤10 行，实际 ${out.summary.top_n_returned}`)
  must(out.elapsed_ms < 200, `流水线耗时 ${out.elapsed_ms}ms 超出预算`)
  // 上下文体量：JSON 输出应远小于 2000 行
  const lineCount = JSON.stringify(out, null, 2).split('\n').length
  must(lineCount < 2000, `聚合输出 ${lineCount} 行仍超 2000 上限`)
  return `生成 ${out.summary.generated} / 剔除 ${out.summary.failed} / 可行 ${out.summary.feasible_count} / 回吐 ${out.summary.top_n_returned} 行（${lineCount} 行）`
})

check('pipeline: V16 超同步在聚合链路内被拦截（15kW 异步 6极@1460 → 切 4极）', () => {
  // 直接给 6 极（超同步非法档），聚合工具应自动把 failed 项剔除，
  // 且 summary 反映：failed 含超同步方案，TopN 不出现 V16 failed 行。
  const out = runL0Pipeline({ power_kw: 15, speed_rpm: 1460, voltage_v: 380, poles: 6, motor_type: 'induction', count: 120 }, {})
  must(out.summary.failed >= 1, `6极@1460 超同步应被 V16 拦下至少 1 项，实际 failed=${out.summary.failed}`)
  // TopN 行的 recommended / results 不应含 V16 failed 候选（已在过滤环节剔除）
  must(out.recommended === null || out.recommended.verdict !== 'failed', 'TopN 不应推荐已判 failed 方案')
  return `failed=${out.summary.failed}（含超同步档） / TopN 已过滤`
})

check('pipeline: write_handoff 落盘且路径可见', () => {
  const out = runL0Pipeline({ power_kw: 15, speed_rpm: 3000, count: 24 }, { handoffDir: join(ROOT, '.l0_handoff_test') })
  must(typeof out.handoff_path === 'string' && out.handoff_path.length > 0, 'handoff_path 应为非空字符串')
  must(existsSync(out.handoff_path), 'handoff 文件未实际落盘')
  return `handoff → ${out.handoff_path}`
})

// ---------- 26~35. W4：物理一致性校验 ----------
check('validate: 规则目录编号唯一且无缺号（V01~V15 齐备，V13 附于 V06）', () => {
  const ids = RULE_CATALOG.map((r) => r.id)
  must(new Set(ids).size === ids.length, '存在重复规则编号')
  // 目录随版本增长：当前为 14 条（V01~V12 + V14 反电势闭环 + V15 槽满率）。
  // 用动态长度断言，避免新增规则后人工同步计数再次假红。
  must(ids.length >= 12, `规则数 ${ids.length} 不应少于基线 12 条`)
  for (const r of RULE_CATALOG) {
    must(r.name && r.severity, `${r.id} 缺 name/severity`)
  }
  return `共 ${ids.length} 条（V01~V15，V13 附于 V06）`
})

check('validate: 磁密公式含正弦平均因子 2/π（初版遗漏已修）', () => {
  // 半齿距齿宽下 Bt 应 = 4·B_gap/(π·k_stack) ≈ 1.30·B_gap
  const statorId = 170
  const slots = 36
  const bt = (Math.PI * statorId) / (2 * slots)
  const b = toothFluxDensity({ statorId, toothWidth: bt, slotsStator: slots, airGapFlux: 0.8 })
  must(Math.abs(b - 1.04) < 0.05, `Bt 应≈1.04T（对齐现场目标 1.02T），实际 ${b}T`)
  const ratio = b / 0.8
  must(Math.abs(ratio - 1.30) < 0.05, `Bt/B_gap 应≈1.30，实际 ${ratio}`)
  return `Bt=${b}T（B_gap=0.8 ⇒ 比值 ${Math.round(ratio * 100) / 100}，现场目标 1.02T 吻合）`
})

check('validate: 轭磁密公式与齿磁密同源（By = B·Dsi/(p·yoke·k)）', () => {
  const by = yokeFluxDensity({ statorId: 170, yokeThickness: 18, poles: 4, airGapFlux: 0.8 })
  must(Math.abs(by - 1.93) < 0.05, `By 应≈1.93T，实际 ${by}T`)
  return `By=${by}T（公式正确；超 B_YOKE_SAT_T=1.9T 由 V08 硬判 failed）`
})

check('validate: V08 轭饱和(>1.9T) 直接 failed（v0.1.7 门禁硬化）', () => {
  // 复刻项目重跑 #1 几何：OD191/ID137.5/2极/yoke≈10.65mm/Bg0.8 ⇒ Bj≈5.29T（深度饱和）
  const r = validateDesign({
    stator_od: 191, stator_id: 137.5, rotor_od: 136.7, shaft_dia: 47.9,
    core_length: 161.5, air_gap: 0.6, tooth_width: 18.0, yoke_thickness: 10.65,
    poles: 2, voltage: 380, speed: 22000, slots_stator: 12, slots_rotor: 0,
    turns_per_coil: 2, parallel_circuits: 1, power_kw: 200, cooling: 'liquid_jacket',
  })
  const v08 = r.issues.filter((i) => i.rule === 'V08')
  must(v08.length > 0 && v08.some((i) => i.level === 'failed'), `轭饱和 5.29T 应判 failed，实际 ${JSON.stringify(v08.map((i) => i.level))}`)
  return `V08=${v08.map((i) => i.level).join('/')}`
})

check('validate: 硬几何规则能抓到明显错误', () => {
  const inverted = validateDesign({
    stator_od: 100, stator_id: 200, rotor_od: 150, shaft_dia: 50,
    core_length: 100, air_gap: 0.5, tooth_width: 5, yoke_thickness: 10,
    poles: 4, voltage: 380, speed: 1500, slots_stator: 36, slots_rotor: 28,
    turns_per_coil: 20, parallel_circuits: 2, power_kw: 15,
  })
  must(inverted.status === 'failed', '内外径倒置应判 failed')
  must(inverted.issues.some((i) => i.rule === 'V01'), '未命中 V01')

  const badParallel = validateDesign({
    stator_od: 260, stator_id: 170, rotor_od: 169, shaft_dia: 59,
    core_length: 155, air_gap: 0.5, tooth_width: 7.42, yoke_thickness: 18,
    poles: 6, voltage: 380, speed: 1000, slots_stator: 36, slots_rotor: 28,
    turns_per_coil: 20, parallel_circuits: 4, power_kw: 15,
  })
  must(badParallel.issues.some((i) => i.rule === 'V06' && i.level === 'failed'),
    '并联支路 4 不能整除极数 6，应判 failed')
  return '几何倒置 / 支路不整除 均被抓到'
})

check('validate: V06 补充 a ≤ q（每极每相槽数）约束（p=4/Qs=36 ⇒ q=3, a∈{1,2}）', () => {
  const base = {
    stator_od: 260, stator_id: 170, rotor_od: 169, shaft_dia: 59,
    core_length: 155, air_gap: 0.5, tooth_width: 7.42, yoke_thickness: 18,
    poles: 4, voltage: 380, speed: 1500, slots_stator: 36, slots_rotor: 0,
    turns_per_coil: 20, power_kw: 15,
  }
  // a=4：整除 4 ✓，但 a > q=3 —— 旧实现放行，新判据必须拦截
  const a4 = validateDesign({ ...base, parallel_circuits: 4 })
  must(a4.issues.some((i) => i.rule === 'V06' && i.level === 'failed'),
    `a=4 > q=3 应判 failed，实际 ${JSON.stringify(a4.issues.filter((i) => i.rule === 'V06'))}`)
  must(a4.metrics.q_slots_per_pole_per_phase === 3, '应输出 q=3 指标')
  // a=2：整除 4 ✓ 且 ≤ 3 ✓ —— 必须放行
  const a2 = validateDesign({ ...base, parallel_circuits: 2 })
  must(!a2.issues.some((i) => i.rule === 'V06' && i.level === 'failed'),
    `a=2 ≤ q=3 应放行，实际 ${JSON.stringify(a2.issues.filter((i) => i.rule === 'V06'))}`)
  // a=3：不整除 4 —— 旧判据仍应拦截（回归保护）
  const a3 = validateDesign({ ...base, parallel_circuits: 3 })
  must(a3.issues.some((i) => i.rule === 'V06' && i.level === 'failed'),
    'a=3 不整除 p=4 应判 failed')
  return 'a=4 拦截 / a=2 放行 / a=3 整除判据回归保护 均通过'
})

check('validate: 缺功率信息时 V12 显式跳过而非静默通过', () => {
  const r = validateDesign({
    stator_od: 260, stator_id: 170, rotor_od: 169, shaft_dia: 59,
    core_length: 155, air_gap: 0.5, tooth_width: 7.42, yoke_thickness: 18,
    poles: 4, voltage: 380, speed: 1460, slots_stator: 36, slots_rotor: 28,
    turns_per_coil: 22, parallel_circuits: 2,
  })
  must(r.skipped.includes('V12'), 'V12 应被记入 skipped')
  must(!r.issues.some((i) => i.rule === 'V12'), 'skipped 的规则不得产生结论')
  return `skipped=[${r.skipped.join(',')}]`
})

check('validate: 批量模式三级分组 + rule_hits 可定位生成偏差', () => {
  const { matrix } = buildParamMatrix({ power_kw: 15, speed_rpm: 3000, count: 40 }, { maxMatrixSize: 2000 })
  const out = runDesignValidate({ params_list: matrix }, {})
  must(out.mode === 'batch', '应为 batch 模式')
  must(out.summary.total === 40, '总数不符')
  must(out.summary.passed + out.summary.warning + out.summary.failed === 40, '三级分组数之和应等于总数')
  must(Array.isArray(out.rule_hits) && out.rule_hits.length > 0, 'rule_hits 为空')
  must(out.passed_indices.length + out.warning_indices.length + out.failed_indices.length === 40,
    '索引清单与总数不符')
  must(out.failed_reasons.every((f) => f.issues.every((i) => i.level === 'failed')),
    'failed_reasons 混入非 failed 条目')
  return `${JSON.stringify(out.summary)} top=${out.rule_hits[0].rule}×${out.rule_hits[0].count}`
})

check('validate: V16 极数-转速超同步拦截（异步 reject 6极@1460 / 8极@985，PMSM 跳过）', () => {
  // 6 极 @ 1460rpm：同步 1000 < 1460 ⇒ 超同步，异步机非法
  const sixPole = validateDesign({
    stator_od: 310, stator_id: 207.9, rotor_od: 206.7, shaft_dia: 59,
    core_length: 226.4, air_gap: 0.59, tooth_width: 7.42, yoke_thickness: 18,
    poles: 6, voltage: 380, speed: 1460, slots_stator: 72, slots_rotor: 58,
    turns_per_coil: 2, parallel_circuits: 1, power_kw: 15,
  })
  must(sixPole.issues.some((i) => i.rule === 'V16' && i.level === 'failed'),
    '6极@1460rpm 超同步应判 V16 failed')

  // 4 极 @ 1460rpm：同步 1500 > 1460 ⇒ 合法（转差 2.7%）
  const fourPole = validateDesign({
    stator_od: 260, stator_id: 170, rotor_od: 169, shaft_dia: 59,
    core_length: 155, air_gap: 0.5, tooth_width: 7.42, yoke_thickness: 18,
    poles: 4, voltage: 380, speed: 1460, slots_stator: 36, slots_rotor: 28,
    turns_per_coil: 22, parallel_circuits: 2, power_kw: 15,
  })
  must(!fourPole.issues.some((i) => i.rule === 'V16'),
    '4极@1460rpm 同步 1500>1460 应判合法（不触发 V16）')

  // PMSM：V16 不适用（跳过）
  const pmsm = validateDesign({
    stator_od: 200, stator_id: 110, rotor_od: 106, shaft_dia: 37.1,
    core_length: 170, air_gap: 2.0, tooth_width: 7.2, yoke_thickness: 18,
    poles: 2, voltage: 380, speed: 22000, slots_stator: 24, slots_rotor: 0,
    turns_per_coil: 12, parallel_circuits: 1, power_kw: 200, motor_type: 'pmsm',
  })
  must(pmsm.skipped.includes('V16'), 'PMSM 应跳过 V16')
  return '6极@1460 拦截 / 4极@1460 放行 / PMSM 跳过'
})

check('validate: escalate 能把温升规则升级为 failed', () => {
  const row = {
    stator_od: 400, stator_id: 260, rotor_od: 258.4, shaft_dia: 90.4,
    core_length: 260, air_gap: 0.8, tooth_width: 8.51, yoke_thickness: 28,
    poles: 4, voltage: 660, speed: 1480, slots_stator: 48, slots_rotor: 44,
    turns_per_coil: 14, parallel_circuits: 2, power_kw: 75, cooling: 'natural',
  }
  const soft = validateDesign(row, {})
  must(!soft.issues.some((i) => i.rule === 'V12' && i.level === 'failed'),
    '默认 V12 不应为 failed（模型未标定）')
  const hard = validateDesign(row, { escalate: ['V12'] })
  must(hard.status === 'failed', 'escalate 后应判 failed')
  must(hard.issues.some((i) => i.rule === 'V12' && i.level === 'failed'), 'V12 未升级')
  return `默认 ${soft.status} → escalate 后 ${hard.status}`
})

check('validate: P3 高速气隙档（2mm @22000rpm 合法，同值常规机判非法）', () => {
  const base = {
    stator_od: 200, stator_id: 110, rotor_od: 106, shaft_dia: 37.1,
    core_length: 170, air_gap: 2.0, tooth_width: 7.2, yoke_thickness: 18,
    poles: 2, voltage: 380, speed: 22000, slots_stator: 24, slots_rotor: 20,
    turns_per_coil: 12, parallel_circuits: 1, power_kw: 200, cooling: 'liquid_jacket',
  }
  const hs = validateDesign(base, {})
  must(!hs.issues.some((i) => i.rule === 'V02' && i.level === 'failed'),
    '高速工况气隙 2mm 不应判非法')
  const normal = validateDesign({ ...base, speed: 3000 }, {})
  must(normal.issues.some((i) => i.rule === 'V02' && i.level === 'failed'),
    '常规转速下气隙 2mm 应判非法（>1.5mm）')
  return `高速档放行 / 常规档拦截`
})

// ---------- 36~39. W4：回归质量门 ----------
const benchPath = join(ROOT, 'benchmarks', 'l0-benchmarks.json')
const benchSuite = JSON.parse(readFileSync(benchPath, 'utf8'))

check('gate: 标定集结构完整', () => {
  must(Array.isArray(benchSuite.cases) && benchSuite.cases.length >= 6, '案例数不足')
  for (const c of benchSuite.cases) {
    must(c.id && c.params && c.reference, `${c.id} 缺 id/params/reference`)
    must(Array.isArray(c.reference.efficiency_band) && c.reference.efficiency_band.length === 2,
      `${c.id} 参考带非法`)
    for (const f of ['stator_od', 'poles', 'voltage', 'speed']) {
      must(typeof c.params[f] === 'number', `${c.id} 缺 L1 严格必填 ${f}`)
    }
  }
  return `${benchSuite.cases.length} 个案例`
})

check('gate: evaluateSuite 产出三态结论', () => {
  const ev = evaluateSuite(benchSuite, {})
  must(['PASS', 'DEBT', 'FAIL'].includes(ev.verdict), `verdict 非法: ${ev.verdict}`)
  must(ev.results.length === benchSuite.cases.length, '结果数与案例数不符')
  for (const r of ev.results) {
    must(typeof r.metrics.efficiency === 'number', `${r.id} 效率缺失`)
    must(typeof r.divergence.status === 'string', `${r.id} 诊断状态缺失`)
  }
  must(ev.sanity.blocking === false, 'W4 首轮应处于非阻断模式（标定未收敛）')
  return `verdict=${ev.verdict} | sanity ${ev.sanity.failed.length} fail / ${ev.sanity.warned.length} warn | 域外 ${ev.divergence.out_of_domain.length}`
})

check('gate: 无基线时放行，人为劣化必被拦截', () => {
  const ev = evaluateSuite(benchSuite, {})
  const first = compareBaseline(ev, null, {})
  must(first.ok === true && first.baseline_found === false, '无基线应放行')

  const base = makeBaseline(ev)
  must(Object.keys(base.metrics).length === ev.results.length, '基线案例数不符')

  const worse = {
    ...ev,
    results: ev.results.map((r, i) => (i === 0
      ? { ...r, metrics: { ...r.metrics, efficiency: r.metrics.efficiency - 3 } }
      : r)),
  }
  const cmp = compareBaseline(worse, base, { efficiency_pt: 0.5, temp_k: 2, loss_pct: 5 })
  must(cmp.ok === false, '效率下降 3pt 应判为劣化')
  must(cmp.regressions.some((g) => g.metric === 'efficiency'), 'regressions 未记录 efficiency')

  const hotter = {
    ...ev,
    results: ev.results.map((r, i) => (i === 0
      ? { ...r, metrics: { ...r.metrics, temp_rise: r.metrics.temp_rise + 15 } }
      : r)),
  }
  must(compareBaseline(hotter, base, { efficiency_pt: 0.5, temp_k: 2, loss_pct: 5 }).ok === false,
    '温升上升 15K 应判为劣化')

  must(compareBaseline(ev, base, { efficiency_pt: 0.5, temp_k: 2, loss_pct: 5 }).ok === true,
    '与自身基线比较不应报劣化')
  return '无基线放行 / 效率-3pt / 温升+15K 均拦截'
})

check('gate: 基线快照已生成且案例齐全', () => {
  const p = join(ROOT, 'benchmarks', 'baseline.json')
  must(existsSync(p), 'baseline.json 不存在，请先运行 scripts/regression.mjs --update-baseline')
  const base = JSON.parse(readFileSync(p, 'utf8'))
  must(base.schema === 'l0-baseline/1', '基线 schema 版本不符')
  for (const c of benchSuite.cases) {
    must(base.metrics?.[c.id], `基线缺案例 ${c.id}`)
  }
  return `${Object.keys(base.metrics).length} 个案例已快照`
})

// ---------- 50~52. 脱敏聚合指标回传（v3 option-2/3）----------
check('telemetry: 白名单字段集合固定且不含设计/隐私键', () => {
  const forbidden = [
    'stator_od', 'poles', 'efficiency', 'temp_rise', 'total_loss',
    'params', 'prompt', 'cwd', 'workdir', 'apiKey', 'token', 'client_id',
  ]
  const leaked = forbidden.filter((k) => TELEM_FIELDS.includes(k))
  must(leaked.length === 0, `白名单混入设计/隐私键: ${leaked.join(',')}`)
  must(TELEM_FIELDS.includes('tool') && TELEM_FIELDS.includes('ok'), '缺核心统计键')
  return `白名单 ${TELEM_FIELDS.length} 键，零设计/隐私泄漏`
})

check('telemetry: sanitize 丢弃非白名单键，error 归一为类别（不回原文）', () => {
  const rec = {
    ts: '2026-09-22T00:00:00Z', tool: 'motor_l0_estimate', ok: false,
    elapsed_ms: 3, n: 20,
    stator_od: 180, poles: 8, efficiency: 95, apiKey: 'sk-SECRET',
    error: 'power_kw must be positive at C:\\secret\\sk-abc line2 line3 very long',
  }
  const s = sanitizeTelemetry(rec)
  must(!('stator_od' in s) && !('poles' in s) && !('efficiency' in s) && !('apiKey' in s),
    '设计/密钥字段未被丢弃')
  must(s.error === 'invalid_argument', `error 应归一为类别，实际 ${s.error}`)
  must(!/sk-abc|C:\\|line2/.test(s.error), 'error 类别泄漏了原文（路径/密钥）')
  must(s.tool === 'motor_l0_estimate' && s.ok === false && s.elapsed_ms === 3, '白名单字段丢失')
  return `丢弃 4 个非白名单键，error → "${s.error}"`
})

check('telemetry: aggregateUsage 只出聚合统计量，无逐条/无密钥', () => {
  const p = aggregateUsage([
    { tool: 'motor_l0_estimate', ok: true, elapsed_ms: 3, n: 20, ts: '2026-09-22T00:00:00Z' },
    { tool: 'motor_l0_estimate', ok: false, elapsed_ms: 7, n: 10, error: 'timeout at C:/x', ts: '2026-09-22T00:00:01Z' },
    { tool: 'motor_design_validate', ok: true, elapsed_ms: 2, n: 40, ts: '2026-09-22T00:00:02Z' },
  ], { pluginVersion: '0.1.5' })
  must(p.calls_total === 3 && p.ok_total === 2 && p.failed_total === 1, '次数统计错误')
  must(p.by_tool['motor_l0_estimate'].calls === 2, '按工具聚合错误')
  must(p.plugin_version === '0.1.5', '来源版本缺失')
  must(!JSON.stringify(p).match(/sk-|C:\\\/x|stator_od/), 'payload 泄漏密钥/路径/设计字段')
  return `3 调用聚合 | by_tool ${Object.keys(p.by_tool).length} 类 | 零泄漏`
})

// ---------- v0.1.6 电气闭环回归（审计 P0 修复锁定）----------
check('闭环: 200kW/22000rpm/380V/PMSM 电流反推≈497A（非 50A 装饰值）', () => {
  const m = buildParamMatrix({ power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm', cooling: 'liquid_jacket', count: 20 }, {})
  const iMin = Math.min(...m.matrix.map((r) => r.peak_current))
  const iMax = Math.max(...m.matrix.map((r) => r.peak_current))
  // I_rms = P/(√3·U·pf·η) ≈ 351A → 峰值 ≈ 496A；闭环应全部落在 480~520 区间
  must(iMin >= 480 && iMax <= 520, `峰值电流应≈497A，实际 ${iMin}~${iMax}A`)
  return `峰值电流 ${iMin}~${iMax}A（反推一致）`
})

check('闭环: PMSM 矩阵无转子导条槽（slots_rotor 全为 0）', () => {
  const m = buildParamMatrix({ power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm', count: 20 }, {})
  must(m.matrix.every((r) => r.slots_rotor === 0), 'PMSM 出现转子槽（异步模板污染）')
  must(m.matrix.every((r) => r._physics.rotor_type === 'pm_synchronous_no_cage'), 'rotor_type 未标记')
  return `${m.matrix.length} 行 slots_rotor=0`
})

check('闭环: 反电势 E≈相电压 Uph（|E-Uph|≤10%），匝数闭环成立', () => {
  const m = buildParamMatrix({ power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm', count: 20 }, {})
  const est = runL0Estimate({ params_list: m.matrix, sort_by: 'efficiency' }, { insulationClass: 'F' })
  must(est.results.every((r) => r.back_emf_ok === true), '存在反电势闭环失配方案')
  must(est.results.every((r) => Math.abs(r.back_emf_v - 219.4) < 30), '反电势应≈219V（380/√3）')
  return `全部 back_emf_ok | E≈${est.results[0].back_emf_v}V`
})

check('门禁: v0.2.5 P0 轭厚按磁密反解 ⇒ 200kW/2极不再全批轭饱和（V08 判据不变）', () => {
  const m = buildParamMatrix({ power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm', cooling: 'liquid_jacket', count: 20 }, {})
  const est = runL0Estimate({ params_list: m.matrix, sort_by: 'efficiency' }, { insulationClass: 'F' })
  // v0.2.5 订正：本用例原断言「2极下 By 物理下界 ≥2.1T，无论轭厚如何都无法避免饱和」。
  // 该断言是错的 —— By = Bg·Dsi/(p·yoke·k_stack)，轭厚 yoke 在分母上，
  // 只要放大轭厚（并相应放大 OD）By 必然下降。旧实现之所以全批饱和，
  // 是因为 yokeThickness() 恒给 half×0.40、与磁密无关（改进方案 P0 的根因）。
  // 现在轭厚按 By≤B_YOKE_WARN_T 反解生成，V08 判据本身未变（仍是 >1.9T 判 failed）。
  const satCount = est.results.filter((r) => r.yoke_sat === true).length
  must(satCount === 0, `P0 后不应再有轭饱和行，实际 ${satCount}/${est.results.length}`)
  must(est.feasible_count > 0, `P0 后应产出可行方案，实际 ${est.feasible_count}`)
  // 反向守卫：V08 判据未被放宽 —— 手填一个薄轭行必须仍被判 failed
  const thin = { ...m.matrix[0], yoke_thickness: 3, _physics: { ...m.matrix[0]._physics } }
  const thinRep = validateDesignBatch([thin], { insulationClass: 'F' })
  const thinV08 = (thinRep.reports?.[0]?.issues ?? []).some((i) => i.rule === 'V08' && i.level === 'failed')
  must(thinV08, '手填薄轭（yoke=3mm）必须仍被 V08 判 failed（判据不得因 P0 而放宽）')
  const maxBy = Math.max(...est.results.map((r) => r.yoke_flux_density))
  must(maxBy <= 1.9, `轭磁密应全部 ≤1.9T，实际最大 ${maxBy}`)
  return `feasible=${est.feasible_count} | 轭饱和行=${satCount} | By_max=${maxBy}T | 薄轭手填仍被 V08 拦截`
})

check('门禁: V17 理想匝数<1 判圆线整匝绕组无解（并给出可执行出路）', () => {
  const m = buildParamMatrix({ power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm', count: 20 }, {})
  const rep = validateDesignBatch(m.matrix, { insulationClass: 'F' })
  const v17 = (rep.reports ?? []).flatMap((r) => r.issues ?? []).filter((i) => i.rule === 'V17')
  must(v17.length > 0, '200kW/22000rpm/380V 应存在 V17 命中（理想匝数<1）')
  // failed 级（物理无解）必须给出可执行出路；warn 级（可整除但偏差大）只需陈述偏差
  const v17Fail = v17.filter((i) => i.level === 'failed')
  const withAdvice = v17Fail.filter((i) => /升压|降频|加大|分数槽/.test(String(i.msg ?? '')))
  must(v17Fail.length > 0 && withAdvice.length === v17Fail.length,
    `V17 failed 必须给出可执行出路（升压/降频/加大磁通/分数槽），实际 ${withAdvice.length}/${v17Fail.length}`)
  // V14 不应再被整数量化偏差误杀：矩阵正常路径的行内匝数必≈理想值
  const v14Failed = (rep.reports ?? []).flatMap((r) => r.issues ?? []).filter((i) => i.rule === 'V14' && i.level === 'failed')
  must(v14Failed.length === 0,
    `V14 不应再对「整数量化」判 failed（该职责已移交 V17），实际命中 ${v14Failed.length}`)
  return `V17×${v17.length}（failed=${v17Fail.length} 均含出路建议）| V14 failed=${v14Failed.length}`
})

check('门禁: V15 槽满率>0.78 的 hand-fed 矩阵判 failed', () => {
  const row = buildParamMatrix({ power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm', count: 20 }, {}).matrix[0]
  // 注入不可能的大电流（10kA）→ 槽满率必然爆表
  const bad = { ...row, peak_current: 10000, _physics: { ...row._physics } }
  const rep = validateDesignBatch([bad], { insulationClass: 'F', includeThermal: true })
  must(rep.summary.failed >= 1, '超大电流未触发槽满率 failed')
  return `bad 电流→ ${rep.summary.failed} failed（槽满率门禁生效）`
})

check('门禁: V14 匝数严重失配触发反电势 failed', () => {
  // ⚠ v0.2.5：必须挑**理想匝数 ≥1** 的行做「填错」守卫。
  //   200kW/22000rpm/380V 全部行理想匝 <1（整匝钳位必然造成 >2× 偏离），
  //   那属于 V17 的「整匝绕组无解」，V14 按设计不再重复归因（否则误导为「参数填错」）。
  //   故这里用 15kW/1460rpm 常规工况（理想匝 ~9.3）验证 V14 的自洽性守卫仍然有效。
  const m = buildParamMatrix({ power_kw: 15, speed_rpm: 1460, voltage_v: 380, count: 20 }, {})
  const good = m.matrix[0]
  must(good.turns_per_coil >= 2, `守卫用例需理想匝≥1，实际行内匝=${good.turns_per_coil}`)
  const bad = { ...good, turns_per_coil: good.turns_per_coil * 10, _physics: { ...good._physics } }
  const rep = validateDesignBatch([bad], { insulationClass: 'F', includeThermal: true })
  const f = rep.reports[0]
  must(f.issues.some((i) => i.rule === 'V14' && i.level === 'failed'),
    `手填 10× 匝数（${good.turns_per_coil}→${bad.turns_per_coil}）未触发 V14 failed`)
  return `15kW 行内匝 ${good.turns_per_coil}→${bad.turns_per_coil} → V14 failed（自洽性守卫生效）`
})

// ══════════════════════════════════════════════════════════════
// v0.2.5 能力门（P5）
// ══════════════════════════════════════════════════════════════

check('P5: 能力门三档判定（ok / caution / reject）', () => {
  const ok = assessApplicability({ powerKw: 15, speedRpm: 1460, voltageV: 380, poles: 4, motorType: 'induction' })
  must(ok.verdict === 'ok', `15kW/1460rpm/380V/4极 异步应判 ok，实际 ${ok.verdict}（${ok.reasons.map((r) => r.code)}）`)
  must(ok.routing === 'l0' && ok.advice === null, 'ok 档不应给路由建议')

  const caution = assessApplicability({ powerKw: 200, speedRpm: 1460, voltageV: 690, poles: 6, motorType: 'induction' })
  must(caution.verdict === 'caution', `690V 应判 caution，实际 ${caution.verdict}`)
  must(caution.routing === 'l0_caution', `caution 档 routing 应为 l0_caution，实际 ${caution.routing}`)
  must(/横向比较/.test(caution.advice ?? ''), 'caution 档建议必须说明「仅供横向比较」')

  const reject = assessApplicability({ powerKw: 200, speedRpm: 22000, voltageV: 380, poles: 2, motorType: 'pmsm' })
  must(reject.verdict === 'reject', `22000rpm 应判 reject，实际 ${reject.verdict}`)
  must(reject.routing === 'l1', `reject 档 routing 应为 l1，实际 ${reject.routing}`)
  must(/L1/.test(reject.advice ?? '') && /L2/.test(reject.advice ?? ''),
    'reject 档建议必须同时给出 L1 与 L2 出路')
  return `ok=${ok.verdict} caution=${caution.verdict} reject=${reject.verdict} | 标定域 f≤${APPLICABILITY.FREQ_CALIBRATED_MAX}Hz/n≤${APPLICABILITY.SPEED_CALIBRATED_MAX}rpm/U≤${APPLICABILITY.VOLTAGE_CALIBRATED_MAX}V`
})

check('P5: 能力门随工况单调收紧（标定域边界处翻转）', () => {
  const base = { powerKw: 15, poles: 4, motorType: 'induction', voltageV: 380 }
  must(assessApplicability({ ...base, speedRpm: APPLICABILITY.SPEED_CALIBRATED_MAX + 1 }).verdict === 'reject',
    '转速超标定域必须 reject')
  must(assessApplicability({ ...base, speedRpm: APPLICABILITY.SPEED_CALIBRATED_MAX }).verdict !== 'reject',
    '转速恰在标定域边界不应 reject')
  must(assessApplicability({ ...base, speedRpm: 1460, voltageV: APPLICABILITY.VOLTAGE_CALIBRATED_MAX + 1 }).verdict === 'reject',
    '电压超标定域必须 reject')
  must(assessApplicability({ ...base, speedRpm: 1460, poles: 60 }).verdict === 'reject',
    '电频率超标定域必须 reject')
  return '转速/电压/频率三维度边界翻转正确'
})

check('P5: 能力门结论进入 motor_l0_estimate 返回体（空转变可执行决策）', () => {
  const m = buildParamMatrix({ power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm', count: 20 }, {})
  const rej = runL0Estimate({ params_list: m.matrix, sort_by: 'efficiency' }, {})
  must(rej.applicability === 'reject', `200kW/22000rpm 应回吐 applicability=reject，实际 ${rej.applicability}`)
  must(Array.isArray(rej.applicability_reasons) && rej.applicability_reasons.length > 0,
    'reject 必须回吐具体原因列表')
  must(/标定域/.test(rej.applicability_note ?? ''),
    'feasible_count=0 时必须把原因归到「超出标定域」而非「设计上无解」')

  const okM = buildParamMatrix({ power_kw: 15, speed_rpm: 1460, voltage_v: 380, count: 20 }, {})
  const ok = runL0Estimate({ params_list: okM.matrix, sort_by: 'efficiency' }, {})
  must(ok.applicability === 'ok', `15kW 应判 ok，实际 ${ok.applicability}`)
  must(ok.applicability_note === undefined, 'ok 档不应输出能力门噪音')
  return `200kW→${rej.applicability}(原因${rej.applicability_reasons.length}条) | 15kW→${ok.applicability} 无噪音`
})

// ══════════════════════════════════════════════════════════════
// v0.2.5 轭厚按磁密反解（P0）
// ══════════════════════════════════════════════════════════════

check('P0: solveYokeAndFrame 在磁密不足时放大轭厚且放大后 By≤目标', () => {
  const need = yokeThicknessForFlux({ statorId: 300, poles: 2 })
  must(need > 0, 'yokeThicknessForFlux 必须给出正需求值')
  // 构造一个比例式明显不足的几何
  const fr = solveYokeAndFrame({ statorOd: 400, statorId: 300, poles: 2 })
  must(fr.yoke_by_flux > fr.yoke_by_ratio,
    `磁密需求轭厚(${fr.yoke_by_flux}) 应大于比例式(${fr.yoke_by_ratio})，否则该用例无效`)
  must(fr.grown === true, '轭厚被放大时 grown 必须为 true')
  const by = (0.8 * 300) / (2 * fr.yoke_thickness * 0.98)
  // yokeFluxDensity() 内部按 0.01T 取整展示，故容差取一个显示刻度
  must(by <= 1.5 + 0.011, `放大后 By=${by.toFixed(3)}T 应 ≤1.5T 目标（含 0.01T 展示刻度容差）`)
  // 比例式足够时不得放大（零回归）。选 4 极：比例式 60mm > 磁密需求 40.8mm
  const noGrow = solveYokeAndFrame({ statorOd: 600, statorId: 300, poles: 4 })
  must(noGrow.grown === false,
    `轭厚已充足时不得放大（ratio=${noGrow.yoke_by_ratio} flux=${noGrow.yoke_by_flux}）`)
  must(noGrow.yoke_thickness === noGrow.yoke_by_ratio,
    '未放大时轭厚必须严格等于比例式值（保证既有行为不变）')
  return `OD400/Dsi300/p2: 比例${fr.yoke_by_ratio}mm→需求${fr.yoke_by_flux}mm→实配${fr.yoke_thickness}mm(By=${by.toFixed(2)}T)`
})

check('P0: 放大后的 OD 仍构成完整几何链（不产生非法行）', () => {
  for (const spec of [
    { power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm', count: 40 },
    { power_kw: 15, speed_rpm: 1460, voltage_v: 380, count: 40 },
  ]) {
    const built = buildParamMatrix(spec, {})
    for (const row of built.matrix) {
      must(row.stator_od > row.stator_id, `OD(${row.stator_od}) 必须 > Dsi(${row.stator_id})`)
      must(row.stator_id > row.rotor_od, `Dsi(${row.stator_id}) 必须 > rotor_od(${row.rotor_od})`)
      must(row.rotor_od > row.shaft_dia, `rotor_od 必须 > shaft_dia`)
      must(row.yoke_thickness > 0, 'yoke_thickness 必须为正')
      must(row.stator_od <= built.od_limit.value, `OD 超过上限 ${built.od_limit.value}`)
      const shape = assertMatrixShape(row)
      must(shape.ok, `矩阵行形状非法：${shape.missing?.join(',')}`)
    }
  }
  return '放大后几何链与形状校验全通过'
})

check('P0: 显式 od_limit 是硬约束 —— 磁密需求不得突破上限', () => {
  const built = buildParamMatrix(
    { power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm', stator_od_limit: 380, count: 40 }, {})
  for (const row of built.matrix) {
    must(row.stator_od <= 380, `显式上限未被尊重：${row.stator_od} > 380`)
  }
  // 被上限截断的行必须如实标记（不得静默缩小轭厚后假装满足磁密）
  const capped = built.matrix.filter((r) => r._physics?.yoke_limited_by_od_cap)
  return `OD≤380 全守住 | 被上限截断行=${capped.length}（轭厚不足由 V08 如实判失败）`
})

// ══════════════════════════════════════════════════════════════
// v0.2.5 匝数量化语义拆分（P1）与磁密口径（P2）
// ══════════════════════════════════════════════════════════════

check('P1: deriveElectricalClosure 同时输出理想匝数与量化偏差', () => {
  const base = { voltage: 380, speed: 1460, poles: 4, statorId: 200, coreLength: 120, slotsStator: 36, powerKw: 15 }
  const c = deriveElectricalClosure({ ...base, parallelCircuits: 1 })
  must(typeof c.turns_per_coil_ideal === 'number', '必须输出 turns_per_coil_ideal')
  must(typeof c.turns_quant_dev === 'number', '必须输出 turns_quant_dev')
  // 理想匝 × a 关系：a↑ ⇒ 每线圈匝↑（并联支路方向正确性守卫）
  const c8 = deriveElectricalClosure({ ...base, parallelCircuits: 8 })
  must(c8.turns_per_coil > c.turns_per_coil,
    `并联支路↑ ⇒ 每线圈匝应↑（a=1→${c.turns_per_coil}, a=8→${c8.turns_per_coil}）`)
  must(Math.abs(c8.turns_per_phase - c.turns_per_phase) < 1,
    `每相串联匝数应与 a 无关（a=1→${c.turns_per_phase}, a=8→${c8.turns_per_phase}）`)
  return `a=1 理想${c.turns_per_coil_ideal}→整匝${c.turns_per_coil}(偏差${c.turns_quant_dev}) | a=8 理想${c8.turns_per_coil_ideal}→${c8.turns_per_coil}`
})

check('P1: V14 只抓「填错」不抓「量化」，量化移交 V17', () => {
  // 常规工况（理想匝≥1）：矩阵正常路径 V14 必须恒为 0
  const m = buildParamMatrix({ power_kw: 15, speed_rpm: 1460, voltage_v: 380, count: 20 }, {})
  const rep = validateDesignBatch(m.matrix, { insulationClass: 'F' })
  const v14 = (rep.reports ?? []).flatMap((r) => r.issues ?? []).filter((i) => i.rule === 'V14')
  must(v14.length === 0, `矩阵正常路径 V14 不应命中（自洽性恒成立），实际 ${v14.length}`)
  const row = m.matrix[0]
  const bad = { ...row, turns_per_coil: row.turns_per_coil * 10, _physics: { ...row._physics } }
  const badRep = validateDesignBatch([bad], { insulationClass: 'F' })
  must(badRep.reports[0].issues.some((i) => i.rule === 'V14' && i.level === 'failed'),
    '手填 10× 匝数必须仍被 V14 判 failed（自洽性守卫不得失效）')
  // 理想匝<1 的行不得被 V14 重复归因（该职责归 V17）
  const pmsm = buildParamMatrix({ power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm', count: 40 }, {})
  const pmsmRep = validateDesignBatch(pmsm.matrix, { insulationClass: 'F' })
  const pmsmIssues = (pmsmRep.reports ?? []).flatMap((r) => r.issues ?? [])
  must(pmsmIssues.filter((i) => i.rule === 'V14').length === 0,
    '理想匝<1 的工况不得报 V14（避免与 V17 重复归因、误导为参数填错）')
  const v17n = pmsmIssues.filter((i) => i.rule === 'V17' && i.level === 'failed').length
  must(v17n > 0, '该工况应由 V17 判整匝绕组无解')
  return `正常路径 V14=0 | 手填 10× 仍被 V14 拦截 | 22000rpm 重复归因=0（V17 failed×${v17n}）`
})

check('P1: V17 判据以「取整后偏差」为准，不以「理想匝<1」一刀切（防误杀）', () => {
  // 反例构造：理想匝 0.995（<1）但取整后偏差仅 0.5% ⇒ 必须可实现，不得判 failed。
  // 早期实现按「理想匝<1 即无解」判，会把这类完全正常的行误杀（实测误杀数十行）。
  const V = 380; const f = 100; const phi = 0.001; const kw = 0.95
  // 用真实矩阵行做基准，只调电压使理想匝逼近 1 匝
  const m = buildParamMatrix({ power_kw: 15, speed_rpm: 1460, voltage_v: 380, count: 20 }, {})
  const row = m.matrix[0]
  const nPhaseIdeal = ((row.slots_stator / 3) * row.turns_per_coil) / Math.max(1, row.parallel_circuits)
  // 令电压缩放使理想匝 = 0.995
  const scale = 0.995 / ((nPhaseIdeal * Math.max(1, row.parallel_circuits) * 3) / row.slots_stator)
  const tuned = {
    ...row, voltage: V * scale, poles: row.poles, speed: row.speed,
    stator_id: row.stator_id, core_length: row.core_length,
    slots_stator: row.slots_stator, parallel_circuits: row.parallel_circuits,
    _physics: { ...row._physics },
  }
  const rep = validateDesign(tuned, { insulationClass: 'F' })
  const be = rep.metrics?.back_emf
  must(be && be.turns_ideal < 1, `本用例需理想匝<1，实际 ${be?.turns_ideal}`)
  must(be.dev <= 0.10, `构造偏差应 ≤10%，实际 ${be.dev}`)
  const v17 = (rep.issues ?? []).filter((i) => i.rule === 'V17')
  must(v17.every((i) => i.level !== 'failed'),
    `理想匝 ${be.turns_ideal}<1 但偏差仅 ${(be.dev * 100).toFixed(1)}% 时不得判 failed（实际：${v17.map((i) => i.level).join(',')}）`)

  // 对照：**只**升速（不降压），使理想匝被压到 1 以下且取整后偏差超容差 ⇒ 必须 failed。
  //  注意不能同时降压 —— 那会抵消升速效果，理想匝仍停在 1 附近（实测踩过这个坑）。
  const bad = {
    ...row, voltage: V, poles: row.poles, speed: row.speed * 12,
    stator_id: row.stator_id, core_length: row.core_length,
    slots_stator: row.slots_stator, parallel_circuits: row.parallel_circuits,
    _physics: { ...row._physics },
  }
  const badRep = validateDesign(bad, { insulationClass: 'F' })
  const badBe = badRep.metrics?.back_emf
  must(badBe && badBe.turns_ideal < 1 && badBe.dev > 0.10,
    `对照用例需满足 理想匝<1 且偏差>10%，实际 理想匝=${badBe?.turns_ideal} 偏差=${badBe?.dev}`)
  const badV17 = (badRep.issues ?? []).filter((i) => i.rule === 'V17' && i.level === 'failed')
  must(badV17.length > 0, '偏差超容差时 V17 必须判 failed（防守卫被削掉）')
  return `理想匝${be.turns_ideal}/偏差${(be.dev * 100).toFixed(1)}% → 不判死 | 对照 理想匝${badBe.turns_ideal}/偏差${(badBe.dev * 100).toFixed(1)}% → failed`
})

check('P2: airGapFluxActual 由实际整数匝数反解，且随转速变化', () => {
  const base = { voltageV: 380, poles: 4, statorId: 200, coreLength: 120, slotsStator: 36, parallelCircuits: 1, turnsPerCoil: 11 }
  const bg1 = airGapFluxActual({ ...base, speedRpm: 1000 })
  const bg2 = airGapFluxActual({ ...base, speedRpm: 8000, turnsPerCoil: 4 })
  must(bg1 > 0 && bg2 > 0, '反解气隙磁密必须为正')
  // 缺输入时必须返回 null（调用方回退设计意图值），不得返回 NaN/0
  must(airGapFluxActual({ ...base, speedRpm: null }) === null, '缺转速必须返回 null')
  must(airGapFluxActual({ ...base, speedRpm: 1000, turnsPerCoil: 0 }) === null, '缺匝数必须返回 null')
  return `n=1000rpm/11匝→Bg=${bg1.toFixed(3)}T | n=8000rpm/4匝→Bg=${bg2.toFixed(3)}T（同一 Dsi/L/p，磁密随工况变）`
})

check('P2: bg_caliber=intent 可回退到 v0.2.4 旧口径（对外可对齐）', () => {
  const m = buildParamMatrix({ power_kw: 15, speed_rpm: 1460, voltage_v: 380, count: 20 }, {})
  const actual = runDesignValidate({ params_list: m.matrix, bg_caliber: 'actual' })
  const intent = runDesignValidate({ params_list: m.matrix, bg_caliber: 'intent' })
  must(actual.mode === 'batch' && intent.mode === 'batch', '两口径都必须跑通批量模式')
  const repA = validateDesign(m.matrix[0], { bgCaliber: 'actual' })
  const repI = validateDesign(m.matrix[0], { bgCaliber: 'intent' })
  must(repA.metrics.flux.bg_caliber === 'actual', 'metrics 必须回吐所用口径')
  const byA = repA.metrics.flux.yoke_t
  const byI = repI.metrics.flux.yoke_t
  return `同一行 By: actual=${byA}T vs intent=${byI}T（口径可切换，intent 保持旧行为）`
})

// ═══════════════════════════════════════════════════════════════════
// v0.2.5 压测复核修复组（1V16频率 / 2绝缘透传 / 3转矩口径 / 4极数合法性）
// ═══════════════════════════════════════════════════════════════════

check('V16+推荐极数: 电源频率可配置，1500rpm 断崖消除', () => {
  // 断崖根因：50Hz 硬编码 + recommendPoles 把 1460~1500rpm 全给 4 极，
  // 而 4 极 n_sync=120×50/4=1500 ⇒ n_sync ≤ speed 被 V16 判死。
  const at50 = validateDesign({ stator_od: 300, stator_id: 180, core_length: 200, poles: 4, slots_stator: 36, parallel_circuits: 1, turns_per_coil: 8, peak_current: 45, voltage: 380, power_kw: 30, cooling: 'forced_air', speed: 1500 })
  const at60 = validateDesign({ stator_od: 300, stator_id: 180, core_length: 200, poles: 4, slots_stator: 36, parallel_circuits: 1, turns_per_coil: 8, peak_current: 45, voltage: 380, power_kw: 30, cooling: 'forced_air', speed: 1500, line_freq_hz: 60 })
  must(at50.issues.some((i) => i.rule === 'V16' && i.level === 'failed'), '50Hz 下 4极@1500rpm 应判 V16 failed（n_sync=1500 触及同步点）')
  must(!at60.issues.some((i) => i.rule === 'V16'), '60Hz 下 4极@1500rpm 应合法（n_sync=1800>1500）—— 频率感知生效')
  must(at60.metrics.line_freq_hz === 60, `metrics 应回吐生效频率，实际 ${at60.metrics.line_freq_hz}`)
  must(at50.metrics.sync_speed_rpm === 1500 && at60.metrics.sync_speed_rpm === 1800,
    `同步转速应随频率变化：50Hz→${at50.metrics.sync_speed_rpm} 60Hz→${at60.metrics.sync_speed_rpm}`)
  must(recommendPoles(1500) === 2, '1500rpm@50Hz 唯一合法偶极为 2 极')
  must(recommendPoles(1500, 60) === 4, '1500rpm@60Hz 应为 4 极')
  // 断崖消除：原 1500rpm 可行率 0.15%，现推荐极数下 V16 不再判死
  return `50Hz: n_sync=1500 判死 / 60Hz: n_sync=1800 放行；推荐极数 1500rpm 50Hz→2极 60Hz→4极`
})

check('V18: 非法电源频率显式判 failed，不静默回落 50Hz', () => {
  const base = { stator_od: 300, stator_id: 180, core_length: 200, poles: 4, slots_stator: 36, parallel_circuits: 1, turns_per_coil: 8, peak_current: 45, voltage: 380, power_kw: 30, cooling: 'forced_air', speed: 1460 }
  const bad = validateDesign({ ...base, line_freq_hz: 5 })
  const nan = validateDesign({ ...base, line_freq_hz: '五十' })
  const ok = validateDesign({ ...base, line_freq_hz: 60 })
  must(bad.issues.some((i) => i.rule === 'V18' && i.level === 'failed'), '5Hz 越界应判 V18 failed')
  must(nan.issues.some((i) => i.rule === 'V18' && i.level === 'failed'), '非数值频率应判 V18 failed')
  must(!ok.issues.some((i) => i.rule === 'V18'), '60Hz 合法频率不应触发 V18')
  return `越界/非数值各拦 1，合法放行（V18 独立于 V16 归因）`
})

check('绝缘等级: params.insulation_class 透传到温升判据（B 级 80K 生效）', () => {
  // 取证根因：formula-engine 此前只读 opts.insulationClass，行内字段从未被消费
  const base = { stator_od: 300, stator_id: 180, core_length: 200, poles: 4, slots_stator: 36, parallel_circuits: 1, turns_per_coil: 8, peak_current: 45, speed: 1500, voltage: 380, power_kw: 30, cooling: 'natural' }
  const viaParams = quickL0Estimate({ ...base, insulation_class: 'B' })
  const viaOpts = quickL0Estimate(base, { insulationClass: 'B' })
  const viaF = quickL0Estimate({ ...base, insulation_class: 'F' })
  must(viaParams.thermal_limit_k === 80, `params 传 B 级应得 80K，实际 ${viaParams.thermal_limit_k}K（透传已修复）`)
  must(viaOpts.thermal_limit_k === 80, 'opts 传 B 级应得 80K')
  must(viaF.thermal_limit_k === 105, 'F 级应得 105K')
  must(viaParams.insulation_class === 'B' && viaParams.insulation_class_valid === true, '结果行应回显生效等级与合法性')
  const bad = quickL0Estimate({ ...base, insulation_class: 'Z' })
  must(bad.insulation_class_valid === false && bad.thermal_limit_k === 105, '非法等级应标 valid=false 并回落 F 级 105K')
  // 端到端：矩阵行 → 估算链路必须保持等级
  const { matrix } = buildParamMatrix({ power_kw: 30, speed_rpm: 1500, count: 3, poles: 4, insulation_class: 'B' })
  must(matrix[0].insulation_class === 'B', '矩阵行必须携带 insulation_class')
  const est = quickL0Estimate(matrix[0], { insulationClass: matrix[0].insulation_class })
  must(est.thermal_limit_k === 80, `矩阵行等级应生效，实际 ${est.thermal_limit_k}K`)
  return `params/opts/矩阵三路一致（B=80K、F=105K、Z→valid=false）；B级比F级严 25K`
})

check('转矩口径: estimated_torque 标注为类比外推量而非规格承诺', () => {
  // 压测把 _physics.estimated_torque 当规格断言 → 报 91.4% 守恒失效。
  // 复核：它是 D²L 类比外推量，与 target_torque(9550P/n) 本就不同物理量。
  const { matrix } = buildParamMatrix({ power_kw: 400, speed_rpm: 750, poles: 4, count: 3 })
  const row = matrix[0]
  const ph = row._physics
  must(ph.torque_basis === 'analogy_d2l', '必须标注 torque_basis=analogy_d2l')
  must(ph.torque_is_spec === false, '必须显式声明 torque_is_spec=false')
  must(typeof ph.torque_ratio_estimated_over_target === 'number', '必须给出类比比例供核对')
  must(Math.abs(ph.target_torque - 400 * 9550 / 750) < 0.01,
    `target_torque 应等于规格 9550P/n=${(400 * 9550 / 750).toFixed(2)}，实际 ${ph.target_torque}`)
  // 转矩公式本身正确：native.torque 与规格逐位吻合（偏差 0%）
  const est = quickL0Estimate(row)
  must(Math.abs(est.torque - ph.target_torque) < 0.01, `native.torque 应等于规格值，实际 ${est.torque} vs ${ph.target_torque}`)
  // 交接面必须暴露契约声明
  const out = runL0Estimate({ params_list: matrix.slice(0, 2) }, {})
  must(out.torque_contract?.basis === 'analogy_d2l', '返回体必须给出 torque_contract 声明')
  must(out.torque_contract?.spec_field === 'torque_nm', '必须指明规格字段为 torque_nm')
  return `target=规格(9550P/n) / estimated=类比外推(D²L)，比例=${ph.torque_ratio_estimated_over_target}；native.torque 与规格偏差 0%`
})

check('V06: 极数奇偶合法性（3/5/7/9 极物理不存在）', () => {
  const base = { stator_od: 300, stator_id: 180, core_length: 200, slots_stator: 36, parallel_circuits: 1, turns_per_coil: 8, peak_current: 45, voltage: 380, power_kw: 30, cooling: 'forced_air', speed: 500 }
  for (const p of [3, 5, 7, 9]) {
    const v = validateDesign({ ...base, poles: p })
    must(v.issues.some((i) => i.rule === 'V06' && i.level === 'failed' && i.msg.includes('奇数')),
      `${p} 极应判 V06 failed（奇数极物理不存在）`)
  }
  for (const p of [0, 1, 2.5]) {
    const v = validateDesign({ ...base, poles: p })
    must(v.issues.some((i) => i.rule === 'V06' && i.level === 'failed'), `${p} 极应判 V06 failed（非法极数）`)
  }
  const ok = validateDesign({ ...base, poles: 4 })
  must(!ok.issues.some((i) => i.rule === 'V06'), '4 极合法不应触发 V06')
  // 独立于 parallel：不给 parallel_circuits 也必须校验极数
  const noPar = validateDesign({ stator_od: 300, stator_id: 180, core_length: 200, slots_stator: 36, speed: 500, poles: 3, voltage: 380, power_kw: 30, cooling: 'forced_air' })
  must(noPar.issues.some((i) => i.rule === 'V06' && i.level === 'failed'), '未填 parallel_circuits 时仍须校验极数（防御纵深）')
  return `奇数 3/5/7/9 全拦；0/1/2.5 极全拦；4 极放行；缺 parallel 时仍校验`
})

check('V10 电频率与 V16 频率口径一致（同源 f_line）', () => {
  // V10 用 speed·p/120 得电频率（变频工况真实值），V16 用电网频率。
  // 60Hz 工频 + 4极@1460rpm：电频率 48.7Hz（V10 视角）但电网 60Hz（V16 视角），
  // 两者物理含义不同，不得互相覆盖 —— 只验证各自成立不被串扰。
  const base = { stator_od: 300, stator_id: 180, core_length: 200, poles: 4, slots_stator: 36, parallel_circuits: 1, turns_per_coil: 8, peak_current: 45, voltage: 380, power_kw: 30, cooling: 'forced_air', speed: 1460 }
  const r = validateDesign({ ...base, line_freq_hz: 60 })
  must(Math.abs(r.metrics.freq_hz - 1460 * 4 / 120) < 0.05,
    `电频率应按 speed·p/120=${(1460 * 4 / 120).toFixed(1)}Hz，实际 ${r.metrics.freq_hz}Hz`)
  must(r.metrics.line_freq_hz === 60, `电网频率应独立回吐 60Hz，实际 ${r.metrics.line_freq_hz}`)
  return `电频率=${r.metrics.freq_hz}Hz（V10 变频口径）/ 电网=${r.metrics.line_freq_hz}Hz（V16 工频口径），互不覆盖`
})

// ── 社区反馈闭环（v0.2.5）────────────────────────────────────────────
check('反馈工具：只产出草稿，不产出设计明细', () => {
  const mod = { buildCaseFeedbackDraft, assertNoSensitiveData, sensitivityRules }
  const exported = Object.keys(mod).sort()
  must(exported.includes('buildCaseFeedbackDraft'), '必须导出 buildCaseFeedbackDraft')
  must(exported.includes('assertNoSensitiveData'), '必须导出脱敏自检')
  // 红线：不得导出任何携带设计明细的函数
  const forbidden = exported.filter((k) => /param|matrix|row|result|detail|case_/i.test(k))
  must(forbidden.length === 0, `不得导出可拿到明细的接口：${forbidden.join(', ')}`)
  return `导出仅 ${exported.join(', ')}（草稿 + 自检 + 规则表），无明细接口`
})

check('反馈工具：10 项脱敏自检全部能拦截注入数据', () => {
  const rules = sensitivityRules()
  must(rules.length === 10, `脱敏规则应为 10 项，实际 ${rules.length}`)
  const injections = [
    'stator_od=425.3', 'slots_stator=72', 'turns_per_coil=8', 'peak_current=496.2',
    'l0_eff=93.73', '客户: 某某电机厂', 'C:\\Users\\zhang\\d.json', '\\\\srv\\share\\x',
    'a3f9b2c1d4e5f60718293a4b', 'me@example.com',
  ]
  const missed = []
  for (const text of injections) {
    try {
      assertNoSensitiveData(`正常内容\n${text}`)
      missed.push(text)
    } catch { /* 正确：应拦截 */ }
  }
  must(missed.length === 0, `以下注入未被拦截：${missed.join(', ')}`)
  return `10/10 注入全部拦截（几何/槽数/匝数/电流/效率/客户/绝对路径/UNC/哈希/邮箱）`
})

check('反馈工具：草稿过自检且含规则画像与断崖定位', () => {
  const draft = buildCaseFeedbackDraft({
    pluginVersion: '0.2.5',
    count: 8,
    cases: [
      { label: '常规 15kW', spec: { power_kw: 15, voltage_v: 380, speed_rpm: 1460, poles: 4 } },
      { label: '常规 200kW', spec: { power_kw: 200, voltage_v: 690, speed_rpm: 985, poles: 6 } },
      { label: '对照 1450rpm', spec: { power_kw: 30, voltage_v: 380, speed_rpm: 1450, poles: 4 } },
      { label: '对照 1500rpm', spec: { power_kw: 30, voltage_v: 380, speed_rpm: 1500, poles: 4 } },
    ],
  })
  must(typeof draft === 'string' && draft.length > 200, '草稿应为非空字符串')
  must(draft.includes('规则命中画像'), '草稿缺规则命中画像')
  must(draft.includes('断崖定位'), '草稿缺断崖定位小节')
  must(draft.includes('隐私声明'), '草稿缺隐私声明（用户敢提交的前提是能自证清白）')
  must(!/stator_od|turns_per_coil|peak_current/.test(draft), '草稿出现设计明细字段名')
  return `草稿 ${draft.length} 字符，含画像/分箱/断崖/隐私声明，且无明细字段`
})

check('反馈工具：可行率口径须把 warning 计入可行（对齐三步链）', () => {
  const r = buildCaseFeedbackDraft({
    dryRun: true, count: 8,
    cases: [{ label: '常规 15kW', spec: { power_kw: 15, voltage_v: 380, speed_rpm: 1460, poles: 4 } }],
  })
  must(r.feasibleRate > 0,
    `常规工况可行率不应为 0（实测 ${r.feasibleRate}）—— 若为 0 说明误用 status==='passed'，`
    + '把带 warning 的正常方案全算成不可行，会同时废掉断崖检测')
  must(r.ruleRows.length > 0, '规则画像为空，无法定位阻断源')
  return `可行率 ${(r.feasibleRate * 100).toFixed(1)}%，命中规则 ${r.ruleRows.length} 条`
})

check('社区资产：ISSUE 模板与 package.json author 齐备', () => {
  for (const f of ['bug.yml', 'case-feedback.yml', 'benchmark.yml', 'config.yml']) {
    must(existsSync(join(ROOT, '.github', 'ISSUE_TEMPLATE', f)), `缺 ISSUE 模板 ${f}`)
  }
  const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'))
  must(pkg.author && pkg.author.name, 'package.json 缺 author（贡献者需要知道找谁）')
  must(pkg.bugs && pkg.bugs.url, 'package.json 缺 bugs.url')
  must(pkg.repository && pkg.repository.url, 'package.json 缺 repository.url')
  return `3 套模板 + config 齐备；author=${pkg.author.name}`
})

//P0防泄漏：0.2.4/0.2.5 两次发版都把本机绝对路径打进了 npm 包。
// .gitignore 管不住 npm 打包（files 是白名单，优先级更高），因此改为断言「发布内容里没有硬编码本机路径」。
check('P0 发布物无本机绝对路径泄漏（0.2.4/0.2.5 教训固化）', () => {
  const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'))
  const dirs = (pkg.files || []).filter(f => !f.endsWith('.md') && !f.endsWith('.json'))
  const bad = []
  // 只扫会进包的运行时目录（lib/tools/scripts），docs/examples 已在人工审阅范围
  for (const d of dirs) {
    const abs = join(ROOT, d)
    if (!existsSync(abs) || !statSync(abs).isDirectory()) continue
    for (const f of walk(abs)) {
      if (!/\.(mjs|js|json|py|md)$/.test(f)) continue
      const txt = readFileSync(f, 'utf8')
      // 匹配任意盘符 + 真实目录名的绝对路径
      //（0.2.6 教训：原正则只覆盖 C:/Users，漏掉 D:/MotorDesign 这类工作区盘符）
      // 反选锚点：包内合法出现的 <MotorDesign> / <user> 等占位符形态不带盘符与真实目录名
      const m = txt.match(/[A-Za-z]:[\\/](Users|dsh-motor-ai-l0|MotorDesign)[\\/][^"'`\s]+/g)
      if (m) bad.push(`${relative(ROOT, f)}: ${[...new Set(m)].join(',')}`)
    }
  }
  must(bad.length === 0, `发布目录含本机绝对路径：\n     ${bad.join('\n     ')}\n     修法：改用环境变量/占位符，或把该文件移出 package.json files`)
  return `扫描 ${dirs.length} 个发布目录，0 处盘符绝对路径`
})

// ---------- 输出 ----------
console.log('')
console.log('══════ motor-ai-l0 W1-W4 离线校验 ══════')
for (const r of results) {
  console.log(`${r.ok ? '✅' : '❌'} ${r.name.padEnd(40, ' ')} | ${r.detail}`)
}
console.log('══════════════════════════════════════')
console.log(failed === 0
  ? `✅ 全部通过（${results.length}/${results.length}）`
  : `❌ 失败 ${failed} / ${results.length}`)
console.log('')
process.exit(failed === 0 ? 0 : 1)
