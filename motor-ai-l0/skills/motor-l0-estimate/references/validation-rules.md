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
| V08 | 轭部磁密 | warning | stator_id / yoke_thickness / poles |
| V09 | 槽形几何 | failed | stator_od / stator_id / tooth_width / slots_stator |
| V10 | 电频率 | warning | speed / poles |
| V11 | 转子轭厚度 | failed | rotor_od / shaft_dia |
| V12 | 温升限值 | warning | power_kw 或 torque_nm + cooling |

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
