# 电机AI辅助设计软件插件

`dsh-motor-ai-l0` · DSH 插件（bundle） · 纯 JS，零求解器依赖

> **不装 RMxprt / Motor-CAD / Python / license，把 120 个候选在十几毫秒内压成 Top5，
> 只把求解器时间花在最值得的那几个上。**

📁 **[算例集：3 个真实算例（含反例）→ examples/](examples/README.md)** · [能力边界](#已知精度边界)

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

> ⚠️ **推荐用聚合工具 `motor_l0_pipeline` 一步跑完上述三步链**。DSH 对话上下文有 **2000 行读取上限**：
> 当 `count=120` 时，单 `motor_param_matrix` 就回显 4000+ 行，模型静默丢行后计数对不上 120 还**无报错**——
> 这是「让模型亲手编排三步链」的结构性风险（三案例实战全中）。聚合工具在插件进程内跑完三步链，
> 只回吐紧凑摘要（计数自检 + Top10 + 交接载荷 + 文件交接路径），上下文恒 < 2000 行，从根上绕开截断。
> 只有在单独调试某一步（查完整校验报告、手调 failed 阈值）时才拆开用三个独立工具。

> ⚠️ `motor_l0_estimate` 只接受 `params_list`（每项含 16 个 L1 顶层字段），**不接受** `power_kw` / `speed_rpm` 这类散装命名参数 —— 请先用第 1 步生成矩阵再传入。
> L0 结果只用于**排序与相对比较**，不用于绝对值交付；输出恒带 `solve_mode='l0'` 标记，最终结论须由 L1/L2 给出。
>
> **两条通道的分工（v0.2.2）**
>
> | 通道 | 触发 | 输出 | 适用 |
> |---|---|---|---|
> | **公式通道**（默认） | `l0Mode: 'auto'` / `'formula'` | 效率 + 温升 + 损耗拆分 + 齿/轭磁密 + 功率因数 + 可行判定 —— 字段完整 | 日常排序与相对比较 |
> | **代理通道**（可选增强） | `l0Mode: 'surrogate'` | **仅效率**（温升/损耗/磁密不输出） | 异步机效率排序的交叉参考 |
>
> 代理模型 v2.3.0-fixseg（在 v2.2.0 基础上用全量 6029 条真实 RMxprt 结果重训，特征扩为含 `poles` 的 5 维；`designs.db.l0_residuals`，`fault=0`，效率 ∈ [50,99]），按机座外径分三段：
>
> | 机种 | 样本 OD 覆盖 | 样本数 | 5 折 CV R² | MAE |
> |---|---|---|---|---|
> | 异步 small / medium / large | 150.6–756.5 / 151.3–1085.4 / 1196–2651 mm | 1291 / 2578 / 1276 | 0.652 / 0.438 / 0.670 | 1.44 / 3.29 / 4.53 pp |
> | PMSM small / medium / large | 612–1178 mm（均为大机座） | 367 / 745 / 355 | ≤ 0.31 → **自动降级公式通道** | — |
>
> 因此 **PMSM 与段外样本始终走公式通道**；`auto` 默认走字段完整的公式通道。启用代理时输出带 `surrogate_experimental: true`，且置信度低于阈值（`surrogateConfidenceThreshold`，默认 0.4）自动降级 —— 不会硬给一个数。

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
| `motor_l0_pipeline` | **推荐入口**：三步链聚合，只回紧凑摘要 + 文件交接，根治 2000 行截断 | `power_kw` `speed_rpm` `count` `top_n` `write_handoff` |
| `motor_param_matrix` | 生成候选参数矩阵（聚焦扫描，非笛卡尔积） | `power_kw` `speed_rpm` `voltage_v` `poles` `count` |
| `motor_l0_estimate` | 毫秒级估算 + 排序 + L1 交接载荷（默认 Top10） | `params_list` `top_n` `sort_by` |
| `motor_design_validate` | 17 条物理一致性校验，返回 `passed`/`warning`/`failed` | `params_list` `escalate` `bg_caliber` |

入参别名：`power`→`power_kw`、`rpm`→`speed_rpm`、`volt`→`voltage_v`。
`sort_by` 白名单：`efficiency` `torque_density` `temp_rise` `total_loss` `power`（`temp_rise`、`total_loss` 升序，越小越好）。

## 示例（15kW / 1460rpm / 380V 异步，实跑 16 ms）

```text
$ motor_param_matrix       { power_kw: 15, speed_rpm: 1460, voltage_v: 380, motor_type: 'induction', count: 120 }  →  120 候选
$ motor_design_validate    17 条物理校验  →  0 passed / 80 warning / 40 failed
$ motor_l0_estimate        sort_by: 'efficiency'  →  Top5，OD310 / 6 极 / η=95.17%
```

| # | OD (mm) | L (mm) | 极数 | 槽数 | 效率 (%) | 温升 (K) | 轭磁密 (T) | 总损耗 (W) |
|---|---|---|---|---|---|---|---|---|
| 1 | 310 | 226.4 | 6 | 72 | 95.17 | 14.6 | 1.39 | 604 |
| 2 | 327 | 216.6 | 6 | 72 | 95.02 | 14.6 | 1.38 | 628 |
| 3 | 278 | 187.1 | 6 | 72 | 94.94 | 20.5 | 1.38 | 642 |
| 4 | 343 | 206.7 | 6 | 72 | 94.89 | 14.7 | 1.38 | 650 |
| 5 | 294 | 177.2 | 6 | 72 | 94.81 | 20.5 | 1.38 | 663 |

→ 这 5 个方案直接作为 L1（RMxprt）的入参，而不是把 120 个全丢给求解器。
完整算例、复现命令与另外两个工况（75kW 异步、200kW 高速 PMSM **反例**）见 **[`examples/`](examples/README.md)**。

## 配置

配置位于 `cordis.patch.yml`（查看：`npx -y @deepseek-ai/dsh --profile web --dump-config`）。最常用四项：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `level` | `'l0'` | 层级总闸。设为 `l1`/`l2` 立即抛错（快速失败，杜绝静默降级） |
| `l0Mode` | `'auto'` | `formula` 纯公式（字段完整）/ `surrogate` 代理模型（**仅输出效率**）/ `auto` = 公式通道，段外与低置信度自动降级 |
| `surrogatePath` | `'models/l0_surrogate_family.json'` | 代理模型路径（v2.3.0-fixseg） |
| `surrogateConfidenceThreshold` | `0.4` | 代理置信度门限，低于此值降级公式 |
| `efficiencyCap` | `96` | 效率封顶（与 L1 同口径，避免两层排序跳变）。v0.2.4 起仅钳**展示值** `efficiency`，排序按 `efficiency_raw`（未封顶真值） |
| `insulationClass` | `'F'` | 绝缘等级，决定温升限值（B=80K / F=105K / H=125K）。**v0.2.5**：行内 `insulation_class` 优先于本项 |
| `line_freq_hz` | `50` | 电源频率 (Hz)，合法区间 20~400Hz。**60Hz 电网与 VFD 变频工况必须设置**，否则 V16 按 50Hz 判超同步（4极@1500rpm 会形成假断崖）。非法值判 V18 failed，不静默回落 |

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

## 社区反馈（结论式，永不自动提交）

**遇到「我的工况跑不出方案 / 结果明显不合理」时，可以把结论反馈给作者。**
插件提供 `tools/feedback/case-feedback.mjs`，在你自己的机器上生成一份可直接粘贴的 issue 草稿：

```js
import { buildCaseFeedbackDraft } from 'dsh-motor-ai-l0/tools/feedback/case-feedback.mjs'

const draft = buildCaseFeedbackDraft({
  pluginVersion: '0.2.5',
  cases: [{ label: '工况A', spec: { power_kw: 450, voltage_v: 690, speed_rpm: 985, poles: 6 } }],
})
// 粘贴到 https://github.com/wangxuewangxue98/dsh-motor-ai-l0/issues/new?template=case-feedback.yml
```

草稿只含三类内容：

| 层次 | 例子 | 是否回传 |
|---|---|---|
| **L1 明细** | `stator_od=425, l0_eff=93.73` | ❌ **永不** |
| **L2 结论** | `1400~1500rpm 可行率 66.7% → 1500~3000rpm 33.3%` | ✅ |
| **L3 元统计** | `V16 failed×40` | ✅ |

**三条硬约束：**
1. **工具只返回草稿字符串**，不导出 `params` / `matrix` —— 脱敏不靠过滤，靠不产出。
2. 草稿生成后强制过 **10 项脱敏自检**（几何/槽数/匝数/电流/效率/客户名/绝对路径/UNC/哈希/邮箱），
   命中任一项即抛错、拒绝输出。
3. **永不实现自动提交**。自动上报是信任的重灾区，作者需要收到有人愿意署名的东西。

**闭环承诺**：反馈修复后，该工况会被匿名化纳入 `benchmarks/` 回归用例，由 CI 永久保护。

提交前请看 [.github/ISSUE_TEMPLATE/](.github/ISSUE_TEMPLATE/) —— 有问题反馈、案例反馈、
标定数据贡献三套模板。

## 已知精度边界

L0 物理通道目前是 **v0.1 未标定版**（回归质量门判定 DEBT）：6 个标定案例效率低于参考带 0.35~2.96pt，温升偏保守。根因是损耗模型尚未用 RMxprt 批量结果标定（铁损未分齿/轭、铜损缺电路约束、机械损未标定、散热筋未计）。
因此 L0 的正确用法是**排序与相对比较**，不是绝对值采信 —— **排序稳定性优于绝对精度**。完整实算数据与标定计划见 [`docs/QUALITY-GATE.md`](docs/QUALITY-GATE.md)。

**明确不适用的工况**（实测，见 [`examples/03`](examples/03-pmsm-200kw-22000rpm.md)）：

| 工况 | 表现 | 建议 |
|---|---|---|
| 高速 PMSM（>10000rpm） | 轭部磁路模型外推失真（轭磁密算得 >5 T，物理不可能）、温升 300 K+；门控会拒绝输出任何可行方案（`recommended = null`） | 直接进 L1 / L2 |
| 效率绝对值交付 | 本版未标定，6 个标定案例偏差 0.35~2.96 pt | 仅排序，绝对值由 L1 复核 |

> 门控"宁可交白卷，也不给假答案"是刻意设计：`level` 设为 `l1`/`l2` 时插件立即抛错，绝不静默降级成 L0 结果。

## 文档与许可

- [`docs/ENGINEERING.md`](docs/ENGINEERING.md) —— 目录结构、字段契约 Param 锁、17 条校验规则、与 Python 体系差异、工程复盘、配置全表、路线图
- [`docs/QUALITY-GATE.md`](docs/QUALITY-GATE.md) —— 回归质量门（两道门 + divergence）、实算对照、待校准清单
- [MIT](LICENSE) © Motor-AI
