---
name: motor-l0-estimate
description: >
  Use this skill when the user needs rapid electric motor (PMSM / induction)
  design pre-screening: turn a customer spec into a parameter matrix, run
  millisecond-level L0 performance estimation (efficiency, torque, temperature
  rise, loss breakdown), validate physical consistency, and rank TopN
  candidates for downstream RMxprt / Motor-CAD verification.
  Triggers include: motor design / estimation / sizing / selection requests,
  电机设计、电机估算、电机选型、参数矩阵、方案初筛、效率与温升估算,
  spec-to-candidate screening for 200kW-class high-speed PMSM or Y-series
  induction machines. This is the L0 fast pre-screening layer — it does NOT
  call RMxprt or Motor-CAD, needs no Python, no license, no external solver.
whenToUse: >
  Load when the user asks to design, estimate, size, or pre-screen an electric
  motor from a spec (power / speed / voltage), or asks for a candidate
  parameter matrix, L0 efficiency/temperature-rise estimation, physical
  consistency validation, or a ranked TopN shortlist before RMxprt / Motor-CAD
  verification. Covers both PMSM (incl. high-speed 22000rpm class) and
  induction machines. Do NOT load for finite-element post-processing or
  final design sign-off — those belong to L1/L2.
user-invocable: true
metadata:
  author: Motor-AI
  version: 0.1.4
  level: l0
  tier: free
---

# 电机设计专家 · L0 快速预筛

## 定位与边界

L0 是**广筛层**：用纯 JS 经验公式在毫秒级把成百上千个参数组合压缩成 TopN，
交给 L1（RMxprt 磁路法，秒级）精算、L2（Motor-CAD，分钟级）校核。

**能做到**：参数矩阵生成、性能估算、物理一致性校验、TopN 排序与交接。
**做不到**：有限元精算、热耦合、真实空载/负载曲线。任何"这台电机最终效率多少"
的结论必须由 L1/L2 给出 —— L0 的数字只用于**排序**，不用于**交付**。

> 硬规则：**不得**把 L0 输出直接当成设计结论报给用户。
> 每次汇报必须带上 `solve_mode='l0'` 标记，并说明这是预筛结果。

## 字段契约（不可违反）

所有工具的输入输出字段名以 `lib/param-schema.mjs` 为唯一真源，
真源锚定 `Scripts/physics_kernel.py:618-646`（矩阵字段）与
`Scripts/motor_tools.py:726-778`（求解输出字段）。

- **L1 严格必填（缺一即拒）**：`stator_od`、`poles`、`voltage`、`speed`
- **L0 私有字段**：`power_kw`、`torque_nm`、`cooling` —— 交接给 L1 时必须剔除
- **L1 镜像字段**：`max_temp`、`power`、`copper_loss`、`iron_loss`、
  `mechanical_loss`、`solve_mode`、`l1_efficiency_proxy`、`l1_handoff`

> 禁止在对话里手写字段名后直接调用工具。字段名以 Param 锁为准，
> 别名（`power`/`rpm`/`slots` 等）由 `normalizeSpec` 自动归一，不需要手工转换。

## Pipeline（五阶段，严格按序）

### Phase 1 · 需求解析

从用户描述中抽取结构化规格。缺项**先问再做**，不要猜。

| 字段 | 必填 | 说明 |
|---|---|---|
| `power_kw` | ✅ | 额定功率 kW |
| `speed_rpm` | ✅ | 额定转速 rpm |
| `voltage` | ✅ | 电压 V（380 / 660 / …） |
| `poles` | ⬜ | 极数，缺省由 `recommendPoles(speed)` 推荐 |
| `cooling` | ⬜ | 冷却方式，缺省 `forced_air` |
| `stator_od_limit` | ⬜ | 外径上限 mm（机座号约束） |
| `count` | ⬜ | 组合数，缺省 24 |

**极数推荐口径**：高速（≥12000rpm）一律 2 极。
永磁同步机**不存在 1 极** —— 若用户口径出现 1 极，按 2 极处理并说明
（依据 f = n·p/120：22000rpm × 2 / 120 = 366.7Hz，与现场工况吻合）。

### Phase 2 · 参数矩阵生成

调用 `motor_param_matrix`。产出每一行含 16 个 L1 矩阵字段 +
`_physics` 派生量（`d_squared_l` / `lambda` / `lambda_valid` / `estimated_torque`）。

生成策略是**聚焦扫描**（复刻 `physics_kernel.focused_scan`），不是笛卡尔积：
内径 D 分档 → 反算外径 → 极数候选 → 槽配合轮询 → 匝数扰动 → λ 夹紧。

> 详见 `references/param-matrix.md`。

### Phase 3 · 物理一致性校验

调用 `motor_design_validate`（批量模式）。三级语义：

- `failed` —— 硬几何错误（内外径倒置、并联支路不整除极数等），**必须剔除**
- `warning` —— 软规则（磁密偏离目标、气隙偏大、槽宽深比异常），**可保留但需说明**
- `passed` —— 无问题

输入不足以判定的规则会进 `skipped`，**绝不静默通过**。
若要把软规则临时升级为硬剔除，传 `escalate: ['V12']` 等。

> 详见 `references/validation-rules.md`。

### Phase 4 · L0 估算

调用 `motor_l0_estimate`，对剔除后的组合做毫秒级估算。
输出含原生 6 字段（`efficiency` / `torque` / `temp_rise` / `total_loss` /
`torque_density` / `prediction_source`）+ L1 镜像字段。

**排序口径**：默认按 `efficiency` 降序；`temp_rise` / `total_loss` 为升序
（越小越好）。可用 `sort_by` 切换。

> 公式与系数来源详见 `references/formula-reference.md`。

### Phase 5 · 排序与输出

输出 TopN 候选，并附 `handoff` 交接载荷（`from_level='l0'` → `to_level='l1'`）。
汇报时必须包含：

1. **TopN 表**（效率 / 温升 / 转矩密度 / 主要几何）
2. **被剔除的原因摘要**（`rule_hits` 排行，定位矩阵生成偏差）
3. **L1 升级提示**：TopN 已按 L1 入参格式备好，可直接进 RMxprt 精算
4. **口径声明**：本轮为 L0 预筛（`solve_mode='l0'`），非最终设计结论

> 交接格式详见 `references/l1-l2-handoff.md`。

## 已知精度边界（必须如实告知）

L0 目前处于**标定欠账**状态（质量门判定 DEBT）：
6 个标定案例效率低于参考带 0.35~2.96pt，温升普遍偏保守。
根因是损耗模型未用 RMxprt 批量结果标定（铁损未分齿轭 / 铜损缺电路约束 /
机械损未标定 / 散热筋未计）。

因此 L0 结果的正确使用方式是**排序与相对比较**，
不是**绝对值采信**。排序稳定性优于绝对精度。

## 禁止事项

- ❌ 把 L0 效率数字当作交付值报给客户
- ❌ 声称 L0 调用了 RMxprt / Motor-CAD（它没有）
- ❌ 手工改写字段名绕过 Param 锁
- ❌ 越过 `level` 总闸开启 l1/l2（会触发 `assertLevelImplemented` 快速失败）
- ❌ 产出 1 极方案（永磁同步机物理上不存在）
