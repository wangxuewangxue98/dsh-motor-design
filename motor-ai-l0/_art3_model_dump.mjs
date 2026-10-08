/**
 * 篇三取证：把 l0_surrogate_family.json 全层级字段 dump 出来
 * 目的：核对"cv_r2 六个分族完全相同"这一结论，并找出真正可区分的指标
 * 只读，不修改任何文件
 */
import { readFileSync } from 'node:fs'

const m = JSON.parse(readFileSync('./models/l0_surrogate_family.json', 'utf8'))

console.log('=== 顶层字段 ===')
for (const k of Object.keys(m)) {
  const v = m[k]
  const t = Array.isArray(v) ? `array(${v.length})` : typeof v
  console.log(`  ${k.padEnd(20)} : ${t}`)
}

console.log('\n=== 顶层标量 ===')
for (const k of ['schema', 'version', 'target', 'generated_at', 'trained_at', 'note']) {
  if (k in m) console.log(`  ${k.padEnd(20)} : ${JSON.stringify(m[k])}`)
}
console.log(`  ${'features'.padEnd(20)} : ${JSON.stringify(m.features)}`)
console.log(`  ${'gate'.padEnd(20)} : ${JSON.stringify(m.gate)}`)
console.log(`  ${'data_sources'.padEnd(20)} : ${JSON.stringify(m.data_sources)}`)

for (const [mk, key] of [['INDUCTION', 'induction_segments'], ['PMSM', 'pmsm_segments']]) {
  const segs = m[key]
  console.log(`\n=== ${key} ===`)
  if (!segs) { console.log('  (无此键)'); continue }
  for (const [name, s] of Object.entries(segs)) {
    console.log(`\n  [${mk}/${name}]`)
    for (const k of Object.keys(s)) {
      const v = s[k]
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        if (k === 'primary' || k === 'gbr' || k === 'secondary' || k === 'residual') {
          console.log(`    ${k}:`)
          for (const kk of Object.keys(v)) {
            const vv = v[kk]
            const s2 = Array.isArray(vv) ? `[${vv.join(', ')}]` : JSON.stringify(vv)
            console.log(`      ${kk.padEnd(18)} : ${s2}`)
          }
        } else {
          console.log(`    ${k.padEnd(20)} : <obj keys=${Object.keys(v).join('|')}>`)
        }
      } else {
        console.log(`    ${k.padEnd(20)} : ${JSON.stringify(v)}`)
      }
    }
  }
}

// 跨族一致性检查
console.log('\n=== 跨族一致性检查 ===')
const all = []
for (const key of ['induction_segments', 'pmsm_segments']) {
  const segs = m[key] || {}
  for (const [name, s] of Object.entries(segs)) {
    const p = s.primary || {}
    all.push({ key, name, n: p.n ?? s.n, cv_r2: p.cv_r2 ?? s.cv_r2, cv_mae_pp: p.cv_mae_pp ?? s.cv_mae_pp, base: p.l0_baseline_mae_pp ?? s.l0_baseline_mae_pp })
  }
}
for (const f of ['cv_r2', 'cv_mae_pp', 'l0_baseline_mae_pp', 'n']) {
  const vals = all.map((a) => a[f]).filter((v) => v !== undefined)
  const uniq = [...new Set(vals.map(String))]
  console.log(`  ${f.padEnd(22)} : 取值 ${vals.length} 个, 去重 ${uniq.length} 个 -> ${uniq.slice(0, 8).join(' | ')}`)
}
