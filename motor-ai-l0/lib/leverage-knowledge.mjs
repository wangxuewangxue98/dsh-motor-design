/**
 * v0.5.5 P2-3：杠杆知识库（Leverage Knowledge Base）
 * ===========================================================
 * 把「拧哪个杠杆 → 影响哪些指标 → 典型方向 → 关联风险规则」的工程经验
 * 固化为结构化表，供 leverage_audit 增强解释、并支持独立查询。
 *
 * 设计边界：
 *   - 本库是「经验关联」，不是物理求解；精确反解仍来自 core 的 headroom。
 *   - 每条 lever 的描述都是量级/方向性指导，落地数值以 headroom/l0 校验为准。
 *   - 纯数据模块，零外部依赖（不 import core，避免循环依赖与发版耦合）。
 */

/**
 * @typedef {object} LeverKB
 * @property {string} lever        杠杆参数名（与 L1 字段对齐）
 * @property {string} label        中文名
 * @property {string[]} affects     该杠杆主要影响的指标
 * @property {string} direction     典型调节方向（增大/减小的影响）
 * @property {string} [coupling]    与其他参数的耦合提示
 * @property {string[]} risk_rules  关联的物理规则 id（Vxx）
 * @property {string} family        族：geometry / winding / electric / thermal
 */

/** @type {Record<string, LeverKB>} */
export const LEVERAGE_KB = {
  yoke_thickness: {
    lever: 'yoke_thickness',
    label: '轭厚',
    affects: ['efficiency', 'temp_rise', 'torque_density', 'mass'],
    direction: '增大轭厚 → 轭磁密↓、铁损↓、温升↓；但铜耗/材料成本↑，且挤占槽深（齿宽比变化）',
    coupling: '与 stator_od/air_gap 耦合：轭厚触顶时优先放大定子外径或降气隙磁密，而非无限加厚',
    risk_rules: ['V08', 'V03', 'V09'],
    family: 'geometry',
  },
  tooth_width: {
    lever: 'tooth_width',
    label: '齿宽',
    affects: ['tooth_flux_density', 'slot_fill_ratio', 'temp_rise', 'slot_family'],
    direction: '增大齿宽 → 齿磁密↓、但槽宽↓槽满率↑风险↑；减小则相反',
    coupling: '与 slot_family 强耦合：不同槽形族有典型齿宽比区间，越界看 V09',
    risk_rules: ['V07', 'V09', 'V15'],
    family: 'geometry',
  },
  turns_per_coil: {
    lever: 'turns_per_coil',
    label: '每线圈匝数',
    affects: ['back_emf', 'efficiency', 'temp_rise', 'slot_fill_ratio'],
    direction: '增大匝数 → 反电势↑、铜耗↑、槽满率↑；减小 → 反电势↓贴近电压极限',
    coupling: '与 voltage/parallel_circuits 联动：匝数取整后在 V14/V17 闭环里须自洽',
    risk_rules: ['V14', 'V15', 'V17'],
    family: 'winding',
  },
  air_gap: {
    lever: 'air_gap',
    label: '气隙',
    affects: ['tooth_flux_density', 'yoke_flux_density', 'cogging', 'temp_rise'],
    direction: '增大气隙 → 磁密↓、漏抗↑、定位转矩/齿槽转矩↓，但励磁电流↑效率略降',
    coupling: '与 peak_current 联动：气隙增大需更多磁动势，电流密度随之上行',
    risk_rules: ['V02', 'V07', 'V08'],
    family: 'geometry',
  },
  core_length: {
    lever: 'core_length',
    label: '铁心长度',
    affects: ['torque_density', 'flux', 'temp_rise', 'mass', 'lambda'],
    direction: '增长铁心 → 磁通↑、转矩密度↑、温升↓；但长径比 λ 越界风险↑、材料↑',
    coupling: '与 stator_id 决定长径比 λ（V04）；与功率密度权衡',
    risk_rules: ['V03', 'V04', 'V07', 'V08'],
    family: 'geometry',
  },
  slots_stator: {
    lever: 'slots_stator',
    label: '定子槽数',
    affects: ['winding_factor', 'tooth_pitch', 'slot_fill_ratio', 'cogging'],
    direction: '增槽 → 绕组系数↑、齿距↓、齿槽转矩频率↑幅值↓；但工艺复杂度↑',
    coupling: '与 poles 决定极槽配合（V05）、与 parallel_circuits 决定整除（V06）',
    risk_rules: ['V05', 'V06', 'V09'],
    family: 'geometry',
  },
  slot_family: {
    lever: 'slot_family',
    label: '槽形族',
    affects: ['slot_fill_ratio', 'leakage_reactance', 'tooth_flux_density', 'manufacturability'],
    direction: '离散族切换（梨形/梯形/开口矩形/斜肩梨形/闭口），非连续杠杆；'
      + '开口矩形→槽满率高漏抗小，梨形→工艺好漏抗大齿磁密易偏高',
    coupling: '与 tooth_width/peak_current 联动：族决定典型齿宽比，进而约束可用匝数与电流密度',
    risk_rules: ['V09', 'V07', 'V15'],
    family: 'geometry',
  },
  parallel_circuits: {
    lever: 'parallel_circuits',
    label: '并联支路数',
    affects: ['turns_per_coil', 'current_density', 'slot_fill_ratio'],
    direction: '增支路 → 每支路匝数↓、电流密度↓；但须满足 slots/poles 整除（V06）',
    coupling: '直接改写 turns_per_coil 可行域，联动 V14/V15/V17',
    risk_rules: ['V06', 'V15', 'V14'],
    family: 'winding',
  },
  peak_current: {
    lever: 'peak_current',
    label: '峰值电流',
    affects: ['current_density', 'slot_fill_ratio', 'temp_rise', 'torque'],
    direction: '增电流 → 转矩↑但电流密度↑、温升↑、槽满率↑风险↑',
    coupling: '与 turns_per_coil/parallel_circuits 决定导体截面积与槽满率',
    risk_rules: ['V15', 'V12'],
    family: 'electric',
  },
  voltage: {
    lever: 'voltage',
    label: '相电压',
    affects: ['back_emf_margin', 'turns_per_coil'],
    direction: '升电压 → 反电势余量↑、可减匝数；降电压 → 匝数需增以保反电势',
    coupling: '与 turns_per_coil 在 V14 反电势闭环里直接耦合',
    risk_rules: ['V14', 'V17'],
    family: 'electric',
  },
  speed: {
    lever: 'speed',
    label: '转速',
    affects: ['freq', 'back_emf', 'power', 'temp_rise'],
    direction: '升速 → 频率↑、反电势↑（同匝数下）、功率↑；铁损随频率↑',
    coupling: '与 poles 决定同步频率（V10/V16）；与 voltage 决定反电势窗口',
    risk_rules: ['V10', 'V16', 'V14'],
    family: 'electric',
  },
  stator_od: {
    lever: 'stator_od',
    label: '定子外径',
    affects: ['yoke_flux_density', 'torque_density', 'mass', 'slot_depth'],
    direction: '放大外径 → 轭厚空间↑、轭磁密↓、转矩密度↑；但体积/成本↑',
    coupling: '轭饱和（V08）触顶时的首选解，优于无限加轭厚',
    risk_rules: ['V08', 'V09', 'V01'],
    family: 'geometry',
  },
}

/** 杠杆查表：归一化 key 后返回知识条目或 null（未知杠杆静默回落） */
export function getLeverageHint(lever) {
  if (lever == null || typeof lever !== 'string') return null
  const key = String(lever).toLowerCase().trim()
  return LEVERAGE_KB[key] ?? null
}

/** 列出全部杠杆知识（供 leverage_kb 查询模式返回全表） */
export function listLeverageKb() {
  return Object.values(LEVERAGE_KB).map((k) => ({ ...k }))
}
