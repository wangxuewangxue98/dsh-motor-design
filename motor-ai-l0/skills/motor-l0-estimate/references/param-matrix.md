# 参数矩阵生成规范

> 实现：`tools/l0/param-matrix.mjs`
> 复刻对象：`Scripts/physics_kernel.py:focused_scan`

## 1. 生成策略：聚焦扫描，不是笛卡尔积

原资料稿用四层笛卡尔积（外径 × 槽数 × 磁钢厚 × 匝数），问题有两个：

1. 组合爆炸，绝大多数组合在物理上不成立（内径与槽数不匹配等）
2. 与 Python 主线 `focused_scan` 的输出分布不一致，L0 排序无法与 L1 对齐

因此改为**聚焦扫描**，按物理量逐级派生：

```
① 功率 → 基准尺寸（Dsi 基准）
② Dsi 分档（5 档）→ 反算 Dso = Dsi / idRatio(poles)
③ 外径超限？→ 夹紧 Dso 并反算 Dsi（不是丢弃！）
④ 极数候选（推荐极数 ± 邻近）
⑤ 槽配合按索引轮询（SLOT_MAP）
⑥ 匝数扰动 [-2, 0, +2]
⑦ λ 夹紧：调整 core_length 使 λ = L/Dsi 落入极数对应区间
⑧ 派生齿宽/轭厚/轴径/转子外径
```

## 2. 关键设计决策

### 2.1 超限夹紧而非丢弃

早期实现用 `continue` 丢弃超限候选，导致 `200kW + 外径≤300mm` 产出 **0 行**。
现改为**夹紧外径并反算内径**，并在 `_physics.od_clamped` 打标。

> 理由：预筛层的价值在于"在约束内给最好的答案"，
> 空矩阵等于工具失效。夹紧后由 V03（内外径比）去告警，而不是直接抹掉。

### 2.2 确定性（去随机化）

Python 主线用 `random.choice` 选槽配合，无法复现。
L0 改为**索引轮询** `slotPairs[comboIndex % slotPairs.length]`。

> 硬规则：同 spec 两次调用必须逐字节一致。
> 校验项 `matrix: 生成结果完全确定` 守着这条。

### 2.3 极数候选

| 转速 | 推荐极数 |
|---|---|
| ≥ 12000 rpm | 2 |
| ≥ 2500 rpm | 2 |
| ≥ 1400 rpm | 4 |
| ≥ 900 rpm | 6 |
| 其他 | 8 |

> ⚠ **永磁同步机不存在 1 极**。资料稿曾为 200kW/22000rpm 设"1 极档"，
> 已作废。频率反推可证伪：f = 22000 × 2 / 120 = **366.7Hz**，
> 与现场记录的 f≈367Hz 完全吻合 —— 该工况就是 2 极。

### 2.4 λ 区间（长径比）

按极数分档（`LAMBDA_RANGE`），10/12 极由 `LAMBDA_RANGE_EXT` 补。
`fitLambdaWithinRange()` 会把 `core_length` 夹进区间，
因此**不会出现 80mm 默认值兜底**。

## 3. 输出形状

每行严格包含：

- **16 个 L1 矩阵顶层字段**（顺序同 `physics_kernel.py:618-634`）
  `stator_od / stator_id / rotor_od / core_length / air_gap / tooth_width /
   yoke_thickness / shaft_dia / poles / voltage / speed / peak_current /
   slots_stator / slots_rotor / turns_per_coil / parallel_circuits`
- **`_physics`** 派生量：
  `d_squared_l / lambda / lambda_valid / lambda_advice / estimated_torque / od_clamped`
- **L0 私有**：`power_kw` / `torque_nm` / `cooling`

## 4. 截断语义

```
truncated = 请求 count > 实际返回行数
```

> 早期实现拿"被 maxMatrixSize 夹断后的 count"跟自己比，恒为 false，
> 掩盖真实截断。已修。

## 5. 入参别名

外部写法由 `normalizeSpec` 归一（如 `power`→`power_kw`、`rpm`→`speed_rpm`、
`slots`→`slots_stator`）。归一化后**只认规范名**，不接受第二套管用。

## 6. 快速失败

`power_kw ≤ 0`、`speed_rpm ≤ 0` 直接抛错，不返回空矩阵。
