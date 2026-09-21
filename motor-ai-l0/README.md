# dsh-motor-ai-l0

Motor-AI DSH「电机设计专家」插件 —— **L0 快速预筛层**。

> 一句话定位：用毫秒级经验公式把上千个参数组合压缩到 TopN，再交给 L1 RMxprt 精算。
> 不装 RMxprt、不装 Motor-CAD、不装 Python —— 装完插件即可用。

---

## 一、能力边界

| 层级 | 引擎 | 速度 | 范围 | 付费 | 本仓库状态 |
|---|---|---|---|---|---|
| **L0** | 经验公式 + 代理模型 | 毫秒级 | 全部方案 | free | ✅ W1-W4 已交付核心 |
| L1 | RMxprt 磁路法 | 1-3s/方案 | TopN | vip | ⛔ 预留（接口已留） |
| L2 | Motor-CAD 有限元 | ~200s/方案 | Top3 | flagship | ⛔ 预留（接口已留） |

**本阶段做**：参数矩阵生成、物理一致性校验、L0 性能估算、排序、TopN 交接载荷、回归质量门。
**本阶段不做**：RMxprt/Motor-CAD 调用、MCP Server、Python 桥接、多目标优化、代理模型（W5）。

---

## 二、目录结构

```
motor-ai-l0/
├── package.json                     # Bundle 声明（无 mcpServers、无 Python 依赖）
├── cordis.patch.yml                 # 配置层补丁（level 总闸 + L0 运行参数）
├── index.mjs                        # Config Schema + apply() 生命周期
├── lib/
│   ├── level-gate.mjs               # 层级门控 + assertLevelImplemented 快速失败
│   ├── param-schema.mjs             # Param 锁：字段单一真源 + 交接白名单
│   ├── motor-constants.mjs          # 常量（P1 真源：由 knowledge-sync 构建期生成）
│   ├── formula-engine.mjs           # 经验公式引擎（零依赖、确定性）
│   ├── design-rules.mjs             # W4：12 条物理一致性规则（纯函数）
│   └── regression-gate.mjs          # W4：物理边界门 + 回归门
├── tools/l0/
│   ├── index.mjs                    # L0 工具统一注册
│   ├── param-matrix.mjs             # motor_param_matrix
│   ├── l0-estimate.mjs              # motor_l0_estimate
│   └── design-validate.mjs          # W4：motor_design_validate
├── benchmarks/
│   ├── l0-benchmarks.json           # 6 个标定案例（几何配平 + 能效带锚点）
│   └── baseline.json                # 回归基线快照（--update-baseline 生成）
└── scripts/
    ├── verify.mjs                   # 离线自检 49 项（无需 DSH Runtime）
    └── regression.mjs               # 质量门运行器
```

分层原则：**`lib/` 零依赖纯计算，`tools/` 只做薄封装**。
所有 `tools/*.mjs` 都不在顶层 import `@deepseek-ai/dsh-tools`，因此可在裸 Node 下加载单测。

---

## 三、工具

### 3.1 motor_param_matrix

聚焦扫描生成参数矩阵。入参支持别名：`power`→`power_kw`、`rpm`→`speed_rpm`、`volt`→`voltage_v`。

```js
buildParamMatrix({ power_kw: 15, speed_rpm: 3000, poles: 8, count: 24 })
// → { matrix: [...], total, returned, truncated, spec }
```

每行严格产出 **16 个 L1 顶层字段 + `_physics` 8 字段 + L0 私有 3 字段**，
可被 `assertMatrixShape()` 断言通过。

### 3.2 motor_l0_estimate

对矩阵做毫秒级估算并排序输出 TopN。

```js
runL0Estimate({ params_list, top_n: 10, sort_by: 'efficiency' })
// → { results, handoff, total, success, failed, elapsed_ms, avg_ms_per_case }
```

排序白名单：`efficiency` / `torque_density` / `temp_rise` / `total_loss` / `power`，
其中 `temp_rise`、`total_loss` 为升序（越小越好）。

### 3.3 motor_design_validate（W4）

物理一致性校验，返回 `passed / warning / failed` 三级结论。**建议在估算前先跑批量模式，
把 `failed` 剔除后再喂 `motor_l0_estimate`** —— 广筛层最怕的是「看起来效率高」的假方案污染 TopN。

```js
runDesignValidate({ params_list, escalate: ['V12'] })
// → { summary, rule_hits, passed_indices, warning_indices, failed_indices, failed_reasons }
```

12 条规则（`lib/design-rules.mjs` 的 `RULE_CATALOG`）：

| 规则 | 内容 | 默认严重度 |
|---|---|---|
| V01 | 几何链自洽（OD>ID>转子径>轴径） | failed |
| V02 | 气隙边界 + 转子外径一致性 | failed |
| V03 | 定子内外径比 vs `idRatio(poles)` | warning |
| V04 | 长径比 λ | warning |
| V05 | 极槽配合 + 每极每相槽数 q + 定转子槽数差 | warning |
| V06 | 并联支路数整除极数、匝数下限 | failed |
| V07 | 齿部磁密 vs 目标 1.02T | warning |
| V08 | 轭部磁密 vs 目标 0.82T | warning |
| V09 | 槽形几何（槽宽>0、宽深比） | failed |
| V10 | 电频率（>400Hz 提示、>1200Hz 越界） | warning |
| V11 | 转子轭最小厚度 | failed |
| V12 | 温升 vs 绝缘等级限值 | **warning（模型未标定）** |

`escalate` 可把任一条软规则临时提升为 `failed`（如 `['V12']`）。
缺必要输入时规则记入 `skipped`，**绝不静默通过**。

---

## 四、字段契约（Param 锁）

字段真源集中在 `lib/param-schema.mjs`，**禁止任何模块手写字段字面量**。

| 集合 | 内容 | 用途 |
|---|---|---|
| `L1_STRICT_REQUIRED` | `stator_od / poles / voltage / speed` | L1 侧为 `params["x"]` 直接索引，缺一即 KeyError |
| `L1_MATRIX_FIELDS` | 16 项 | 交接白名单，字段名须与 Python 侧完全一致 |
| `PHYSICS_FIELDS` | 8 项 | `_physics` 子结构 |
| `L0_NATIVE_FIELDS` | 6 项 | L0 原生产出 |
| `L1_MIRROR_FIELDS` | 8 项 | 让 L0 结果零翻译进入 L1 下游 |

**双字段策略**：每行结果 = L0 原生 6 字段 + L1 镜像字段。

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

`l1_efficiency_proxy` 是 L1 仿真公式的**去噪克隆**，专供 W4 回归质量门比对 ——
它存在的意义是让「物理通道」与「仿真通道」的偏差可被量化，而不是让 L0 退化成 L1 的复制品。

---

## 五、与现有 Python 体系的关系（必读）

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

### 已修正的资料稿偏差

| # | 资料稿写法 | 实际情况 | 本仓库处理 |
|---|---|---|---|
| 1 | 输出对齐 `quick_l0_estimate` | 全代码库不存在该函数 | 改对齐 `_run_simulated`（`motor_tools.py:726`） |
| 2 | 字段 `slots / cooling / speed_rpm` | L1 真源是 `slots_stator / speed`，且无 cooling 概念 | Param 锁统一命名，交接时自动过滤 L0 私有字段 |
| 3 | 效率封顶 98.5% | Python 侧封顶 96 | 统一 96，`l1_efficiency_proxy` 独立输出 |
| 4 | 冷却系数 `{8,15,40,60,80}` | 标定偏小约一个量级，温升虚高一倍以上 | 重标定为 `{25,140,350,500,650}`，依据见 motor-constants.mjs 注释 |
| 5 | 型谱覆盖 2/4/6/8 极 | 不覆盖 200kW/22000rpm 高速工况 | P3 扩表：补 10/12 极 λ 范围与槽配合；高速工况归入 **2 极**（初版误设「1 极档」已作废 —— 永磁同步机不存在 1 极，f = 22000×2/120 = 366.7Hz 与现场 f≈367Hz 吻合） |
| 6 | 镜像字段照抄仿真经验式 | `_run_simulated` 的 SIM_EFF/SIM_TEMP 只在 ~180mm/8极/3000rpm 附近有效 | 镜像字段改取 L0 物理通道；仿真口径降级为**诊断列** `l1_efficiency_proxy` / `l1_temp_proxy` |

> ⚠ **配置项命名待议**：`tempRiseRange` 实际对齐的是 L1 `max_temp` 的钳位区间（°C），
> 不是 L0 温升（K）。语义易被误读，建议后续改名为 `maxTempClamp`。

---

## 六、安装与校验

```bash
# 离线自检（不需要 DSH Runtime，裸 Node 即可）
node scripts/verify.mjs

# 本地安装测试
dsh plugin --profile web add dsh-motor-ai-l0

# 查看配置层
dsh --profile web --dump-config
```

**层级门控**：把 `level` 设为 `l1` 或 `l2` 会立即抛错并阻止插件加载 —— 这是有意为之的
快速失败，避免「配置打开了、代码没实现」的静默降级。

---

## 七、版本与发布

语义化版本三重一致性由 `scripts/release.mjs`（W5 交付）强制：

- PATCH：公式系数微调、bug 修复
- MINOR：新增公式 / 新增代理模型 / 新增校验规则
- MAJOR：输出格式变更、工具参数结构变更、DSH 兼容性提升

---

## 八、路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| W1 | 插件骨架 + 层级门控 + 离线自检 | ✅ 完成（12/12） |
| W2 | P1 常量真源同步 + 公式引擎 + P3 高速扩表 + Param 锁 | ✅ 完成 |
| W3 | motor_param_matrix + motor_l0_estimate | ✅ 完成（37/37） |
| W4 | motor_design_validate + 公式 vs 模拟回归质量门 | ✅ 完成（49/49，质量门判定 DEBT） |
| W5 | 代理模型 + SKILL.md + release.mjs 门禁 | ⬜ 待开始 |
| W6 | npm / DSHHub 发布 + 口令分发 + L1 转化钩子 | ⬜ 待开始 |

---

## 九、已知偏差与 W4 待校准清单（实算数据）

L0 物理通道目前是 **v0.1 未标定版**。刻意不为了「数字好看」而乱调系数 ——
下列偏差全部登记待 W4 用 RMxprt 批量结果回归后再收紧。

### 实算对照（`node scripts/verify.mjs` 之外的独立探针）

| 案例 | 工况 | L0 物理通道 | 参照 | 偏差 |
|---|---|---|---|---|
| A | Y160M-4：15kW / 1460rpm / 4极 / OD260 / L155 / 风冷 | η 86.63%，温升 88.6K | IE1 88.5% / IE3 91.5%；B级允许温升 80K | η 偏低 1.9~4.9pt，温升偏高约 9K |
| B | 现场工况：200kW / 22000rpm / 2极 / 液冷 / SmCo | η 96%（受 cap 封顶，推算值约 97.5%），温升 107.2K | 同类高速 PMSM 典型 96.5~97.5% | 数值合理，但 cap=96 造成低估 |

### 偏差根因（已定位）

1. **铁损**：Steinmetz 用单一等效磁密（默认 1.2T），未区分齿部/轭部；`kh/ke` 系数未标定
2. **铜损**：槽面积由几何推算，缺少「匝数 × 电流」的电路约束；槽满率 0.45 与电密 5.5 A/mm² 均为经验值
3. **机械损**：`MECH_LOSS_K = 5` 未标定，仅保证量级正确
4. **温升**：散热面积按**光滑**机座外表面积计，未计散热筋；`COOLING_COEFFICIENT` 为单点标定（标定算例见 motor-constants.mjs）
5. **效率封顶**：`efficiencyCap=96` 是为与 `_run_simulated` 同口径的权宜值，对高效高速机会低估

### W4 必须完成的三件事

- [ ] 用 RMxprt 批量结果做 native vs L1 回归，拟合 `kh/ke`、`MECH_LOSS_K`、`COOLING_COEFFICIENT`
- [ ] 回归目标：标定域内 `|Δη| ≤ 2pt`、`|Δ温升| ≤ 10K`
- [ ] 校准完成后重新评估 `efficiencyCap` 是否需要放宽（当前 96 对高速高效机偏保守）

> 诊断列 `l1_efficiency_proxy` / `l1_temp_proxy` 就是为这件事准备的：
> 案例 B 中它给出 80%，与 L0 的 96% 相差 16pt —— 这不是 bug，
> 而是把「Python 仿真经验式在高速区失真」这件事显性暴露了出来。

---

## 十、回归质量门（W4 交付）

```bash
node scripts/regression.mjs                  # 跑门（比对基线）
node scripts/regression.mjs --update-baseline   # 重新生成基线快照
node scripts/regression.mjs --strict         # DEBT 也按失败计
node scripts/regression.mjs --json           # 机器可读输出
```

### 两道门 + 一个诊断

| 门 | 判据 | 阻断 |
|---|---|---|
| **A 物理边界门** | 效率落进该功率档 IE1~IE4 能效带、温升不越绝缘设计限值 | 由 `benchmarks/gate.sanity_blocking` 控制，**当前 false** |
| **B 回归门** | 与 `baseline.json` 逐指标比较，允许 0.5pt / 2K / 5% 劣化 | **是**（首次运行无基线时放行） |
| **诊断 divergence** | L0 物理通道 vs L1 仿真 proxy 的 Δη / ΔT | 否（仅记录，见下） |

为什么 divergence 不阻断：`_run_simulated`（`motor_tools.py:750-756`）是在
~180mm / 8 极 / 3000rpm 附近标定的线性式，套到 200kW/22000rpm 会推出 80% 效率
——比 IE1 地板还低，是**仿真模型自身失真**。强行把 L0 对齐到它等于把 L0 一起带偏。
故只有落在 `SIM_VALID_DOMAIN`（120~260mm / 4~8 极 / 500~4000rpm）内的案例才计入 2pt 预算，
域外标记 `out_of_domain`、可见但不计分。

### 首轮实算结论（2026-09-21）

**判定：DEBT**（sanity 6/6 越界，但按配置不阻断；无回归）。

| 案例 | L0 η | 参考带 | Δη | L0 ΔT | 考核/设计限值 | 仿真 proxy | 域内? |
|---|---|---|---|---|---|---|---|
| y160m4 15kW/4极 | 86.65% | 87~94% | −0.35pt | 88.4K | 80/105K ⚠ | 83.4% | 是（超预算 3.25pt） |
| y200l4 30kW/4极 | 86.65% | 89~95% | −2.35pt | 113.8K | 80/105K ✗ | 86.08% | 否 |
| hs200kw 200kW/22000rpm | 96%（cap） | 95~98% | 0 | 113.3K | 105/105K ✗ | 80% | 否 |
| mine75kw 75kW/660V | 89.59% | 91~96% | −1.41pt | 131K | 80/105K ✗ | 89% | 否 |
| small55kw 5.5kW/2极 | 81.04% | 84~91% | −2.96pt | 108.8K | 80/105K ✗ | 80% | 否 |
| fan110kw 110kW/2极 | 90.13% | 92~97% | −1.87pt | 87.1K | 105/105K ✓ | 85.32% | 否 |

欠账集中在两点：**效率系统性偏低 1.4~3pt**（损耗三项未标定）、**温升系统性偏高**（散热筋未计 + 损耗高估）。
根因与第九节登记的 5 项一致，属待标定项，不是实现缺陷。

> 翻转判据：6 个案例全部落进参考带且温升不越考核限值后，
> 必须把 `benchmarks/l0-benchmarks.json` 的 `gate.sanity_blocking` 改为 `true`，
> 否则门形同虚设。

### W4 实施中修掉的 4 个真问题

1. **磁密公式漏 2/π（我的 bug）** —— 每极磁通应为 `Φ = (2/π)·B_peak·极面积`，
   初版直接用 `B_peak × 极面积`，磁密被高估 π/2≈1.57 倍，表现为「齿磁密 1.63T vs 现场目标 1.02T」的假性冲突。
   修正后 `Bt = 2·B_gap·Dsi/(Qs·bt·k)`，半齿距齿宽下 `Bt = 4·B_gap/(π·k) ≈ 1.30·B_gap`，
   取 B_gap=0.8T 得 **1.04T，与现场 1.02T 吻合到 2%**。
2. **V12 温升默认判 failed 会批死整批** —— 实测矩阵 40/40 全 failed，预筛层失去意义。
   未标定模型不该承担硬剔除职责，改为默认 warning，`escalate: ['V12']` 可显式升级。
3. **槽宽深比下界 0.30 过紧** —— 26/40 行误报。定子梨形槽宽深比常见 0.2~0.8，下界改为 0.20。
4. **高速气隙档缺失** —— `AIR_GAP_MAX=1.5mm` 会把现场 2.0mm 气隙判非法。
   新增 `highSpeedRpm=8000` / `AIR_GAP_MAX_HIGH_SPEED=4.0`，并让经验气隙比对在高速档让位。

### W4 暴露出的 3 项 Python 真源问题（需回主线确认）

| # | 现象 | 定位 | 建议 |
|---|---|---|---|
| 1 | 全部案例轭磁密 1.83~2.72T，超目标 0.82T 达 123~232% | `physics_kernel.py:601-602` `yoke = 0.4·half` 对 4 极机偏薄约 2× | 校核 yoke 系数，或按极数分档 |
| 2 | 参数矩阵外径系统性偏大（15kW 推到 ID≈197mm，实机约 170mm） | `physics_kernel.py:536-538` `empiricalBaseSize` | 用型谱数据回归 d/l 系数 |
| 3 | `baseTurns = 0.3·V + 0.02·(450−OD)`，380V 推得 114 匝/线圈，实机约 15~30 | `physics_kernel.py:608` | 该式量级需复核（L0 未依赖 turns，暂不影响估算） |
