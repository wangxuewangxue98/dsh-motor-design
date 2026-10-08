/** 篇三取证：只打印顶层元信息 + 分族可区分指标（跳过 gbr.trees） */
import { readFileSync } from 'node:fs'
const m = JSON.parse(readFileSync('./models/l0_surrogate_family.json', 'utf8'))

console.log('=== 顶层元信息 ===')
for (const k of ['schema','version','experimental','target','generated_at','lambda','residual_model','base_model']) {
  console.log(`  ${k.padEnd(16)} : ${JSON.stringify(m[k])}`)
}
console.log(`  ${'features'.padEnd(16)} : ${JSON.stringify(m.features)}`)
console.log(`  ${'gate'.padEnd(16)} : ${JSON.stringify(m.gate)}`)
console.log(`  ${'segment_rule'.padEnd(16)} : ${JSON.stringify(m.segment_rule)}`)
console.log(`  ${'trained_on'.padEnd(16)} : ${JSON.stringify(m.trained_on)}`)
console.log(`  ${'data_sources'.padEnd(16)} : ${JSON.stringify(m.data_sources)}`)

console.log('\n=== 分族可区分指标 ===')
console.log('族             n      cv_r2      cv_mae_pp  l0_base_mae_pp  coef[0]   poles_coef  gbr_trees')
const all = []
for (const [mk, key] of [['INDUCTION','induction_segments'],['PMSM','pmsm_segments']]) {
  for (const [name, s] of Object.entries(m[key] || {})) {
    const p = s.primary || {}
    const g = s.gbr || {}
    all.push({ f: `${mk.slice(0,4).toLowerCase()}/${name}`, n: s.n ?? p.train_n, cv_r2: p.cv_r2, cv_mae: p.cv_mae_pp, base: p.l0_baseline_mae_pp })
    console.log(
      `${(`${mk.slice(0,4).toLowerCase()}/${name}`).padEnd(14)}` +
      `${String(s.n ?? p.train_n).padEnd(7)}` +
      `${String(p.cv_r2).slice(0,9).padEnd(11)}` +
      `${String(p.cv_mae_pp.toFixed(3)).padEnd(11)}` +
      `${String(p.l0_baseline_mae_pp.toFixed(3)).padEnd(16)}` +
      `${p.coef[0].toFixed(2).padEnd(10)}` +
      `${String(p.coef[4]).padEnd(12)}` +
      `${(g.trees || []).length}`
    )
  }
}

console.log('\n=== 跨族一致性检查（去重）===')
console.log('指标          取值数  去重数  最小~最大（或唯一值）')
for (const f of ['cv_r2','cv_mae','base','n']) {
  const vals = all.map(a => a[f])
  const uniq = [...new Set(vals.map(v => typeof v === 'number' ? v.toFixed(6) : String(v)))]
  const rng = uniq.length === 1
    ? `唯一值 ${uniq[0]}`
    : `${Math.min(...vals).toFixed(3)} ~ ${Math.max(...vals).toFixed(3)}`
  console.log(`${f.padEnd(14)}${String(vals.length).padEnd(8)}${String(uniq.length).padEnd(8)}${rng}`)
}
console.log(`\n  cv_r2 极差 = ${(Math.max(...all.map(a=>a.cv_r2)) - Math.min(...all.map(a=>a.cv_r2))).toFixed(6)}  <- 6 族完全相同`)
console.log(`  cv_mae_pp 极差 = ${(Math.max(...all.map(a=>a.cv_mae)) - Math.min(...all.map(a=>a.cv_mae))).toFixed(3)} pt  <- 有区分力`)
console.log(`  l0_base_mae_pp 极差 = ${(Math.max(...all.map(a=>a.base)) - Math.min(...all.map(a=>a.base))).toFixed(3)} pt`)
