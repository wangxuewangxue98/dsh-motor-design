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
  version: 0.2.9
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

## 能力门（v0.2.5）：调用前必须先判适用域

L0 的几何/磁密/反电势模型按**工频中压异步电机**标定。超出标定域时它不会给出错误数字，
而是把整批判死、`recommended = null` —— 调用方只看到「无解」，无法区分
「设计真的不可行」与「这工具根本不适用」。

因此 `motor_l0_estimate` 返回体带 `applicability` 字段（三档）：

| 档 | 含义 | 该怎么用 |
|---|---|---|
| `ok` | 在标定域内 | 正常用 L0 排序 |
| `caution` | 边界附近 / PMSM | 结果**仅供横向比较**，不可作定量结论 |
| `reject` | 超出标定域 | **停止调 L0**，按 `applicability_advice` 改工况或直接进 L1/L2 |

标定域（实测划定，可调 `lib/applicability-gate.mjs` 的 `APPLICABILITY`）：
- 电频率 `f = n·p/120 ≤ 400Hz`
- 转速 `n ≤ 3000rpm`（无转子强度/挠度/轴承寿命校核）
- 电压 `U ≤ 690V`
- PMSM 无 dq/弱磁/退磁/转矩脉动模型 ⇒ **恒为 caution**

> **处理 `reject` 的硬规则**：`applicability_note` 会把 `feasible_count=0` 明确归因为
> 「超出 L0 标定域」而非「设计上无解」。此时**不得**把它报告成「该工况做不出来」，
> 也**不得**反复调参试图让 L0 出方案 —— 正确动作是按 `applicability_advice` 路由到 L1/L2。

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
| `stator_od_limit` | ⬜ | 外径上限 mm（机座号/隔爆外壳约束）。缺省按基准尺寸自伸缩，夹紧时回吐 `OD_LIMIT_CLAMPED` 告警 |
| `count` | ⬜ | 组合数，缺省 24 |

**极数推荐口径**：高速（≥12000rpm）一律 2 极。
永磁同步机**不存在 1 极** —— 若用户口径出现 1 极，按 2 极处理并说明
（依据 f = n·p/120：22000rpm × 2 / 120 = 366.7Hz，与现场工况吻合）。

> ⚠️ **推荐入口：`motor_l0_pipeline` 一步跑完三步链**。DSH 对话上下文有 **2000 行读取上限**：
> `count=120` 时单 `motor_param_matrix` 就回显 4000+ 行，模型静默丢行后计数对不上 120 还**无报错**——
> 这是「让模型亲手编排三步链」的结构性风险（15kW/75kW/200kW 三案例实战全中）。
> 聚合工具在插件进程内串完 `buildParamMatrix → runDesignValidate → runL0Estimate`，
> 只回吐紧凑摘要（计数自检 `summary.count_ok` + Top10 + 交接载荷 + `handoff_path`），上下文恒 < 2000 行。
> **仅当单独调试某一步（查完整校验报告、手调 failed 阈值）才拆开用三个独立工具**，并务必把完整矩阵落盘到文件而非在对话里搬运。

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

调用 `motor_l0_estimate`（或在上游直接用聚合工具 `motor_l0_pipeline` 一步拿结果），对剔除后的组合做毫秒级估算。
注意 `motor_l0_estimate` 输出**默认封顶 Top10**（`top_n:0` 才取全量），避免回吐全量矩阵触发 DSH 截断。
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

## 结果文件输出（Excel 条件降级 —— 必须遵守）

汇报 TopN 时**默认输出 Markdown 表格**（直接在回复里给表，或写 `.md` 文件）。
只有同时满足以下条件才生成 `.xlsx`：

1. 用户**明确要求** Excel/表格文件；且
2. 运行环境已确认存在可用的 Excel 生成工具（此前调用成功过，或环境清单里有 office 工具）。

**降级规则**：Excel 生成一旦报错（如 `sheets must be an array`、`zagens-office
binary not found`、`SetNamedSecurityInfoW failed`），**立即改写 `.md` 文件完成交付**，
不得换参数重试、不得另寻下载链接、不得把报错抛给用户。交付物里注明
「Excel 工具不可用，已降级为 Markdown」即可。

理由：L0 结果的消费方是人和 L1 交接链路，Markdown 表完全够用；
为生成 xlsx 反复失败拖垮整轮汇报（实测连续 6 次报错、耗时 13 分钟）得不偿失。

## 已知精度边界（必须如实告知）

损耗模型已于 2026-10-08 完成 v3 标定（控制变量扫参 RMxprt 真值，
`scripts/calibrate_losses_v3.mjs`）：
- 铜耗分档系数（≤30/30~100/>100kW）：档内 R² 0.92~0.97
- 铁耗 f·B 双基（变频扫参标定）：R²=0.9896
- 机械耗+杂散按功率分档比例（3.0%/2.2%/2.0%×P_out）：15kW 基线效率偏差 +0.58pt

**仍存在的欠账**：
- 温升模型未用真实热真值标定（RMxprt 无热输出），温升普遍偏保守
- 中大机座（75/450kW）效率偏差仍有 +2.6~3.3pt（铜耗/铁耗档内形状误差）
- 散热筋效应未计

因此 L0 结果的正确使用方式是**排序与相对比较**，
绝对值仅供初筛参考。排序稳定性优于绝对精度。

## 禁止事项

- ❌ 把 L0 效率数字当作交付值报给客户
- ❌ 声称 L0 调用了 RMxprt / Motor-CAD（它没有）
- ❌ 手工改写字段名绕过 Param 锁
- ❌ 越过 `level` 总闸开启 l1/l2（会触发 `assertLevelImplemented` 快速失败）
- ❌ 产出 1 极方案（永磁同步机物理上不存在）
