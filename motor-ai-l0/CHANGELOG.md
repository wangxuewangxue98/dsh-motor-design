# 更新日志

本项目的所有重大变更都记录在此文件中。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

版本三重一致性：`package.json.version` = `SKILL.md` 的 `metadata.version` = `CHANGELOG.md` 最新条目。

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
