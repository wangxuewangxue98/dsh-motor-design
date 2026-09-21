/**
 * 代理模型引擎 —— L0 surrogate 预测通道（灰度集成版）
 * ===========================================================
 * 设计原则：
 *   1. **灰度安全**：默认不启用，需显式配置 l0Mode='surrogate'
 *   2. **分族模型**：异步/ PMSM 各分小/中/大型，基于 OD 自动选择
 *   3. **置信度门控**：低于 threshold 自动降级公式通道
 *   4. **格式兼容**：输出结构与 formula-engine.mjs 完全一致
 *   5. **零依赖**：只用 Node 内置能力，可被单测
 *
 * 模型文件：
 *   - models/l0_surrogate_family.json  ← 分族模型（推荐）
 *   - models/l0_surrogate_single.json  ← 单模型（备选）
 *
 * W5 阶段交付：正式 surrogate 模型权重替换 stub。
 */

import { readFileSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'
import { buildL0Result } from './param-schema.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))

/** 默认置信度阈值 */
export const DEFAULT_CONFIDENCE_THRESHOLD = 0.7

/** 模型 schema 版本校验 */
const REQUIRED_SCHEMA = ['l0_surrogate_family', 'l0_surrogate_single']
const REQUIRED_VERSION_PREFIX = '1.'

/**
 * 加载代理模型权重文件
 * @param {string} modelPath 模型文件路径（相对或绝对）
 * @returns {object} 模型对象
 */
export function loadSurrogateModel(modelPath) {
  // 支持相对路径（从 lib/ 目录）和绝对路径
  let absPath
  if (modelPath.startsWith('/') || modelPath.startsWith('C:\\') || modelPath.startsWith('file://')) {
    absPath = modelPath.replace('file://', '')
  } else if (modelPath.startsWith('models/')) {
    // 相对路径从 lib/ 目录的父目录出发（即项目根目录）
    absPath = resolve(__dirname, '..', modelPath)
  } else if (modelPath.startsWith('../')) {
    // 向上路径：从 lib/ 出发
    absPath = resolve(__dirname, modelPath)
  } else if (modelPath.startsWith('./')) {
    // 当前目录路径
    absPath = resolve(__dirname, modelPath)
  } else {
    // 默认从项目根目录出发
    absPath = resolve(__dirname, '..', modelPath)
  }
  
  try {
    const raw = readFileSync(absPath, 'utf-8')
    const model = JSON.parse(raw)
    
    // 基础校验
    if (!REQUIRED_SCHEMA.includes(model.schema)) {
      throw new Error(`模型 schema 不匹配: 期望 ${REQUIRED_SCHEMA.join('|')}, 实际 ${model.schema}`)
    }
    if (!model.version?.startsWith(REQUIRED_VERSION_PREFIX)) {
      throw new Error(`模型版本过旧: ${model.version}, 需要 >= 1.0.0`)
    }
    
    return model
  } catch (err) {
    if (err.message.includes('stub')) {
      throw new Error('代理模型仍为 stub 占位，请训练后替换模型文件')
    }
    throw new Error(`加载代理模型失败: ${err.message}`)
  }
}

/**
 * 根据定子外径选择分族模型
 * @param {number} statorOd 定子外径 mm
 * @param {string} motorType 'induction' | 'pmsm'
 * @param {object} model 模型对象
 * @returns {object|null} 匹配的族模型或 null
 */
export function selectFamilySegment(statorOd, motorType, model) {
  if (model.schema !== 'l0_surrogate_family') {
    return null
  }
  
  const segments = model[`${motorType}_segments`]
  if (!segments) return null
  
  for (const [size, seg] of Object.entries(segments)) {
    const [lo, hi] = seg.od_range
    if (statorOd >= lo && statorOd < hi) {
      return { size, ...seg }
    }
  }
  
  return null
}

/**
 * 计算特征向量（标准化）
 * @param {object} params L0 参数字典
 * @param {object} norm 归一化参数
 * @returns {number[]} 特征向量
 */
export function extractFeatures(params, norm) {
  const features = []
  const order = norm.featureOrder || norm.feature_names || []
  
  for (const feat of order) {
    const raw = params[feat]
    if (raw === undefined || raw === null) {
      throw new Error(`特征 ${feat} 缺失`)
    }
    
    // 支持两种归一化格式
    let mean, std
    if (norm.stats && norm.stats[feat]) {
      mean = norm.stats[feat].mean
      std = norm.stats[feat].std
    } else {
      const idx = order.indexOf(feat)
      mean = norm.feature_mean?.[idx] ?? 0
      std = norm.feature_std?.[idx] ?? 1
    }
    
    const normalized = std > 0 ? (raw - mean) / std : 0
    features.push(normalized)
  }
  return features
}

/**
 * 线性回归预测（单输出）
 * @param {number[]} features 特征向量
 * @param {object} coeffs 系数对象
 * @returns {number} 预测值
 */
export function predictLinear(features, coeffs) {
  let result = coeffs.coef?.[0] ?? 0
  const order = coeffs.featureOrder || coeffs.feature_names || []
  
  for (let i = 1; i < coeffs.coef.length; i++) {
    if (features[i - 1] !== undefined) {
      result += features[i - 1] * coeffs.coef[i]
    }
  }
  return result
}

/**
 * 代理模型预测单个样本（分族模型）
 * @param {object} params L0 参数字典
 * @param {object} model 模型对象
 * @param {string} [motorType] 电机类型（'induction' | 'pmsm'），不传则自动检测
 * @returns {object|null} 预测结果或 null（超出范围时）
 */
export function predictSurrogate(params, model, motorType = null) {
  const statorOd = params.stator_od
  const type = motorType ?? detectMotorType(params)

  // 分族模型
  if (model.schema === 'l0_surrogate_family') {
    const segment = selectFamilySegment(statorOd, type, model)
    if (segment) {
      return predictFromSegment(params, segment.primary, model, type)
    }
    // 超出范围，返回 null 让调用方降级
    return null
  }

  // 单模型
  if (model.schema === 'l0_surrogate_single') {
    return predictFromSingle(params, model, type)
  }

  throw new Error(`不支持的模型 schema: ${model.schema}`)
}

/**
 * 从分族模型预测
 */
function predictFromSegment(params, primary, model, motorType) {
  const norm = {
    featureOrder: primary.feature_names,
    feature_mean: primary.feature_mean,
    feature_std: primary.feature_std
  }
  const features = extractFeatures(params, norm)

  // 预测效率
  const efficiency = predictLinear(features, primary)

  // 置信度：基于 R² 和特征范围
  const confidence = estimateConfidence(features, norm, primary.cv_r2)

  return {
    efficiency: Math.round(efficiency * 100) / 100,
    temp_rise: params.temp_rise ?? 0, // 暂不支持温升预测
    total_loss: estimateTotalLoss(params, efficiency),
    confidence,
    prediction_source: 'surrogate',
    family: { motor_type: motorType, size: primary.tag?.split('_')[0] },
    cv_r2: primary.cv_r2,
  }
}

/**
 * 从单模型预测
 */
function predictFromSingle(params, model) {
  const targets = model.targets
  const effPrimary = targets.efficiency?.primary
  if (!effPrimary) {
    throw new Error('单模型缺少 efficiency 系数')
  }
  
  const norm = {
    featureOrder: effPrimary.feature_names,
    feature_mean: effPrimary.feature_mean,
    feature_std: effPrimary.feature_std
  }
  const features = extractFeatures(params, norm)
  
  // 直接预测效率
  const efficiency = predictLinear(features, effPrimary)
  
  // 置信度
  const confidence = estimateConfidence(features, norm, 0.487) // 单模型 R²≈0.49
  
  return {
    efficiency: Math.round(efficiency * 100) / 100,
    temp_rise: 0, // 单模型 temp_rise 不可用
    total_loss: estimateTotalLoss(params, efficiency),
    confidence,
    prediction_source: 'surrogate',
    family: { motor_type: detectMotorType(params), size: 'single' },
    cv_r2: 0.487,
  }
}

/**
 * 检测电机类型（简化版：基于 OD 范围）
 */
function detectMotorType(params) {
  // 简化：根据 OD 判断，实际应根据 motor_type 字段
  const od = params.stator_od
  if (od < 400) return 'pmsm'  // 高速小电机
  return 'induction'
}

/**
 * 估算总损耗（基于效率反推）
 */
function estimateTotalLoss(params, efficiency) {
  const powerKw = params.power_kw ?? 0
  if (!powerKw) return 0
  const powerW = powerKw * 1000
  return Math.round(powerW * (1 / efficiency - 1) * 100) / 100
}

/**
 * 置信度估算（基于 R² 和特征距离）
 * @param {number[]} features 标准化特征
 * @param {object} norm 归一化参数
 * @param {number} r2 R² 值
 * @returns {number} 置信度 [0, 1]
 */
function estimateConfidence(features, norm, r2) {
  // 基础置信度 = R²
  let confidence = Math.max(0, r2)
  
  // 特征范围惩罚
  let outOfRangeCount = 0
  for (let i = 0; i < features.length; i++) {
    if (Math.abs(features[i]) > 3) { // 3σ 外
      outOfRangeCount++
    }
  }
  const ratio = outOfRangeCount / features.length
  confidence *= (1 - ratio * 0.5)
  
  return Math.max(0, Math.min(1, confidence))
}

/**
 * 批量预测（带置信度门控和范围外降级）
 * @param {Array<object>} paramsList 参数列表
 * @param {object} model 模型对象
 * @param {number} confidenceThreshold 置信度阈值
 * @returns {object} { results, fallbacks, skipped }
 */
export function predictBatch(paramsList, model, confidenceThreshold = DEFAULT_CONFIDENCE_THRESHOLD) {
  const results = []
  const fallbacks = []
  let skipped = 0

  for (const params of paramsList) {
    try {
      const pred = predictSurrogate(params, model)

      if (pred === null) {
        // 超出模型范围，标记为需降级
        results.push({
          error: 'OD 超出模型训练范围',
          fallback_to_formula: true,
          confidence: 0,
          skipped: true,
        })
        skipped++
        continue
      }

      if (pred.confidence < confidenceThreshold) {
        // 置信度不足，标记降级
        pred.fallback_to_formula = true
        pred.confidence_reason = `置信度 ${pred.confidence.toFixed(2)} < 阈值 ${confidenceThreshold}`
        fallbacks.push(pred)
      }

      results.push(pred)
    } catch (err) {
      // 预测失败，标记降级
      results.push({
        error: err.message,
        fallback_to_formula: true,
        confidence: 0,
      })
      skipped++
    }
  }

  return { results, fallbacks: fallbacks.length, skipped }
}

/**
 * 构建 L0 结果（兼容公式通道格式）
 * @param {object} params 原始参数
 * @param {object} prediction 预测结果
 * @returns {object} L0 结果字典
 */
export function buildSurrogateResult(params, prediction) {
  const native = {
    efficiency: prediction.efficiency ?? 0,
    torque: 0, // L0 不预测转矩（由 D²L 类比推算）
    temp_rise: prediction.temp_rise ?? 0,
    total_loss: prediction.total_loss ?? 0,
    torque_density: 0,
    prediction_source: 'surrogate',
  }
  
  const mirror = {
    max_temp: (prediction.temp_rise ?? 0) + 25, // 环境温度 25°C
    power: 0,
    copper_loss: 0,
    iron_loss: 0,
    mechanical_loss: 0,
    solve_mode: 'l0',
    l1_efficiency_proxy: prediction.efficiency ?? 0,
    l1_temp_proxy: (prediction.temp_rise ?? 0) + 25,
  }
  
  return buildL0Result({ params, native, mirror, confidence: prediction.confidence })
}

export default {
  loadSurrogateModel,
  predictSurrogate,
  predictBatch,
  buildSurrogateResult,
  extractFeatures,
  selectFamilySegment,
  DEFAULT_CONFIDENCE_THRESHOLD,
}
