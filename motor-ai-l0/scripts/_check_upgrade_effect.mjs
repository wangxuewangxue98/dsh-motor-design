import { buildParamMatrix } from '../tools/l0/param-matrix.mjs'
import { runL0Estimate } from '../tools/l0/l0-estimate.mjs'
import { loadSurrogateModel } from '../lib/surrogate-engine.mjs'

const model = loadSurrogateModel('models/l0_surrogate_family.json')
console.log('=== model version:', model.version, '===')

// 模拟「插件加载后实际生效」的 config（来自 index.mjs schema 默认值）
const PLUGIN_DEFAULT = { l0Mode: 'auto', surrogatePath: 'models/l0_surrogate_family.json', surrogateConfidenceThreshold: 0.4 }

function test(spec, label, cfg) {
  const matrix = buildParamMatrix(spec, { maxMatrixSize: 2000, topNPreview: 20 })
  const rows = (matrix.params_list || matrix.matrix || []).slice(0, 8)
  const r = runL0Estimate({ params_list: rows, top_n: 5 }, cfg)
  console.log(`\n--- ${label} ---`)
  console.log(`  l0_mode=${r.l0_mode} model=${r.surrogate_model_version ?? '-'} fallbacks=${r.surrogate_fallbacks ?? '-'} total=${r.total}`)
  for (const x of (r.results || []).slice(0, 5))
    console.log(`   eff=${x.efficiency} src=${x.prediction_source ?? '-'} od=${x.params?.stator_od} p=${x.params?.poles}`)
}

test({ power_kw: 75, speed_rpm: 1480, voltage_v: 660, motor_type: 'async' }, 'async 75kW [插件真实默认 auto]', PLUGIN_DEFAULT)
test({ power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm' }, 'pmsm 200kW [插件真实默认 auto]', PLUGIN_DEFAULT)
test({ power_kw: 75, speed_rpm: 1480, voltage_v: 660, motor_type: 'async' }, 'async 75kW [显式 surrogate]', { l0Mode: 'surrogate', surrogatePath: 'models/l0_surrogate_family.json', surrogateConfidenceThreshold: 0.4 })
test({ power_kw: 200, speed_rpm: 22000, voltage_v: 380, motor_type: 'pmsm' }, 'pmsm 200kW [显式 surrogate]', { l0Mode: 'surrogate', surrogatePath: 'models/l0_surrogate_family.json', surrogateConfidenceThreshold: 0.4 })
