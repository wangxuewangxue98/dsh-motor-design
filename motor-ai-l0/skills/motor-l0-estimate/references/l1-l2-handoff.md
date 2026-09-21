# L0 → L1 / L2 交接约定

> 目标：L0 的 TopN 能**零翻译**喂给 L1 批量求解。

## 1. 为什么需要"零翻译"

L1（RMxprt 磁路法）的入参 Schema 就是 `physics_kernel.focused_scan`
产出的那 16 个字段。只要 L0 的参数字典逐字段镜像它们，
TopN 就可以直接进 `batch_solve`，不需要任何适配层。

> 反面案例：原资料稿用 `{slots, cooling, speed_rpm}`，
> 与 Python 真源 `{slots_stator/slots_rotor, voltage, speed}` 完全对不上，
> 交接时必须写一层翻译 —— 那层翻译就是 bug 温床，也是字段漂移的起点。

## 2. 交接载荷格式

```json
{
  "handoff": {
    "from_level": "l0",
    "to_level": "l1",
    "candidates": [ ... ],
    "l0_ranking": ["efficiency", "torque_density"],
    "timestamp": "2026-09-21T04:00:00Z"
  }
}
```

`candidates` 中每项只包含 `HANDOFF_WHITELIST` 内的字段（= L1 矩阵字段全量）。

## 3. 字段隔离规则

| 类别 | 字段 | 是否进交接 |
|---|---|---|
| L1 矩阵字段 | `stator_od` / `poles` / `voltage` / `speed` / … | ✅ |
| L0 私有 | `power_kw` / `torque_nm` / `cooling` | ❌ 剔除 |
| L0 原生结果 | `efficiency` / `temp_rise` / `torque_density` / … | ❌ 剔除 |
| L1 镜像结果 | `max_temp` / `power` / `copper_loss` / … | ❌ 剔除 |
| `l1_handoff` 自身 | — | ❌ 禁止嵌套 |

> `pickL1Payload()` 自动执行这套过滤，并兼容**平铺**与
> **`{params, ...}` 嵌套**两种结果形状。

## 4. 双字段策略（v2 定案②）

L0 单行结果同时带两套字段：

**原生 6 字段**（L0 自己的物理口径）
`efficiency` / `torque` / `temp_rise` / `total_loss` / `torque_density` / `prediction_source`

**L1 镜像字段**（与 `_run_simulated` 同名，供下游直接消费）
`max_temp` / `power` / `copper_loss` / `iron_loss` / `mechanical_loss` /
`solve_mode='l0'` / `l1_efficiency_proxy` / `l1_handoff`

**自洽硬约束**：镜像损耗之和必须 ≈ 原生 `total_loss`（容差 3W）。
同一行出现两个损耗口径是严重缺陷，由校验项守着。

> ⚠ 早期实现直接照抄 `_run_simulated` 的经验式，
> 在 200kW/22000rpm 推出 `total_loss=5185W` 却 `copper_loss=22000W` 的
> 自相矛盾行。根因是那套经验式只在 ~180mm/8极/3000rpm 附近标定。
> 现镜像字段一律取 L0 物理通道，仿真口径降级为**诊断列**。

## 5. 诊断列（不阻断，只暴露）

`l1_efficiency_proxy` / `l1_temp_proxy` 保留 Python 仿真经验式的输出，
用途是**暴露两套模型的分歧**，供回归门判定域外失真。

> 案例：200kW/22000rpm 下 L0 给 96%，仿真 proxy 给 80%（差 16pt）。
> 这不是 bug —— 80% 比 IE1 地板还低，是**仿真模型自身在高速区失真**。
> 强行对齐等于把 L0 一起带偏。故只有落在 `SIM_VALID_DOMAIN` 内的案例
> 才计入 2pt 预算，域外标 `out_of_domain`，可见但不计分。

## 6. L1 实现时的动作清单

1. 新增 `mcp/` 目录，实现 PyAEDT 桥接
2. 在 `package.json` 的 `dsh.mcpServers` 中声明
3. 把 `level-gate.mjs` 的 `IMPLEMENTED_LEVELS` 允许列表扩展为 `['l0','l1']`
4. 新增 `tools/l1/` 目录，实现 RMxprt 批量求解工具
5. 更新 `l1Enabled` 的 `.description()`（去掉"当前不生效"）
6. 消费本文件 §2 的 `handoff` 格式 —— **不需要改 L0 任何代码**

## 7. L2 同理

Motor-CAD 走 PyMotorCAD 桥接，允许列表扩到 `['l0','l1','l2']`，
新增 `tools/l2/`。交接格式不变。
