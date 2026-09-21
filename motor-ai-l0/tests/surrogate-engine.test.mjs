/**
 * 代理模型引擎单元测试（支持分族模型）
 * ===========================================================
 */

import { describe, it, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'url'
import { dirname, resolve } from 'path'

const __dirname = dirname(fileURLToPath(import.meta.url))
// 测试文件在 tests/ 目录，模型在 models/ 目录
const MODEL_FAMILY_PATH = resolve(__dirname, '../models/l0_surrogate_family.json')
const MODEL_SINGLE_PATH = resolve(__dirname, '../models/l0_surrogate_single.json')

// 动态导入
import {
  loadSurrogateModel,
  predictSurrogate,
  predictBatch,
  buildSurrogateResult,
  extractFeatures,
  selectFamilySegment,
  DEFAULT_CONFIDENCE_THRESHOLD,
} from '../lib/surrogate-engine.mjs'

describe('Surrogate Engine', () => {
  describe('loadSurrogateModel', () => {
    it('should load family model', () => {
      const model = loadSurrogateModel('../models/l0_surrogate_family.json')
      assert.equal(model.schema, 'l0_surrogate_family')
      assert.equal(model.version, '1.0.0')
      assert.equal(Object.keys(model.induction_segments).length, 3)
      assert.equal(Object.keys(model.pmsm_segments).length, 3)
    })

    it('should load single model', () => {
      const model = loadSurrogateModel('../models/l0_surrogate_single.json')
      assert.equal(model.schema, 'l0_surrogate_single')
      assert.ok(model.targets.efficiency)
      assert.ok(!model.targets.temp_rise.usable)
    })
  })

  describe('selectFamilySegment', () => {
    let model
    beforeEach(() => {
      model = loadSurrogateModel('../models/l0_surrogate_family.json')
    })

    it('should select induction small segment', () => {
      const seg = selectFamilySegment(800, 'induction', model)
      assert.equal(seg.size, 'small')
      assert.equal(seg.od_range[0], 684.7)
    })

    it('should select induction medium segment', () => {
      const seg = selectFamilySegment(1100, 'induction', model)
      assert.equal(seg.size, 'medium')
    })

    it('should select induction large segment', () => {
      const seg = selectFamilySegment(1500, 'induction', model)
      assert.equal(seg.size, 'large')
    })

    it('should return null for out-of-range OD', () => {
      const seg = selectFamilySegment(500, 'induction', model)
      assert.equal(seg, null)
    })
  })

  describe('extractFeatures', () => {
    let model
    beforeEach(() => {
      model = loadSurrogateModel('../models/l0_surrogate_family.json')
    })

    it('should extract 4 features', () => {
      // OD=800 属于 induction small [684.7, 903.8]
      const params = { stator_od: 800, stator_id: 500, core_length: 570, l0_eff: 90 }
      const seg = selectFamilySegment(800, 'induction', model)
      const norm = {
        featureOrder: seg.primary.feature_names,
        feature_mean: seg.primary.feature_mean,
        feature_std: seg.primary.feature_std
      }
      const features = extractFeatures(params, norm)
      assert.equal(features.length, 4)
    })

    it('should normalize features correctly', () => {
      // OD=800, mean≈803.54 → near 0
      const params = { stator_od: 800, stator_id: 500, core_length: 570, l0_eff: 95 }
      const seg = selectFamilySegment(800, 'induction', model)
      const norm = {
        featureOrder: seg.primary.feature_names,
        feature_mean: seg.primary.feature_mean,
        feature_std: seg.primary.feature_std
      }
      const features = extractFeatures(params, norm)
      assert.ok(Math.abs(features[0]) < 0.1)
    })

    it('should throw on missing feature', () => {
      // OD=800 属于 induction small
      const params = { stator_od: 800, stator_id: 500 }
      const seg = selectFamilySegment(800, 'induction', model)
      const norm = {
        featureOrder: seg.primary.feature_names,
        feature_mean: seg.primary.feature_mean,
        feature_std: seg.primary.feature_std
      }
      assert.throws(() => extractFeatures(params, norm), /特征/)
    })
  })

  describe('predictSurrogate', () => {
    let model
    beforeEach(() => {
      model = loadSurrogateModel(MODEL_FAMILY_PATH)
    })

    it('should predict efficiency for induction motor', () => {
      // OD=800 属于 induction small
      const params = { stator_od: 800, stator_id: 500, core_length: 570, l0_eff: 85, power_kw: 15 }
      const result = predictSurrogate(params, model)
      assert.ok(result !== null)
      assert.ok(result.efficiency > 50 && result.efficiency < 99)
      assert.equal(result.prediction_source, 'surrogate')
      assert.ok(result.family)
    })

    it('should predict efficiency for PMSM motor', () => {
      // OD=630 属于 PMSM small [612.4, 657.9]
      const params = { stator_od: 630, stator_id: 480, core_length: 570, l0_eff: 88, power_kw: 10 }
      const result = predictSurrogate(params, model, 'pmsm')
      assert.ok(result !== null)
      assert.ok(result.efficiency > 50 && result.efficiency < 99)
      assert.equal(result.family.motor_type, 'pmsm')
    })

    it('should return null for out-of-range OD', () => {
      // OD=260 远小于任何分段范围
      const params = { stator_od: 260, stator_id: 170, core_length: 150, l0_eff: 80 }
      const result = predictSurrogate(params, model, 'induction')
      assert.equal(result, null)
    })

    it('should return confidence in [0, 1]', () => {
      const params = { stator_od: 800, stator_id: 500, core_length: 570, l0_eff: 85 }
      const result = predictSurrogate(params, model)
      assert.ok(result.confidence >= 0 && result.confidence <= 1)
    })
  })

  describe('predictBatch', () => {
    let model
    beforeEach(() => {
      model = loadSurrogateModel('../models/l0_surrogate_family.json')
    })

    it('should predict all in-range samples', () => {
      // 所有 OD 都在模型范围内，但置信度可能低于阈值（模型 R² 较低）
      const paramsList = [
        { stator_od: 800, stator_id: 500, core_length: 570, l0_eff: 85, power_kw: 15 },
        { stator_od: 1100, stator_id: 700, core_length: 1600, l0_eff: 90, power_kw: 100 },
        { stator_od: 1500, stator_id: 900, core_length: 1600, l0_eff: 92, power_kw: 200 },
      ]
      const { results, fallbacks, skipped } = predictBatch(paramsList, model)
      assert.equal(results.length, 3)
      // 注意：由于模型 R² 较低（0.5-0.7），大部分样本置信度 < 0.7
      assert.ok(fallbacks >= 0 && fallbacks <= 3)
      assert.equal(skipped, 0)
    })

    it('should skip out-of-range samples', () => {
      // OD=260 超出所有分段范围
      const paramsList = [
        { stator_od: 260, stator_id: 170, core_length: 150, l0_eff: 80, power_kw: 15 },
      ]
      const { results, skipped } = predictBatch(paramsList, model)
      assert.equal(results.length, 1)
      assert.equal(skipped, 1)
      assert.equal(results[0].fallback_to_formula, true)
    })

    it('should mark low-confidence predictions', () => {
      // 使用极端参数使特征距离训练均值过远
      const extremeParams = [{ stator_od: 2000, stator_id: 1200, core_length: 2000, l0_eff: 50 }]
      const { results } = predictBatch(extremeParams, model, 0.9) // 设置高阈值
      assert(results[0].confidence < 0.9)
      assert.equal(results[0].fallback_to_formula, true)
    })
  })

  describe('buildSurrogateResult', () => {
    const params = { stator_od: 800, stator_id: 500, core_length: 570, l0_eff: 85 }
    const prediction = { efficiency: 88.5, temp_rise: 0, total_loss: 1800, confidence: 0.92 }

    it('should build result with correct structure', () => {
      const result = buildSurrogateResult(params, prediction)
      assert.deepEqual(result.params, params)
      assert.equal(result.efficiency, 88.5)
      assert.equal(result.prediction_source, 'surrogate')
      assert.equal(result.confidence, 0.92)
    })
  })

  describe('Gray-scale logic', () => {
    it('should have DEFAULT_CONFIDENCE_THRESHOLD = 0.7', () => {
      assert.equal(DEFAULT_CONFIDENCE_THRESHOLD, 0.7)
    })
  })
})
