# 物理一致性校验规则（V01~V12）

> 实现：`lib/design-rules.mjs`
> 工具：`motor_design_validate`

## 1. 三级语义

| 级别 | 含义 | 处置 |
|---|---|---|
| `failed` | 硬几何错误，物理上不成立 | **必须剔除** |
| `warning` | 软规则偏离，可制造但需说明 | 保留，汇报时提示 |
| `skipped` | 输入不足以判定 | **绝不静默通过**，单独列出 |

> `skipped` 是这套校验器的核心诚实性设计：宁可说"我没数据判"，
> 也不说"没问题"。

## 2. 规则目录

| ID | 名称 | 默认级别 | 所需输入 |
|---|---|---|---|
| V01 | 几何链自洽 | failed | stator_od / stator_id / rotor_od / shaft_dia |
| V02 | 气隙与转子外径一致 | failed | stator_id / rotor_od / air_gap |
| V03 | 定子内外径比 | warning | stator_od / stator_id / poles |
| V04 | 长径比 λ | warning | core_length / stator_id / poles |
| V05 | 极槽配合 | warning | poles / slots_stator / slots_rotor |
| V06 | 并联支路整除极数且 ≤ q=Qs/(3p) | failed | poles / parallel_circuits / slots_stator |
| V07 | 齿部磁密 | warning | stator_id / tooth_width / slots_stator / poles |
| V08 | 轭部磁密 | **failed** | stator_id / yoke_thickness / poles |
| V09 | 槽形几何 | failed | stator_od / stator_id / tooth_width / slots_stator |
| V10 | 电频率 | warning | speed / poles |
| V11 | 转子轭厚度 | failed | rotor_od / shaft_dia |
| V12 | 温升限值 | warning | power_kw 或 torque_nm + cooling |
| V14 | 反电势**自洽性** | failed | voltage / speed / poles / stator_id / core_length / slots_stator / parallel_circuits / turns_per_coil |
| V15 | 槽满率可行性 | failed | stator_od / stator_id / slots_stator / poles / peak_current / turns_per_coil / parallel_circuits |
| V16 | 极数-转速同步一致性 | failed | speed / poles / line_freq_hz |
| V17 | 匝数整量化可行性 | failed | voltage / speed / poles / stator_id / core_length / slots_stator / parallel_circuits |
| V18 | 电源频率合法性 | failed | line_freq_hz |

共 **17 条**（V01–V12 + V14~V18；V13 附于 V06）。
> 注：本文档此前停留在 12 条且把 V08 记为 warning，与代码不符（V08 自 v0.1.7 起为硬门禁 failed）。已按 `RULE_CATALOG` 校正。

### 电源频率（v0.2.5）

`n_sync = 120·f_line/poles`。**60Hz 电网与 VFD 变频工况必须显式传 `line_freq_hz`**：

- 缺省 50Hz（IEC/中国工频），合法区间 20~400Hz，越界或非数值判 **V18 failed**（不静默回落）；
- V16 随频率变化：4极@1500rpm 在 50Hz 下判死（n_sync=1500 触及同步点），60Hz 下合法（n_sync=1800）；
- `recommendPoles()` 按 `n_sync > n` 反推档位，1500rpm@50Hz → 2 极（4/6/8 极均超同步）；
- 行内 `line_freq_hz` 优先于工具入参，便于同一批内混排不同频率。

可升级为 `failed` 的规则（`ESCALATABLE_RULES`）：
V03 / V04 / V05 / V07 / V08 / V10 / V12。

## 3. 关键阈值的来历

### V02 · 气隙（含高速档）

| 场景 | 上限 |
|---|---|
| 常规（< highSpeedRpm=8000） | 1.5 mm |
| 高速（≥ 8000 rpm） | 4.0 mm |

> 现场 200kW/22000rpm 气隙 2.0mm。若沿用常规上限 1.5mm 会被判非法，
> 故新增高速档。高速档同时让位经验气隙比对（不比经验值倍数）。

### V07 · 齿磁密

目标 `FLUX_TARGET.tooth = 1.02T`（现场口径 @150°C）。
公式含正弦平均因子 2/π，详见 `formula-reference.md` 第 7 节。

> 半齿距齿宽下 `Bt ≈ 1.30 · B_gap`。若出现系统性偏离目标，
> 先怀疑**齿宽定义**（Python 侧 `bt = π·Dsi/(2·Qs)`）而非公式本身。

### V08 · 轭磁密

目标 `FLUX_TARGET.yoke = 0.82T`（@150°C）。

> ⚠ 实测全部案例轭磁密 1.83~2.72T，超目标 123~232%。
> 根因指向 Python 真源 `physics_kernel.py:601-602` 的
> `yoke = 0.4 · half` 对 4 极偏薄约 2×。**需回主线确认**。

### V09 · 槽宽深比

合理区间 `[0.20, 3.00]`。

> 下界初版设 0.30，实测 26/40 误报（梨形槽常见 0.2~0.8），已放宽到 0.20。

### V12 · 温升

按绝缘等级考核（默认 F 级 = 105K）：B=80K / F=105K / H=125K。

> 初版默认判 `failed`，实测 40/40 全 failed —— 预筛层失去意义。
> 改为默认 `warning`，需硬剔除时显式传 `escalate: ['V12']`。

> **v0.2.5 修复**：`insulation_class` 此前只从工具入参 `opts` 读取，矩阵行携带的
> `params.insulation_class` 从未被消费 ⇒ B 级用户一律按 F 级 105K 考核且无告警。
> 现取值优先级为 **行内字段 > 工具入参 > config > 默认 F**，并输出
> `insulation_class_valid` / `insulation_class_effective`；非法等级显式标 `valid=false`
> 而非静默按 F 级放行。B 级比 F 级严 25K，自然冷却场景下差异显著。

## 3.1 转矩口径（P0，勿误用）

| 字段 | 含义 | 公式 |
|---|---|---|
| `torque_nm` / `target_torque` | **规格值**（硬约束，交接用这个） | `9550·P(kW)/n(rpm)` |
| `_physics.estimated_torque` | **几何类比外推量**（可达转矩参考） | `torque_nm × (D²L)/(D²L_base)` |

两者差一个 D²L 比例，**不是同一物理量**。10 万案例压测把 `estimated_torque`
当作"承诺满足用户转矩的输出"来断言，报出 91.4% 守恒失效 —— 属断言口径错，
转矩公式本身正确（`native.torque` 与规格偏差 0%）。

为防下游误用，矩阵行已标注 `torque_basis='analogy_d2l'` / `torque_is_spec=false` /
`torque_ratio_estimated_over_target`，`motor_l0_estimate` 返回体给出 `torque_contract` 声明。

**交接给 RMxprt 时必须用 `specs[].torque_nm`，不得用 `estimated_torque` 当规格。**

## 4. 批量模式

`params_list` 传入即批量。输出：

- `summary`：`{total, passed, warning, failed}`
- `passed_indices` / `warning_indices` / `failed_indices`
- **`rule_hits`**：规则命中排行（谁被触发最多）

`rule_hits` 的价值在于**定位矩阵生成偏差**：
若 V03 大面积命中，说明 `idRatio` 分档与当前功率段不匹配，
应该去调常量而不是手工挑方案。

## 5. 使用建议

1. 先批量校验，用 `rule_hits` 看是否存在系统性偏差
2. 若有系统性偏差 → 回调常量层，不要逐条人工放行
3. 剔除 `failed` 后，把 `warning` 的原因带进 TopN 汇报
4. 只有确实需要硬卡时（如客户明确要求温升不超 80K）才用 `escalate`
