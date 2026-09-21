/**
 * L0 工具集统一注册入口
 * ===========================================================
 * index.mjs（插件入口）在 W3 之后开始调用本模块注册 L0 工具。
 * W5（surrogate-predict）交付后在此追加即可。
 *
 * 加载安全性：本模块及其依赖的 tools 均**不在顶层** import '@deepseek-ai/dsh-tools'，
 * 因此可在无 DSH Runtime 的环境被 node 直接加载，供 scripts/verify.mjs 做离线校验。
 */

import { registerParamMatrix, buildParamMatrix, TOOL_PARAMS as MATRIX_PARAMS } from './param-matrix.mjs'
import { registerL0Estimate, runL0Estimate, TOOL_PARAMS as ESTIMATE_PARAMS } from './l0-estimate.mjs'
import {
  registerDesignValidate, runDesignValidate, TOOL_PARAMS as VALIDATE_PARAMS,
} from './design-validate.mjs'

/** 当前层全部工具名（供 index.mjs 日志与自检查询） */
export const L0_TOOL_NAMES = ['motor_param_matrix', 'motor_l0_estimate', 'motor_design_validate']

/**
 * 注册 L0 工具集
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {object} config 插件配置
 * @returns {Promise<string[]>} 已注册的工具名列表
 */
export async function registerL0Tools(ctx, config = {}) {
  await registerParamMatrix(ctx, config)
  await registerL0Estimate(ctx, config)
  await registerDesignValidate(ctx, config)
  return L0_TOOL_NAMES
}

export {
  buildParamMatrix,
  runL0Estimate,
  runDesignValidate,
  MATRIX_PARAMS,
  ESTIMATE_PARAMS,
  VALIDATE_PARAMS,
}

export default { registerL0Tools, L0_TOOL_NAMES, buildParamMatrix, runL0Estimate, runDesignValidate }
