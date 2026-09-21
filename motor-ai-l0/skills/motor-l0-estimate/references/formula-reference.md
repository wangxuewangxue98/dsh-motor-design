# 经验公式说明与系数来源

> 真源文件：`lib/formula-engine.mjs`、`lib/motor-constants.mjs`
> 所有常量带 Python 真源行号标注，禁止在本文件里另立一套数值。

## 1. 基础量

| 量 | 公式 | 真源 |
|---|---|---|
| 转矩 | `T = 9550 · P(kW) / n(rpm)` | `physics_kernel.py:63-67` |
| 电频率 | `f = n · p / 120` | 通用 |
| D²L | `D²L = Dsi² · L`（单位 m³） | `physics_kernel.py` 同名函数 |
| 长径比 | `λ = L / Dsi` | 同名函数 |
| 同步功率 | `P = T · n / 9550` | 与转矩式互逆 |

## 2. 效率

效率走**物理损耗通道**：先算铜损 / 铁损 / 机械损，再由总损耗反推效率，
最后按 `efficiencyCap`（默认 96）封顶。

```
P_out = P_rated
P_loss = P_cu + P_fe + P_mech
η = P_out / (P_out + P_loss) × 100%
η = min(η, efficiencyCap)
```

**封顶口径**：`efficiencyCap=96` 对齐 Python 侧 `_run_simulated`
（`Scripts/motor_tools.py:752`）。目的是避免同一设计在 L0 / L1 之间
排序跳变 —— 两边用同一口径比"谁更准"更重要。

> ⚠ 当前 96 对高速高效机偏保守（推算值可达 97.5% 被压住）。
> 待 W4 标定收敛后重评，`references` 与 `motor-constants.mjs` 同步更新。

## 3. 铜损

走**电路约束**而非经验系数：

```
槽深   h_s ≈ 0.60 · (Dso - Dsi)/2        （同 physics_kernel.py:601）
槽宽   b_s = 齿距 - 齿宽                 （齿距按平均直径处圆周节距）
槽面积 A_slot = b_s · h_s
导体体 V = Qs · A_slot · L · k_end       （k_end=1.35 计端部）
P_cu  = ρ(T) · J² · V
```

- `ρ(T)`：铜电阻率随温度线性修正
- `J`：电流密度，取 `CURRENT_DENSITY_REF`（A/mm²）
- `k_end = 1.35`：端部约占直线段 35%

## 4. 铁损（Steinmetz 简化）

```
P_fe = (kh · f · B^α + ke · f² · B²) · m_core
```

系数按**硅钢片牌号 50W600** 重标定（`STEINMETZ` 常量）：
`α ≈ 1.6`，`kh`、`ke` 见 `motor-constants.mjs`。

> 已知欠账：未分齿、轭两段分别计算。齿部磁密高于轭部，
> 用单一 B 会把铁损算低。列入 W4 标定清单。

## 5. 机械损

风磨损耗标定式：

```
P_mech ∝ ω³ · r⁴ · L
```

> 已知欠账：系数尚未用实测/仿真数据标定，当前量级偏保守。

## 6. 温升

热负荷法：

```
ΔT = P_loss / (h · A)
A  = π · Dso · L · 1.5      （1.5 计端部散热面）
```

对流换热系数 `h`（W/(m²·K)）按冷却方式取：

| 冷却 | h |
|---|---|
| natural | 25 |
| forced_air | 140 |
| liquid_jacket | 350 |
| oil_spray | 500 |
| oil_immersed | 650 |

> 📌 这组值**修正自原始资料稿**。资料稿给的是 `{8,15,40,60,80}`，
> 实算会令温升虚高一倍以上。按 TEFC 整机典型散热量级重新标定。
> 依据写在 `motor-constants.mjs` 的 `COOLING_COEFFICIENT` 注释里。

> 已知欠账：散热面积未计散热筋（机壳带筋可增加 30~60% 有效面积）。

## 7. 磁密反算（V07 / V08 用）

**齿部**：

```
每极磁通   Φ = (2/π) · B_peak · (π·Dsi/p) · L
每极下齿数 = Qs / p
单齿截面   = bt · L · k_stack
Bt = Φ / [(Qs/p) · bt · L · k_stack]
   = 2 · B_gap · Dsi / (Qs · bt · k_stack)
```

> ⚠ **2/π 是正弦磁通的平均因子**。初版遗漏该因子，磁密被高估 π/2 ≈ 1.57 倍，
> 制造出"齿磁密 1.63T vs 现场目标 1.02T"的假性冲突。修正后半齿距齿宽下
> `Bt = 4·B_gap/(π·k_stack) ≈ 1.30·B_gap`；取 B_gap=0.8T 得 **1.04T**，
> 与现场口径 1.02T 吻合到 2%。

**轭部**：

```
By = B_gap · π · Dsi / (2 · p · yoke · k_stack)
```

## 8. 叠压系数

`k_stack = 0.98`（用户现场口径，定子冲片叠压）。

## 9. 已知偏差清单（W4 待校准）

| # | 项目 | 影响 |
|---|---|---|
| 1 | 铁损未分齿轭（单一 B） | 铁损偏低 |
| 2 | 铜损缺电路约束（曾为经验系数） | 已改物理式，待标定 J |
| 3 | 机械损未标定 | 量级偏保守 |
| 4 | 散热面积未计散热筋 | 温升偏高 |
| 5 | efficiencyCap=96 权宜值 | 高速高效机被压 |

标定目标：|Δη| ≤ 2pt、|Δ温升| ≤ 10K。
