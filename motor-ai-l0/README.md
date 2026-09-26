# 电机AI辅助设计软件插件

`dsh-motor-ai-l0` · DSH 插件（bundle） · 纯 JS，零求解器依赖

覆盖电机设计全流程：需求解析 → 参数识别 → 候选矩阵 → L0/L1/L2 解算 → Top10 与校验 → 完整方案。
L0 用代理模型与经验公式毫秒级广筛候选；L1 RMxprt、L2 Motor-CAD 为**可选**精算工具。
装完即用 —— 无需预装 RMxprt / Motor-CAD / Python，无需 license。

## 快速开始

```bash
# 安装（dsh 未进 PATH 时用 npx）
npx -y @deepseek-ai/dsh plugin --profile web add dsh-motor-ai-l0

# 离线自检（无需 DSH Runtime，裸 Node 即可）
node scripts/verify.mjs
```

**标准流水线（顺序很重要）**

1. `motor_param_matrix` —— 由功率 / 转速 / 电压生成候选参数矩阵
2. `motor_design_validate` —— 批量物理校验，剔除 `failed`
3. `motor_l0_estimate` —— 对剩余候选排序，输出 TopN 与 L1 交接载荷

> ⚠️ `motor_l0_estimate` 只接受 `params_list`（每项含 16 个 L1 顶层字段），**不接受** `power_kw` / `speed_rpm` 这类散装命名参数 —— 请先用第 1 步生成矩阵再传入。
> L0 结果只用于**排序与相对比较**，不用于绝对值交付；输出恒带 `solve_mode='l0'` 标记，最终结论须由 L1/L2 给出。
>
> **⚠️ surrogate 通道为实验特性（0.1.7 标注）**：`l0Mode='surrogate'` 当前仅供实验——训练样本 od_range（约 612~2651mm）与真实中小机座（100~500mm）不匹配，段外样本全部降级公式通道；PMSM 段 cv_r2≈0 且特征含公式效率泄漏（`l0_eff`）。启用时工具输出带 `surrogate_experimental: true` 警告字段。**排序决策请使用默认公式通道**，重训前不承诺预测质量。

## 能力边界

| 层级 | 引擎 | 速度 | 范围 | 付费 | 本版本状态 |
|---|---|---|---|---|---|
| **L0** | 代理模型 + 经验公式 | 毫秒级 | 全部方案 | free | ✅ 已交付核心 |
| L1 | RMxprt 磁路法（可选） | 1–3s/方案 | TopN | vip | ⛔ 预留（接口已留） |
| L2 | Motor-CAD 有限元（可选） | ~200s/方案 | Top3 | flagship | ⛔ 预留（接口已留） |

本版本做参数矩阵生成、物理校验、L0 估算排序、TopN 交接与回归质量门；不做 RMxprt / Motor-CAD 调用、MCP Server、Python 桥接、多目标优化。

## 工具

| 工具 | 作用 | 关键入参 |
|---|---|---|
| `motor_param_matrix` | 生成候选参数矩阵（聚焦扫描，非笛卡尔积） | `power_kw` `speed_rpm` `voltage_v` `poles` `count` |
| `motor_l0_estimate` | 毫秒级估算 + 排序 + L1 交接载荷 | `params_list` `top_n` `sort_by` |
| `motor_design_validate` | 12 条物理一致性校验，返回 `passed`/`warning`/`failed` | `params_list` `escalate` |

入参别名：`power`→`power_kw`、`rpm`→`speed_rpm`、`volt`→`voltage_v`。
`sort_by` 白名单：`efficiency` `torque_density` `temp_rise` `total_loss` `power`（`temp_rise`、`total_loss` 升序，越小越好）。

## 配置

配置位于 `cordis.patch.yml`（查看：`npx -y @deepseek-ai/dsh --profile web --dump-config`）。最常用四项：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `level` | `'l0'` | 层级总闸。设为 `l1`/`l2` 立即抛错（快速失败，杜绝静默降级） |
| `l0Mode` | `'auto'` | `formula` 纯公式 / `surrogate` 代理模型 / `auto` 自动降级 |
| `efficiencyCap` | `96` | 效率封顶（与 L1 同口径，避免两层排序跳变） |
| `insulationClass` | `'F'` | 绝缘等级，决定温升限值（B=80K / F=105K / H=125K） |

其余配置项（本地统计 `usageLog`；L0 运行 `surrogatePath` / `surrogateConfidenceThreshold` / `maxMatrixSize` / `topNPreview` / `tempRiseRange` / `airGapFluxT` / `highSpeedRpm`；脱敏回传 `telemetryEnabled` / `telemetryEndpoint` / `telemetryBatchSize` / `telemetryIntervalSec` / `sessionTelemetry`）见 [`docs/ENGINEERING.md`](docs/ENGINEERING.md#八配置全表) 与本文「脱敏聚合指标回传」节。
> ⚠️ `tempRiseRange` 命名待议：它对齐 L1 `max_temp` 的钳位区间（°C），不是 L0 温升（K），拟改 `maxTempClamp`。

## 用量统计（本地，隐私安全）

每次工具调用向 `~/.dsh/storages/dsh-motor-ai-l0/usage.jsonl` 追加一行**元数据**：

```json
{"ts":"2026-09-22T06:40:12.345Z","tool":"motor_l0_estimate","ok":true,"elapsed_ms":2,"n":20,"failed":0,"sort_by":"efficiency","top_n":10}
```

- 只记次数 / 耗时 / 规模 / 成败，**不记设计参数与结果内容** —— 行业 Know-how 不落盘
- 开关：配置项 `usageLog`（默认开）；所有写入静默降级，绝不影响工具调用
- 数据可自行聚合（周期 / 命中率 / 失败率），也可作为社区统计面板
  （`dsh-usage-statistics-panel`、`dsh-usage-unified` 等 DSH 社区插件）的本地数据源搭配使用
- 累积的真实工况分布后续直接服务公式系数标定与代理模型训练

## 脱敏聚合指标回传（显式可选，默认关闭）

在本地 `usage.jsonl` 之上，**可选**地把「按工具聚合的统计量」回传到你指定的端点
（如管理端 `/api/admin/dsh-plugins/<id>/usage-report`）。这是**显式 opt-in**：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `telemetryEnabled` | `false` | 回传总开关。**须为 `true` 且 `telemetryEndpoint` 非空才生效** |
| `telemetryEndpoint` | `''` | 回传目标 URL（POST JSON）；空 = 不外发 |
| `telemetryBatchSize` | `50` | 单批最多携带的用量记录条数（分批推进，失败不推进、下批重报） |
| `telemetryIntervalSec` | `300` | 回传周期（秒）；`0` = 仅进程退出时 flush 一次 |
| `sessionTelemetry` | `'auto'` | 是否顺带挂 DSH Runtime `sessionTelemetry` 瀑布（运行时探测，不静态 inject） |

**合规底线（与 DSH 生态遥测规范一致）：**
- 默认全关，不配置 = 零外发（本地 `usage.jsonl` 照记，行为与 0.1.4 逐字节一致）
- 只回传**白名单聚合统计量**（调用次数 / 成败 / 耗时分布 / 规模总和 / 成功速率 / 来源版本 /
  平台 OS）；**绝不包含**设计参数正文、结果明细（效率/温升/损耗）、提示词、工作目录、密钥
- 逐条明细只保留在本机；`error` 字段截断到 80 字并去换行/路径，避免夹带参数/密钥
- 回传是 fire-and-forget：网络/超时/拒绝都静默降级，**绝不影响**工具主流程

启用示例（`cordis.patch.yml`）：
```yaml
telemetryEnabled: true
telemetryEndpoint: 'http://127.0.0.1:5000/api/admin/dsh-plugins/motor-ai-l0/usage-report'
telemetryIntervalSec: 300
```

## 已知精度边界

L0 物理通道目前是 **v0.1 未标定版**（回归质量门判定 DEBT）：6 个标定案例效率低于参考带 0.35~2.96pt，温升偏保守。根因是损耗模型尚未用 RMxprt 批量结果标定（铁损未分齿/轭、铜损缺电路约束、机械损未标定、散热筋未计）。
因此 L0 的正确用法是**排序与相对比较**，不是绝对值采信 —— **排序稳定性优于绝对精度**。完整实算数据与标定计划见 [`docs/QUALITY-GATE.md`](docs/QUALITY-GATE.md)。

## 文档与许可

- [`docs/ENGINEERING.md`](docs/ENGINEERING.md) —— 目录结构、字段契约 Param 锁、12 条校验规则、与 Python 体系差异、工程复盘、配置全表、路线图
- [`docs/QUALITY-GATE.md`](docs/QUALITY-GATE.md) —— 回归质量门（两道门 + divergence）、实算对照、待校准清单
- [MIT](LICENSE) © Motor-AI
