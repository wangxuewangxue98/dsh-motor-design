# 更新日志

本项目的所有重大变更都记录在此文件中。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

版本三重一致性：`package.json.version` = `SKILL.md` 的 `metadata.version` = `CHANGELOG.md` 最新条目。

## [0.2.2] - 2026-09-26 — 代理通道修复（模型 v2.2.0）

### 🔴 P0 修复：代理模型 coef 空间错位（0.2.1 升级不可用根因）
- **现象**：用户开启 `l0Mode='surrogate'` + 新模型 `l0_surrogate_family.json` 时，异步机预测效率**恒为 50.00%**（物理钳位下界），`prediction_source=surrogate` 且 `surrogate_fallbacks=0` 误报「无降级」。
- **根因**：训练端 `fit_segment` 在**原始特征空间**解 Ridge（coef 截距≈19、权重极小），而推理端 `extractFeatures` 先把特征按 `feature_mean/std` 标准化再乘 coef → 空间错位，`rawEff≈19` 被钳到 50。CV 评估在标准化空间做的（R² 高），但导出 coef 来自原始空间训练，两端不一致。
- **修复**：`fit_segment` 改为在「按全量原始均值/标准差标准化」的特征空间内训练（feature_mean/std 维持原始统计不动），训练/推理空间严格对齐。重训导出模型 **v2.2.0**。
- **验证**（端到端）：异步 75kW/1480rpm → `eff≈89~91%` 且 `source=surrogate`；PMSM 200kW/22000rpm 因段 cv_r2≤0.31、conf<0.4 仍正确降级公式。

### 默认配置同步（让代理通道真正可用）
- `index.mjs` Config schema 默认值：`surrogatePath` `'models/l0_surrogate.json'`→`'models/l0_surrogate_family.json'`；`surrogateConfidenceThreshold` `0.7`→`0.4`。
- `tools/l0/l0-estimate.mjs`：代理分支条件由 `l0Mode==='surrogate'` 扩展为 `'surrogate' || 'auto'`（插件默认 `l0Mode='auto'`，否则代理通道永不启用）。
- `cordis.patch.yml`：同步 `surrogatePath` / `surrogateConfidenceThreshold`，`l0Mode` 保持 `auto`。

### 测试
- `scripts/verify.mjs` 新增「surrogate 通道端到端可用且不被钳底」用例（显式走代理 + 断言 `efficiency>50 && ≤99`）。全量 **61/61** 通过（原 60/60 + 1）。

## [0.2.1] - 2026-09-26 — 干净集重训（v2.1.0）

### 数据清洗
- **排除 fault=1 失败样本**：`l0_residuals` 中 992 条异步 + 30 条永磁标记为 RMxprt 求解失败（l1_eff=0），全部剔除。干净集 n=3835（异步）+1100（永磁）。
- **根因分析**：fault=1 样本集中在大极数(6p)+大 OD 设计，推测 RMxprt 在该参数空间发散，应在 L0 阶段直接标记不可用而非进入代理模型。

### 性能对比（v2.0.0 → v2.1.0）

| 段 | n | CV R² | CV MAE | l0 基线 MAE | 相对改善 |
|---|---|---|---|---|---|
| 异步 small | 1279 | **+0.652** | 1.44pp | 3.68pp | **−61%** |
| 异步 medium | 2558 | +0.438 | 3.29pp | 5.31pp | −38% |
| 异步 large | 1276 | **+0.670** | 4.53pp | 8.69pp | **−48%** |
| 永磁 small | 367 | +0.134 | 2.17pp | 2.09pp | — 公式降级 |
| 永磁 medium | 745 | +0.099 | 1.88pp | 1.83pp | — 公式降级 |
| 永磁 large | 355 | +0.311 | 1.29pp | 1.68pp | −23% |

### 扩充决策（否决）
- `case_motors` 表 `wire_dia` 字段 **100% NULL**，无法补算 l0_eff（公式通道需导线直径计算铜耗）。
- 铭牌效率（平均 82.4%）与 RMxprt 效率（平均 89.5%）口径不一致，强行并入会注入错误残差关系。
- **结论**：扩充暂缓，待 wire_dia 补全或铭牌→RMxprt 映射标定后再做。

## [0.2.0] - 2026-09-26

### 修复
- **`estimateTotalLoss()` 量纲修复**（`lib/surrogate-engine.mjs`）：`efficiency` 是百分数口径（91.34=91.34%），旧实现 `powerW*(1/efficiency-1)` 按小数口径计算，15kW/η91.34% 会得到 **−14835.78W**（负损耗）；现先归一到 [0,1] 再反推，同输入得 +1422.17W，并增加 η≤0 或 η≥100% 的防护（返回 0 交公式通道兜底）
- **`V06` 补充 `a ≤ q = Qs/(3p)` 判据**（`lib/design-rules.mjs`）：旧实现只校验 `a|p`，会放行 `p=4/Qs=36/a=4`（q=3）这类支路无法分配的不可实现设计；现整数槽下 `a>q` 判 `failed`，分数槽（q 非整数）不在此判据范围。规则注册表/`ENGINEERING.md`/skill 参考文档同步更新

### 变更
- **surrogate 通道标注实验特性**：`models/l0_surrogate_family.json` 增加 `experimental: true` 与 `experimental_note`（训练域 od_range≈612~2651mm 与真实中小机座 100~500mm 不匹配、PMSM 段 cv_r2≈0、特征含 l0_eff 泄漏）；`motor_l0_estimate` 工具输出新增 `surrogate_experimental` / `surrogate_warning` 字段；README 增加 ⚠️ 标注。排序决策请使用默认公式通道
- **仓库卫生**：`git rm --cached` 移除历史误入库的 `dist/`（4 文件，含 _debug*.py 与 v0.1.0 tar.gz）与 `metadata/publish-log.json`（本地文件保留，`.gitignore` 规则自此生效）

### 新增（代理模型 v2.0.0 重训 · 基于 6390 行真实 RMxprt 残差）

- **数据源修正**：旧 v1.0.0 误用 242 行 `res_cc` 真解且为"去泄漏"删掉 `l0_eff` → 小/中机 cv_r2 为负。v2.0.0 改用 `designs.db.l0_residuals`（6390 行：5260 异步 + 1130 永磁，含 `l0_eff` 公式效率 + `l1_eff` RMxprt 效率）为主训练源（决策"先用现有数据训练，不调用 RMxprt"）。
- **特征工程（决策"加入特征"）**：特征集 = `[stator_od, stator_id, core_length, poles, l0_eff]` → `l1_eff`。实证 `l0_eff` 是公式通道可算的物理基（推理端公式通道会算，不依赖 RMxprt），非部署泄漏；加入后异步 cv_r2 由 +0.21（纯几何）跃升至 +0.55。异步分族：small R²=0.636/MAE=1.45pp、medium 0.484/3.57pp、large 0.670/4.81pp。
- **扩充消融**：`case_motors`（铭牌效率）按 0.5× 并入经消融验证会拉低异步 R²（0.547→0.537，标签口径铭牌 vs RMxprt 不一致），故 v2.0.0 暂未并入，待 nameplate→RMxprt 映射标定后再扩充（决策"扩充"暂缓）。
- **PMSM 处理**：各段 cv_r2≤0.31（效率集中高位、方差小）→ 不建族，推理自动降级公式通道。

### 修复（插件部署打通）

- **版本校验**（`lib/surrogate-engine.mjs`）：`startsWith('1.')` 拒 v2.0.0 → 改为接受 `'1.' | '2.'`。
- **detectMotorType**：`od<400→pmsm` 误判真实小异步（OD 275/337）→ 改为优先用 `params.motor_type`，回退 OD 启发式。
- **置信阈值**：`DEFAULT_CONFIDENCE_THRESHOLD` 0.7 高于本模型最高 R²(0.67) 会导致代理永不启用 → 降到 0.4。
- **l0_eff 注入**（`tools/l0/l0-estimate.mjs`）：`predictSurrogate` 读取 `params[l0_eff]` 但推理 `params` 无此字段 → 推理端用 `quickL0Estimate` 补算注入（实测 Python/Node 两套公式仅差 ~0.5pp，口径一致）；默认模型路径改为 `models/l0_surrogate_family.json`。

### 校验
- 模型 JSON 结构与 `predictLinear`/`extractFeatures` 对齐（`coef`=[截距+5权重]，`feature_mean/std` 齐备）。
- 推理端自校验（Task #39）：注入 l0_eff 后 `predictSurrogate` 不再抛"特征 l0_eff 缺失"，异步各段返回合理效率区间。
- `scripts/verify.mjs`：**60/60 全绿**（新增 V06 `a≤q` 三态断言：a=4 拦截 / a=2 放行 / a=3 整除判据回归保护）

## [0.1.7] - 2026-09-23

### 修复（L0 薄轭模型失真 · V08 轭磁密门禁硬化）

审计《重跑结果 #1/#5》发现 L0 薄轭模型 `By` 估算虽公式正确（与齿磁密 `Bt` 同源 `By = B·Dsi/(p·yoke·k)`），但 `V08` 仅作 `warning`，未剔除轭饱和候选，导致 `Bj∈[2.88,5.29]T` 全部超过硅钢饱和极限（≈2.0~2.1T）却仍判 `feasible=true`，下游误收到「超标轭磁密方案」。本版将轭饱和判据硬化为**阻断级门禁**：

- **新增轭饱和物理常量**（`lib/motor-constants.mjs`）：`B_YOKE_WARN_T=1.5`（预警）、`B_YOKE_SAT_T=1.9`（硅钢饱和极限，工程工作上限）
- **`V08` 规则体硬化**（`lib/design-rules.mjs`）：`By > 1.9T` 直接 `failed`（轭部无法承载磁通，几何不可行）；`1.5T<By≤1.9T` 仍 `warning`；`|By−0.82T|` 偏差按 `FLUX_TOLERANCE` 分层 warning
- **`quickL0Estimate()` 可行性聚合**（`lib/formula-engine.mjs`）：新增 `yokeSat = yokeB > B_YOKE_SAT_T`，`feasible = backEmfOk && slotFillOk && thermalOk && !yokeSat`；native/result 新增可追溯字段 `yoke_sat`（已纳入 `L0_NATIVE_FIELDS`）
- **物理结论（重要）**：2 极 200kW/22000rpm 种子几何下 `Dsi/p` 比导致轭磁密**物理下界 ≥2.1T**（`yoke→(OD−ID)/2` 极限仍超 1.9T），即 0.82T 轭磁密目标在该尺寸/极数下**不可达**——须放大定子外径或改用 4 极才能落地 0.82T 目标。V08 硬化后 `feasible_count=0` 是**正确物理结论**，而非求解器失真

### 校验
- `scripts/verify.mjs`：**59/59 全绿**（新增 V08 轭饱和 `failed` 断言 + 轭饱和阻断闭环断言；修正 `yoke_sat` 字段作用域 bug）
- 注：原「液冷 200kW feasible_count>0」断言已改为「轭饱和 ⇒ 全部 infeasible 且不推荐超标方案（V08 硬化）」，如实反映物理不可达

## [0.1.6] - 2026-09-23

### 修复（P0 · L0 电磁链物理自洽）

审计《DSH_L0水冷方案审计报告》发现 4 项致命缺陷，本版在 L0 层补齐**电气闭环反推**，使重跑结果物理自洽、可直接交接 L1 精算：

- **电气闭环反推（核心）**：新增 `deriveElectricalClosure()`，按真源物理式反推
  - 反电势 `E ≈ 4.44·f·N·Φ·kdp`、匝数 `N = Uph/(4.44·f·Φ·kdp)`、电流 `I = P/(√3·U·pf·η)`、星接 `Uph = U/√3`、峰值→有效 `I_rms = I_pk/√2`
  - 每极磁通 `Φ = 2·Bg·Dsi·L/p`（与 `design-rules.mjs` 同源 2/π 平均因子）
  - 200kW/22000rpm/380V/PMSM 实测：峰值电流 496.9A、反电势 219.4V≈相电压、频率 366.7Hz，与审计反算吻合
- **铜损/槽满率重写**：`estimateCopperLoss()` 改用**真实电流**（peakCurrent/turnsPerCoil/parallelCircuits）算相电阻、铜损、槽满率 `sf`，输出 `feasible`（≤`SLOT_FILL_MAX`）
- **估算输出接入闭环**：`quickL0Estimate()` 用 `effTurns/effCurrent` 驱动损耗链，新增 18 个可追溯诊断字段（`electrical_frequency_hz`/`back_emf_v`/`slot_fill_ratio`/`peak_current`/`turns_per_coil`/`feasible`/`verdict` 等）
- **新增两条 P0 门禁规则**：
  - **V14 反电势闭环**（failed）：用行内 `turns_per_coil` 反算 `E`，`|E−Uph|/Uph > 10%` 判不自洽（抓「匝数与电压/频率失配」）
  - **V15 槽满率可行性**（failed）：`sf > 0.78` 判几何不可实现（须增大机座或降电流）
- **PMSM 去异步污染**：`motor_param_matrix` 对 PMSM 强制 `slots_rotor=0`（无笼型槽）、定子槽取 `PMSM_SLOT_MAP`；`rotor_type='pm_synchronous_no_cage'`
- **换热系数标定修正**：`COOLING_COEFFICIENT.liquid_jacket` 350→700（油冷/浸油相应上提），液冷 200kW 温升 76K≤F级105K 可行
- **匝数下限放宽**：V13 改为 `<1` 才 fail —— 380V/高频闭环可能给出 N=1（hairpin 扁线绕组可行），不再误报

### 校验
- `scripts/verify.mjs`：**58/58 全绿**（新增 6 条闭环/门禁断言：电流≈497A、PMSM 无转子槽、反电势≈Uph、液冷 feasible、V15 槽满率、V14 反电势）
- `scripts/regression.mjs`：门 B 回归 **OK（无劣化）**；门 A 仍判 **DEBT（非阻断）**——small55kw 1.75pt 为 L0 未标定参考带偏差，与 0.1.5 同口径；baseline 已重快照锁定修正后物理行为
- 闭环矩阵 `validateDesignBatch`：0 failed / 20 warning（warning 多为 V08 轭磁密偏高的 L0 薄轭模型局限，留待 L1 精算）

### 说明
- 本版本聚焦 **L0 电磁链治本（P0 四条致命缺陷全修）**，未触碰脱敏回传（0.1.5）与 PMSM 口径（0.1.4）逻辑，零回归。

## [0.1.5] - 2026-09-22

### 新增
- **脱敏聚合指标回传（显式可选，默认关闭）**：新增 `lib/telemetry.mjs`（零新增依赖，
  仅用 `node:fs`/`node:os`/全局 `fetch`），在 0.1.4 的本地 `usage.jsonl` 之上补一条
  **可选的**脱敏上报通道，配套管理端「DSH 插件管理 · 插件使用情况」卡片闭环：
  - `sanitizeTelemetry()`：字段白名单 `TELEM_FIELDS`（tool/ok/elapsed_ms/n/success/
    failed/warning/sort_by/top_n/error/ts）单一真源硬编码；逐条记录裁剪到只剩白名单，
    设计参数正文、结果明细（效率/温升/损耗）、提示词、工作目录、密钥、client_id 一律丢弃。
  - `aggregateUsage()`：把脱敏记录**按工具聚合成统计量**（调用次数/成败/耗时分布/规模总和/
    成功速率/来源版本），只上聚合值、不上逐条明细；平台元数据仅保留 OS + node 大版本。
  - `reportTelemetry()`：分批（`telemetryBatchSize`）POST 到 `telemetryEndpoint`，
    按字节 offset 增量推进，**HTTP 失败/超时不推进 offset（下批重报）且静默降级**，
    绝不影响工具主流程；`attachSessionTelemetry()` 运行时探测 DSH Runtime 的
    `sessionTelemetry` 瀑布并附带（不静态 `inject`，规避 apiProxy 永久 pending 事故）。
- **Config 新增 5 个字段**（`index.mjs`）：`telemetryEnabled`（默认 `false`）、
  `telemetryEndpoint`（默认 `''`）、`telemetryBatchSize`（默认 50）、
  `telemetryIntervalSec`（默认 300，0=仅退出 flush）、`sessionTelemetry`（默认 `auto`）。
  **须 `telemetryEnabled=true` 且 `telemetryEndpoint` 非空才真正启用**（显式 opt-in）。
- **`apply()` 生命周期**：`setupTelemetry()` 周期 flush + 进程退出（SIGTERM/SIGINT/
  beforeExit）flush 一次；启动日志追加「脱敏回传 on/off → 端点」状态。

### 合规底线
- 默认全关：不配置 = 零外发，`usage.jsonl` 照本地记录，与 0.1.4 行为逐字节一致（零回归）。
- 只回传白名单统计量；`error` 字段截断到 80 字并去换行/路径，避免夹带参数/密钥。
- 逐条明细只在本机（`usage.jsonl`），外发的是聚合 payload。

### 说明
- 本版本聚焦 **信息回传能力（v3 定案 option-2 + option-3）**，未改动 L0 求解/校验/
  口径逻辑，49 项回归基线不受影响（`node scripts/verify.mjs` 全绿）。


## [0.1.4] - 2026-09-22

### 新增
- **PMSM（永磁）id/od 生产口径**：`lib/motor-constants.mjs` 新增 `idRatioByPoles(poles,
  {isPm, legacy})` 与 `isPmsmType(motorType)`，复刻程序侧 `physics_kernel.py:547-562`
  的 `id_ratio_by_poles` 三分支真源——
  - 永磁 `is_pm`：`0.72 + 0.010(p−2)`，钳 `[0.70, 0.80]`（`_ID_OD_PM_MIN/MAX`）
  - 生产异步：`0.55 + 0.012(p−2)`，钳 `[0.45, 0.65]`（`_ID_OD_ASYNC_MIN/MAX`）
  - legacy（零回归基线）：`0.55 + 0.03(p−2)` 无钳，与 0.1.3 / 程序 focused_scan 一致
- **`motor_type` 入参（opt-in）**：`motor_param_matrix` 新增 `motor_type` 入参，`motor_design_validate`
  V03 期望比同口径联动。传 PMSM/BLDC/IPM（含中文别名，判定复刻 `PMSM_TYPES`）时，定子内外径比按
  永磁生产口径生成（更贴合 200kW/22000rpm 这类 PMSM 主场景真实几何，id/od 落 0.70~0.80）；
  不传则维持 legacy 口径，**默认路径逐字节零回归**（缺省不产生 `motor_type` 键）。
- **knowledge-sync 真源指针修正**：`sync-constants.py` 原用 `PLUGIN_ROOT.parents[0]` 相对推导，
  独立仓（`D:\dsh-motor-ai-l0`）下解析成盘符根 `D:\`（无 `Scripts/`）→ `--check` 全判
  `source_missing`（假阴性）。现显式指真源仓 `D:\MotorDesign`，支持 `MOTOR_AI_REPO_ROOT`
  环境变量覆盖其它机器。

### 说明
- 本版本聚焦 **P0（PMSM 口径对齐 + 真源指针修正）**。程序侧 v4.02.x 另有的
  `LAMBDA_BAND_BY_POLES`（λ 分档带）与 `_DEFAULT_TYPE_BOUNDS`（类型可解区间）
  两块属「全对齐」范畴，**未在本版引入**，留待后续版本，以保护既有 49 项回归基线。

## [0.1.3] - 2026-09-22

### 新增
- **本地用量统计（零依赖 JSONL）**：每次工具调用向
  `~/.dsh/storages/dsh-motor-ai-l0/usage.jsonl` 追加一行元数据
  （`ts` / `tool` / `ok` / `elapsed_ms` / `n` / `failed` 等）——只记次数、耗时、
  规模与成败，**不记设计参数与结果内容**（行业 Know-how 留在会话内）。
  新增配置项 `usageLog`（默认开）；新增 `lib/usage-log.mjs`（所有 IO 静默降级，
  绝不影响工具主流程）。数据可直接用脚本聚合，或作为社区统计面板
  （`dsh-usage-statistics-panel` / `dsh-usage-unified` 等）的本地数据源。
  > 刻意**不采用** `inject: ['telemetry']`：核心遥测服务实名是 `sessionTelemetry`
  > （`dsh-session-telemetry-otel`），不存在名为 `telemetry` 的可注入服务，
  > 照抄会令插件永久 pending（同 apiProxy 事故机理）。

### 变更
- **工具链自描述修复**：三个工具的 `description` 显式声明流水线顺序与输入契约——
  `motor_l0_estimate` 写死「`params_list` 必须由 `motor_param_matrix` 生成，
  不接受散装命名参数」（实测首调必败点），`motor_param_matrix` /
  `motor_design_validate` 双向标注上下游。
- **SKILL 触发精确化**：`SKILL.md` frontmatter 新增 `whenToUse`（英文触发场景，
  runtime `SkillEntry.whenToUse` 实锤支持）与 `user-invocable: true`
  （可 `/motor-l0-estimate` 手动触发；注意必须用 kebab-case，
  camelCase `userInvocable` 会被 loader 当 legacy 键拒绝）；`description`
  补中文触发词（电机设计/估算/选型/参数矩阵/方案初筛）。

## [0.1.2] - 2026-09-22

### 变更
- **定位升级**：由「L0 快速预筛层」升级为「电机AI辅助设计软件插件」，覆盖电机设计全流程
  （需求解析 → 参数识别 → 候选矩阵 → L0/L1/L2 解算 → Top10 与校验 → 完整方案）；
  L1 RMxprt / L2 Motor-CAD 明确标注为**可选**精算工具。
- `package.json` 的 `description` 与 `keywords` 同步更新
  （keywords 新增 `dsh` / `motor-ai` / `l0-estimate` / `rmxprt` / `motor-cad`）。

### 文档
- **README 重构（对外）**：新增「快速开始」（可复制的 npx 安装命令 + 三工具标准流水线 + 踩坑提示）、
  「配置」表（11 项逐条说明）、「已知精度边界」；内部工程内容移出，正文精简至对外可读规模。
  - 补充关键提示：`motor_l0_estimate` 只接受 `params_list`，**不接受** `power_kw` / `speed_rpm`
    等散装命名参数，须先用 `motor_param_matrix` 生成矩阵。
  - 修正安装命令：`dsh` 未进 PATH 时须用 `npx -y @deepseek-ai/dsh plugin ...`。
- **新增 `docs/ENGINEERING.md`**（工程内参）：目录结构（同步至真实文件清单，含
  `surrogate-engine.mjs` / `models/` / `tests/` / `knowledge-sync/` / `manifest.json` / `assets/`）、
  字段契约 Param 锁、12 条校验规则、与 Python 体系 3 处差异、6 项资料稿偏差修正、W4 复盘、路线图。
- **新增 `docs/QUALITY-GATE.md`**（质量门）：两道门 + divergence 诊断、5 项偏差根因、
  首轮实算对照、待校准清单与翻转判据。
- **新增 `LICENSE`（MIT）**：`package.json` 早已声明 `"license": "MIT"` 但仓库缺少许可文件，
  本次补齐并加入 `files`。

### 修复
- **字段笔误**：`L1_MIRROR_FIELDS` 实为 **7** 项（旧文档写 8 项）；
  `l1_efficiency_proxy` 属诊断列 `L1_DIAG_FIELDS`，不是镜像字段。
- **路线图状态修正**：W5 由「待开始」更正为「完成」（代理模型 / SKILL.md / release.mjs 均已交付）；
  W6 由「待开始」更正为「部分完成」（npm 已发布）。
- `index.mjs` 头部过期注释（「本阶段不注册任何 tool」）更正为「注册 3 个 L0 工具」；
  `tempRiseRange` 语义描述澄清为「`max_temp` 钳位区间（°C），非 L0 温升（K）」。

## [0.1.1] - 2026-09-21

### 修复
- **工具参数 schema 运行时加载失败**（`dsh web` 启动报 `unsupported JSON schema: parameters.voltage_v.required must be true when present`）：
  - DSH `dsh-tools` 的 schema 编译器规定 `required` 一旦出现，值必须为 `true`；可选参数不得写 `required: false`。
  - 删除 `tools/l0/param-matrix.mjs`、`l0-estimate.mjs`、`design-validate.mjs` 三个工具全部可选参数的 `required: false`（保留真正必填项的 `required: true`）。
  - `design-validate.mjs` 的 `params`（`type: 'object'`）补 `additionalProperties: true`（编译器对 `object` 类型强制要求显式 `additionalProperties`）。
  - 已用真实 `@deepseek-ai/dsh-tools` 的 `defineTool` 对三个工具的 `TOOL_PARAMS` 做编译校验，全部通过：`motor_param_matrix` required=[power_kw,speed_rpm]、`motor_l0_estimate` required=[params_list]、`motor_design_validate` required=[]。

- **插件 apply 退出时报 `Invalid effect`**（`dsh web` 启动报 `TypeError: Invalid effect`，来自 cordis `safeCollect`）：
  - cordis 把 async 插件 `apply()` 的返回值当作「effect（清理句柄）」收集；返回普通对象 `{ isLevelEnabled, tier }` 既非函数也非 null/undefined，触发 `safeCollect` 抛错。
  - 删除 `index.mjs` `apply()` 末尾的 `return { isLevelEnabled, tier }` 块，使 apply 返回 `undefined`（无 effect），cordis 正常放行。
  - 该返回对象无外部消费方（`tierOf` 仅在日志行内联使用，`verify.mjs` 直接 import `lib/level-gate.mjs`），删除无副作用。

## [0.1.0] - 2026-09-21

阶段目标：W1-W4 — L0 预筛层可交付核心（依据《MotorAI_L0第一阶段实施方案_v3.docx》）。

> 说明：W2-W4 在同一版本号下增量交付，未单独发版；条目按阶段分组记录。

### 新增
- 插件骨架与生命周期：
  - `package.json`：DSH Bundle 声明，`files` 含 `cordis.patch.yml`，无 `mcpServers`、无 Python 依赖
  - `cordis.patch.yml`：配置层补丁，含层级总闸 `level: 'l0'` 与 L1/L2 预留注释
  - `index.mjs`：`Config` Schema + `apply()` 生命周期，启动即对越级配置快速失败
- `lib/level-gate.mjs`：层级门控
  - `assertLevelImplemented()`：非 `l0` 立即抛错，杜绝静默降级
  - `isLevelEnabled()`：总闸 + 分闸双层判断
  - `tierOf()`：层级 → 付费等级映射（l0=free / l1=vip / l2=flagship）
  - 零第三方依赖，可被 Node 直接 import 做离线断言
- `scripts/verify.mjs`：离线校验
  - `files` 必须含 `cordis.patch.yml`（否则安装方拿不到配置层）
  - `cordis.patch.yml` 结构校验（insert / id / level 默认 `l0`，L1 分闸不得默认开）
  - `CHANGELOG.md` 含当前版本条目；`SKILL.md` 三重一致性（W5 交付后生效）
  - 全量 `.mjs` 通过 `node --check`
  - level-gate 行为断言共 7 组

### 约束
- L0 为纯 JS 零依赖层：不调用 RMxprt、Motor-CAD、Python 桥接；相关能力归入 L1/L2

### 校验
- `node scripts/verify.mjs` 应全部通过（W4 后为 49/49）
- 越级配置（`level: 'l1'`）必须触发 `assertLevelImplemented` 报错而非静默降级

---

### W2-W3 增量（同版本）

- `lib/param-schema.mjs`：Param 锁 —— 字段单一真源，钉死在 `physics_kernel.py:618-646`
  与 `motor_tools.py:726-778`；导出 5 个互斥字段集合 + `pickL1Payload()` / `buildHandoff()`
- `lib/motor-constants.mjs`：常量层，全部标注 Python 真源行号；P3 高速扩表（10/12 极）
- `lib/formula-engine.mjs`：物理损耗通道（铜损 τ_p 端部物理式 / Steinmetz 铁损 / 机械损）
  + L1 同口径诊断列 `l1_efficiency_proxy` / `l1_temp_proxy`
- `tools/l0/param-matrix.mjs`：`motor_param_matrix`，复刻 `focused_scan` 且去随机化
- `tools/l0/l0-estimate.mjs`：`motor_l0_estimate`，双字段结果 + TopN 排序 + L0→L1 交接载荷
- 修订资料稿偏差 6 项（详见 README 第五节），含冷却系数量级重标定与「1 极档」物理错误作废

---

### W4 增量（同版本）

- `lib/design-rules.mjs` + `tools/l0/design-validate.mjs`：`motor_design_validate`
  - 12 条物理一致性规则（V01~V12），三级结论 `passed / warning / failed`
  - `escalate` 可把软规则临时升为硬剔除；缺输入记入 `skipped` 而非静默通过
  - 批量模式产出 `rule_hits`，可直接定位参数矩阵的生成偏差
- `lib/regression-gate.mjs` + `scripts/regression.mjs` + `benchmarks/`：回归质量门
  - 门 A 物理边界（能效带 + 绝缘限值）、门 B 回归（基线快照劣化检出）、诊断 divergence
  - `SIM_VALID_DOMAIN` 外案例标记 `out_of_domain`，不计入 2pt 预算
  - `--update-baseline` / `--strict` / `--json` 三种运行模式
- `index.mjs` 启用工具注册（3 个工具）；新增 `airGapFluxT` / `insulationClass` / `highSpeedRpm` 配置

### W4 修复
- 磁密公式补正弦平均因子 2/π（`Bt = 2·B_gap·Dsi/(Qs·bt·k)`，修正后 1.04T 对齐现场 1.02T）
- V12 温升默认降为 warning（未标定模型不应批死整批，实测 40/40 → 0/40）
- 槽宽深比下界 0.30 → 0.20（26/40 误报）
- 新增高速气隙档 `highSpeedRpm=8000` / `AIR_GAP_MAX_HIGH_SPEED=4.0`

### W4 已知欠账
- 质量门判定 **DEBT**：6/6 案例效率低于参考带 0.35~2.96pt，温升普遍越考核限值
- 根因同 README 第九节 5 项（损耗模型未标定 + 散热筋未计），需 RMxprt 批量结果回归后收紧
- `gate.sanity_blocking` 标定收敛后必须翻为 `true`
