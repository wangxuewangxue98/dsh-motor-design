/**
 * 温升标定数据钩子（v0.5.4 P1-6）
 * ===========================================================
 * 存在理由：
 *   L0 温升是热负荷法**代理值**（散热面积未计散热筋、损耗三项欠标定），历史实测与真值
 *   相关性极弱 ⇒ 只能作量级判断。要让「杠杆降温 5K」变成可信结论，唯一路径是积累
 *   (参数, L0预测温升, L1真值温升) 配对数据做标定。
 *
 *   本模块负责前半段：把 L0 每次估算的 (输入参数子集, 预测温升/损耗/效率) 以 JSONL
 *   追加到本地文件，供后续与 L1/L2 真值按 (stator_od, poles, power_kw, speed) 关联。
 *
 * 设计约束（与 telemetry / experience-upload 同构）：
 *   1. **默认完全关闭**：仅当 env L0_TEMP_CALIB_LOG（或 config.tempCalibLog）给路径时才落盘；
 *   2. **失败静默**：任何 IO 异常返回 false，绝不影响主流程（标定钩子不是关键路径）；
 *   3. **脱敏**：只记几何/工况/预测值，不记用户目录、设备 id、license 信息；
 *   4. **零依赖**：node 内置 fs/path，可离线加载。
 */

import { appendFileSync, mkdirSync } from 'node:fs'
import { dirname } from 'node:path'

/**
 * 解析标定日志路径（env 优先于 config）
 * @param {object} [config] 插件配置
 * @param {object} [env] 进程环境（可注入以便测试）
 * @returns {string|null} 未配置时返回 null（调用方据此跳过）
 */
export function tempCalibPath(config = {}, env = process.env) {
  const fromEnv = env?.L0_TEMP_CALIB_LOG
  if (typeof fromEnv === 'string' && fromEnv.trim()) return fromEnv.trim()
  const fromCfg = config?.tempCalibLog
  if (typeof fromCfg === 'string' && fromCfg.trim()) return fromCfg.trim()
  return null
}

/** 只取标定需要的参数子集（脱敏白名单，与 design_exp 同思路） */
function pickParams(row) {
  if (!row || typeof row !== 'object') return null
  const keys = [
    'stator_od', 'stator_id', 'rotor_od', 'core_length', 'air_gap', 'poles',
    'slots_stator', 'tooth_width', 'yoke_thickness', 'turns_per_coil',
    'parallel_circuits', 'peak_current', 'voltage', 'speed', 'power_kw',
    'cooling', 'motor_type', 'insulation_class',
  ]
  const out = {}
  for (const k of keys) {
    const v = row[k]
    if (v !== undefined && v !== null) out[k] = v
  }
  return out
}

/**
 * 追加一条温升标定记录（JSONL）
 * @param {object} row 参数行（L1 形状）
 * @param {object} est quickL0Estimate 的返回（取 temp_rise/total_loss/efficiency_raw）
 * @param {string|null} filePath 目标文件路径；null 直接跳过
 * @returns {boolean} 是否写入成功（未配置/失败均 false）
 */
export function appendTempCalibration(row, est, filePath) {
  if (!filePath) return false
  try {
    const params = pickParams(row)
    if (!params || est?.temp_rise === undefined || est?.temp_rise === null) return false
    const rec = {
      ts: new Date().toISOString(),
      params,
      predicted: {
        temp_rise_k: est.temp_rise,
        total_loss_w: est.total_loss ?? null,
        efficiency_raw: est.efficiency_raw ?? est.efficiency ?? null,
        tooth_flux_density: est.tooth_flux_density ?? null,
        yoke_flux_density: est.yoke_flux_density ?? null,
        slot_fill_ratio: est.slot_fill_ratio ?? null,
      },
      // 真值列由 L1/L2 回填（本模块只写预测侧）
      truth: { temp_rise_k: null, source: null },
    }
    mkdirSync(dirname(filePath), { recursive: true })
    appendFileSync(filePath, `${JSON.stringify(rec)}\n`, 'utf8')
    return true
  } catch {
    return false // 钩子失败绝不影响主流程
  }
}

export default { tempCalibPath, appendTempCalibration }
