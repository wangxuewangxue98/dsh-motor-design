# 算例集 · dsh-motor-ai-l0

本目录的每个算例都是 **v0.2.2 实跑产出**（`l0Mode: auto`，裸 Node 直调，无需 DSH Runtime / RMxprt / Motor-CAD），
数值未经人工修饰 —— 包括做不好的那一个。

| # | 算例 | 工况 | 结果概览 | 用途 |
|---|---|---|---|---|
| [01](01-y2-15kw-1460rpm.md) | Y2 系列 15kW 异步 | 15 kW / 1460 rpm / 380 V | 120 候选 → 80 可行，Top1 效率 95.17%、温升 14.6 K，**16 ms** | 入门：看完整输出长什么样 |
| [02](02-75kw-1480rpm-660v.md) | 75kW 异步（矿用档） | 75 kW / 1480 rpm / 660 V | 120 候选 → 80 可行，效率封顶并列，**4 ms** | 进阶：效率并列时如何用损耗/温升二次排序 |
| [03](03-pmsm-200kw-22000rpm.md) | 200kW 高速 PMSM | 200 kW / 22000 rpm / 380 V | **0 可行**，`recommended = null` | 边界：看清 L0 不适用什么（反例） |

## 三步流水线（顺序固定）

```text
motor_param_matrix       功率/转速/电压 → 候选参数矩阵
        ↓
motor_design_validate    12 条物理校验，剔除 failed
        ↓
motor_l0_estimate        毫秒级估算 + 排序 + TopN + L1 交接载荷
```

> `motor_l0_estimate` 只接受 `params_list`（由第 1 步生成，每项含 16 个 L1 顶层字段），
> **不接受** `power_kw` / `speed_rpm` 这类散装参数。

## 复现

所有算例文件末尾都带有可直接粘贴的 `node -e` 命令，在插件根目录执行即可复现（Node.js ≥ 22）：

```bash
cd dsh-motor-ai-l0
node -e "<算例文件中的命令>"
```

## 关于数值的可信度

| 用途 | 是否可信 |
|---|---|
| 方案之间的**相对排序** | ✅ L0 的设计目标，排序稳定性优先 |
| 效率 / 温升的**绝对值** | ⚠️ 本版物理通道未标定，须由 L1（RMxprt）复核 |
| 高速 PMSM（>10000rpm）任何结论 | ❌ 见算例 03 |

细节见根目录 [README.md](../README.md)「已知精度边界」与 [docs/QUALITY-GATE.md](../docs/QUALITY-GATE.md)。
