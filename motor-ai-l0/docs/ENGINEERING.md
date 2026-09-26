# 工程内参（ENGINEERING）

> 本文档面向维护者，收录不适合放在对外 README 的实现细节、字段契约与工程问题复盘。

---

## 一、目录结构

```
dsh-motor-ai-l0/
├── package.json                     # DSH Bundle 声明（无 mcpServers、无 Python 依赖）
├── cordis.patch.yml                 # 配置层补丁（level 总闸 + L0 运行参数 + 校验参数）
├── index.mjs                        # Config Schema + apply() 生命周期
├── manifest.json                    # 插件清单（plugin_id / tier / skills / tools）
├── lib/
│   ├── level-gate.mjs               # 层级门控 + assertLevelImplemented 快速失败
│   ├── param-schema.mjs             # Param 锁：字段单一真源 + 交接白名单
│   ├── motor-constants.mjs          # 常量（构建期由 knowledge-sync 生成，勿手改）
│   ├── formula-engine.mjs           # 经验公式引擎（零依赖、确定性）
│   ├── surrogate-engine.mjs         # 代理模型引擎（formula / surrogate / auto 三档 + 置信度门控）
│   ├── design-rules.mjs             # 12 条物理一致性规则（纯函数）
│   ├── usage-log.mjs                # 本地用量日志（零依赖 JSONL，静默降级，不注入 DSH 服务）
│   └── regression-gate.mjs          # 物理边界门 + 回归门
├── tools/l0/
│   ├── index.mjs                    # L0 工具统一注册
│   ├── param-matrix.mjs             # motor_param_matrix
│   ├── l0-estimate.mjs              # motor_l0_estimate
│   └── design-validate.mjs          # motor_design_validate
├── models/
│   ├── l0_surrogate.json            # 代理模型默认权重
│   ├── l0_surrogate_family.json     # 分族权重
│   └── l0_surrogate_single.json     # 单族权重
├── benchmarks/
│   ├── l0-benchmarks.json           # 6 个标定案例（几何配平 + 能效带锚点）
│   └── baseline.json                # 回归基线快照（--update-baseline 生成）
├── knowledge-sync/
│   ├── constants.json               # 常量真源（P1）
│   └── sync-constants.py            # 真源 → lib/motor-constants.mjs 生成器
├── skills/motor-l0-estimate/        # 随包分发的 DSH Skill
│   ├── SKILL.md
│   └── references/*.md              # formula-reference / param-matrix / validation-rules / l1-l2-handoff
├── tests/
│   └── surrogate-engine.test.mjs    # 代理模型单测
├── assets/                          # 图标（icon-512）与上架素材（listing / checklist）
├── scripts/
│   ├── verify.mjs                   # 离线自检 49 项（无需 DSH Runtime）
│   ├── regression.mjs               # 质量门运行器
│   ├── release.mjs                  # 版本三重一致性门禁
│   └── publish.mjs                  # 发布辅助
├── docs/                            # 本文档 + QUALITY-GATE.md
├── dist/                            # ⚠ 本地产物（旧 tar.gz + 调试脚本），不应入包
└── metadata/publish-log.json        # ⚠ 本地产物（发布记录）
```

**分层原则**：`lib/` 零依赖纯计算，`tools/` 只做薄封装。
所有 `tools/*.mjs` 都不在顶层 import `@deepseek-ai/dsh-tools`，因此可在裸 Node 下加载单测。

> ⚠ 仓库卫生：`dist/`（含 `_debug2/3/4.py` 与旧版本 `motor-ai-l0-v0.1.0.tar.gz`，约 155 KB）
> 与 `metadata/publish-log.json` 目前**已被 git 追踪**。建议 `git rm --cached` 后加入 `.gitignore`。

---

## 二、字段契约（Param 锁）

字段真源集中在 `lib/param-schema.mjs`，**禁止任何模块手写字段字面量**。

| 集合 | 项数 | 内容 / 用途 |
|---|---|---|
| `L1_STRICT_REQUIRED` | 4 | `stator_od / poles / voltage / speed` —— L1 侧为 `params["x"]` 直接索引，缺一即 KeyError |
| `L1_MATRIX_FIELDS` | 16 | 交接白名单，字段名须与 Python 侧完全一致 |
| `PHYSICS_FIELDS` | 8 | `_physics` 子结构 |
| `L0_NATIVE_FIELDS` | 6 | `efficiency / torque / temp_rise / total_loss / torque_density / prediction_source` |
| `L0_META_FIELDS` | 1 | `confidence` |
| `L1_MIRROR_FIELDS` | 7 | `max_temp / power / copper_loss / iron_loss / mechanical_loss / solve_mode / l1_handoff` |
| `L1_DIAG_FIELDS` | 2 | `l1_efficiency_proxy / l1_temp_proxy` —— **诊断列，不可被下游消费** |
| `HANDOFF_WHITELIST` | 16 | = `L1_MATRIX_FIELDS`，允许进入交接载荷的字段 |

> 修正记录：README 旧版写「`L1_MIRROR_FIELDS` 8 项」为笔误 —— 实为 **7 项**；
> `l1_efficiency_proxy` 属诊断列 `L1_DIAG_FIELDS`，不是镜像字段。以本表为准。

**L1_MATRIX_FIELDS 全量（16）**：`stator_od` `stator_id` `rotor_od` `core_length` `air_gap`
`tooth_width` `yoke_thickness` `shaft_dia` `poles` `voltage` `peak_current` `speed`
`slots_stator` `slots_rotor` `turns_per_coil` `parallel_circuits`

**PHYSICS_FIELDS 全量（8）**：`d_squared_l` `lambda` `lambda_valid` `lambda_advice`
`estimated_torque` `ref_model` `scenario` `target_torque`

**双字段策略**：每行结果 = L0 原生 6 字段 + 元信息 `confidence` + L1 镜像 7 字段。

```json
{
  "efficiency": 95.31, "torque": 47.75, "temp_rise": 63.4,
  "total_loss": 733, "torque_density": 147000, "prediction_source": "formula",
  "max_temp": 60.0, "power": 15.0,
  "copper_loss": 673.2, "iron_loss": 367.2, "mechanical_loss": 183.6,
  "solve_mode": "l0", "l1_efficiency_proxy": 93.2,
  "confidence": 1.0
}
```

`l1_efficiency_proxy` 是 L1 仿真公式的**去噪克隆**，专供回归质量门比对 ——
它的意义是让「物理通道」与「仿真通道」的偏差可被量化，而不是让 L0 退化成 L1 的复制品。

> 注意口径差异：`temp_rise` 单位为 **K**（热负荷法），`max_temp` 单位为 **°C**（环境温度 + 温升后钳位）。

---

## 三、12 条物理一致性校验规则（V01~V12）

实现在 `lib/design-rules.mjs` 的 `RULE_CATALOG`。

| 规则 | 内容 | 默认严重度 |
|---|---|---|
| V01 | 几何链自洽（OD>ID>转子径>轴径） | failed |
| V02 | 气隙边界 + 转子外径一致性 | failed |
| V03 | 定子内外径比 vs `idRatio(poles)` | warning |
| V04 | 长径比 λ | warning |
| V05 | 极槽配合 + 每极每相槽数 q + 定转子槽数差 | warning |
| V06 | 并联支路数整除极数、a ≤ q（Qs/(3p)，整数槽）、匝数下限 | failed |
| V07 | 齿部磁密 vs 目标 1.02T | warning |
| V08 | 轭部磁密 vs 目标 0.82T | warning |
| V09 | 槽形几何（槽宽>0、宽深比） | failed |
| V10 | 电频率（>400Hz 提示、>1200Hz 越界） | warning |
| V11 | 转子轭最小厚度 | failed |
| V12 | 温升 vs 绝缘等级限值 | warning（模型未标定） |

`escalate` 可把任一条软规则临时提升为 `failed`（如 `['V12']`）。
缺必要输入时规则记入 `skipped`，**绝不静默通过**。

---

## 四、与现有 Python 体系的关系

L0 的 JS 实现刻意复刻 `Scripts/physics_kernel.py` 与 `Scripts/motor_tools.py` 的计算方式，
但有 **3 处必须存在的差异**：

### 差异 1：去随机化

Python 侧在 `slots / turns / air_gap / peak_current` 上用了 `random`
（`physics_kernel.py:587/595/609/629/634`）。L0 改为**索引轮询**确定选取。

- 理由：L0 要能被回归复现，随机性是测试与质量门的死敌
- 副作用有利：组合更全，恰好适合广筛

### 差异 2：无型谱依赖

Python 走 `Knowledge/y_series_physics.json` 查表；L0 要求零外部依赖，统一走类比模式
（`empiricalBaseSize`，`physics_kernel.py:536-538`）。
调用方可通过 `base_diameter / base_length` 传入型谱中心覆盖默认中心。

### 差异 3：规模与口径

- Python `focused_scan` 目标 5-20 组；L0 是广筛层，可达 `maxMatrixSize`（默认 2000）
- 效率封顶统一到 **96**，对齐 `motor_tools.py:752`。若 L0 用 98.5% 而 L1 用 96%，
  同一批方案在两层之间的排序会跳变

### 已修正的资料稿偏差（6 项）

| # | 资料稿写法 | 实际情况 | 本仓库处理 |
|---|---|---|---|
| 1 | 输出对齐 `quick_l0_estimate` | 全代码库不存在该函数 | 改对齐 `_run_simulated`（`motor_tools.py:726`） |
| 2 | 字段 `slots / cooling / speed_rpm` | L1 真源是 `slots_stator / speed`，且无 cooling 概念 | Param 锁统一命名，交接时自动过滤 L0 私有字段 |
| 3 | 效率封顶 98.5% | Python 侧封顶 96 | 统一 96，`l1_efficiency_proxy` 独立输出 |
| 4 | 冷却系数 `{8,15,40,60,80}` | 标定偏小约一个量级，温升虚高一倍以上 | 重标定为 `{25,140,350,500,650}`，依据见 motor-constants.mjs 注释 |
| 5 | 型谱覆盖 2/4/6/8 极 | 不覆盖 200kW/22000rpm 高速工况 | P3 扩表：补 10/12 极 λ 范围与槽配合；高速工况归入 **2 极**（初版误设「1 极档」已作废 —— 永磁同步机不存在 1 极，f = 22000×2/120 = 366.7Hz 与现场 f≈367Hz 吻合） |
| 6 | 镜像字段照抄仿真经验式 | `_run_simulated` 的 SIM_EFF/SIM_TEMP 只在 ~180mm/8极/3000rpm 附近有效 | 镜像字段改取 L0 物理通道；仿真口径降级为**诊断列** `l1_efficiency_proxy` / `l1_temp_proxy` |

---

## 五、W4 实施中修掉的 4 个真问题

1. **磁密公式漏 2/π** —— 每极磁通应为 `Φ = (2/π)·B_peak·极面积`，
   初版直接用 `B_peak × 极面积`，磁密被高估 π/2≈1.57 倍，表现为「齿磁密 1.63T vs 现场目标 1.02T」的假性冲突。
   修正后 `Bt = 2·B_gap·Dsi/(Qs·bt·k)`，半齿距齿宽下 `Bt = 4·B_gap/(π·k) ≈ 1.30·B_gap`，
   取 B_gap=0.8T 得 **1.04T，与现场 1.02T 吻合到 2%**。
2. **V12 温升默认判 failed 会批死整批** —— 实测矩阵 40/40 全 failed，预筛层失去意义。
   未标定模型不该承担硬剔除职责，改为默认 warning，`escalate: ['V12']` 可显式升级。
3. **槽宽深比下界 0.30 过紧** —— 26/40 行误报。定子梨形槽宽深比常见 0.2~0.8，下界改为 0.20。
4. **高速气隙档缺失** —— `AIR_GAP_MAX=1.5mm` 会把现场 2.0mm 气隙判非法。
   新增 `highSpeedRpm=8000` / `AIR_GAP_MAX_HIGH_SPEED=4.0`，并让经验气隙比对在高速档让位。

---

## 六、W4 暴露出的 3 项 Python 真源问题（需回主线确认）

| # | 现象 | 定位 | 建议 |
|---|---|---|---|
| 1 | 全部案例轭磁密 1.83~2.72T，超目标 0.82T 达 123~232% | `physics_kernel.py:601-602` `yoke = 0.4·half` 对 4 极机偏薄约 2× | 校核 yoke 系数，或按极数分档 |
| 2 | 参数矩阵外径系统性偏大（15kW 推到 ID≈197mm，实机约 170mm） | `physics_kernel.py:536-538` `empiricalBaseSize` | 用型谱数据回归 d/l 系数 |
| 3 | `baseTurns = 0.3·V + 0.02·(450−OD)`，380V 推得 114 匝/线圈，实机约 15~30 | `physics_kernel.py:608` | 该式量级需复核（L0 未依赖 turns，暂不影响估算） |

---

## 七、路线图与版本状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| W1 | 插件骨架 + 层级门控 + 离线自检 | ✅ 完成 |
| W2 | P1 常量真源同步 + 公式引擎 + P3 高速扩表 + Param 锁 | ✅ 完成 |
| W3 | `motor_param_matrix` + `motor_l0_estimate` | ✅ 完成 |
| W4 | `motor_design_validate` + 公式 vs 模拟回归质量门 | ✅ 完成（质量门判定 DEBT） |
| W5 | 代理模型 + `SKILL.md` + `release.mjs` 门禁 | ✅ 完成（`surrogate-engine.mjs` + `models/` + `skills/` + `scripts/release.mjs` 均已交付） |
| W6 | npm 发布 / DSHHub 收录 / 口令分发 / L1 转化钩子 | 🔶 部分完成（npm 已发布；DSHHub 收录、口令分发、L1 转化钩子待办） |

### 语义化版本口径

- PATCH：公式系数微调、bug 修复、**文档重构**
- MINOR：新增公式 / 新增代理模型 / 新增校验规则
- MAJOR：输出格式变更、工具参数结构变更、DSH 兼容性提升

三重一致性（`package.json.version` = `SKILL.md` 的 `metadata.version` = `CHANGELOG.md` 最新条目）
由 `scripts/release.mjs` 与 `scripts/verify.mjs` 共同门禁。

---

## 八、配置全表

配置真源：`cordis.patch.yml`（默认值）+ `index.mjs` 的 `Config` Schema（校验与描述）。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `level` | `'l0'` | 层级总闸（`l0`/`l1`/`l2`）。非 `l0` 触发 `assertLevelImplemented` 快速失败 |
| `l0Mode` | `'auto'` | `formula` 纯公式 / `surrogate` 代理模型 / `auto` 自动降级 |
| `surrogatePath` | `models/l0_surrogate.json` | 代理模型权重路径 |
| `surrogateConfidenceThreshold` | `0.7` | 置信度低于此值自动降级到公式模式 |
| `maxMatrixSize` | `2000` | 参数矩阵最大组合数 |
| `topNPreview` | `20` | L0 结果预览返回的 TopN 数量 |
| `efficiencyCap` | `96` | 效率封顶（对齐 `motor_tools.py:752`，避免 L0/L1 排序跳变） |
| `tempRiseRange` | `[45, 130]` | `max_temp` 钳位区间（**°C**）。⚠ 命名待议 —— 易被误读为 L0 温升（K），拟改 `maxTempClamp` |
| `airGapFluxT` | `0.8` | 气隙磁密基准 T（齿 / 轭磁密反算输入），PMSM 典型 0.75~0.90 |
| `insulationClass` | `'F'` | 绝缘等级，决定温升限值（B=80K / F=105K / H=125K） |
| `highSpeedRpm` | `8000` | 超过此转速气隙上限放宽到 4mm，并让经验气隙比对让位 |
| `usageLog` | `true` | 本地用量日志开关：工具调用元数据追加到 `~/.dsh/storages/dsh-motor-ai-l0/usage.jsonl`（`ts`/`tool`/`ok`/`elapsed_ms`/`n`/`failed`，不记设计参数与结果内容）。刻意不走 `inject: ['telemetry']` —— 核心遥测服务实名 `sessionTelemetry`，不存在 `telemetry` 服务，注入会让插件永久 pending |
| `l1Enabled` / `l2Enabled` | `false` | L1/L2 预留分闸：打开仅告警，不生效（实现后需同步扩展 `IMPLEMENTED_LEVELS`） |

> 另：`constantsSource` 指向 `lib/motor-constants.mjs`，该文件由 `knowledge-sync/constants.json`
> 构建期生成，**勿手改**（改真源后重跑 `knowledge-sync/sync-constants.py`）。
