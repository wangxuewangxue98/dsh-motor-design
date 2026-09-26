# dsh-motor-ai-l0 · 市场 / 社区列表元数据

> 用于 DSH 插件市场、WorkBuddy 市场与社区发布帖。版本号与 `package.json` / `manifest.json` 保持一致。

## 基本信息

| 项目 | 值 |
|---|---|
| 插件 ID | `dsh-motor-ai-l0` |
| 名称 | Motor-AI 电机设计专家 · L0 快速预筛 |
| 版本 | **0.2.2** |
| 作者 | Motor-AI |
| 许可证 | MIT |
| 层级 / 付费等级 | `l0` / `free`（L0 永久免费，L1 RMxprt、L2 Motor-CAD 为可选精算层） |
| npm | `dsh-motor-ai-l0`（registry: **https://registry.npmjs.org**，npmmirror 自动镜像） |
| GitHub | https://github.com/wangxuewangxue98/dsh-motor-ai-l0 |

## 一句话简介（listing title）

不装 RMxprt / Motor-CAD / Python / license，毫秒级把上百个电机候选压成 Top5，只把求解器时间花在最值得的方案上

## 详细描述（listing description）

Motor-AI「电机设计专家」插件的 L0 快速预筛层，面向 PMSM（永磁同步）与三相异步电机的**方案初筛**场景。

**能做到**

- 输入功率 / 转速 / 电压，毫秒级生成上百个候选参数矩阵（聚焦扫描，非笛卡尔积）
- 估算效率、温升、损耗拆分、转矩密度、齿/轭磁密、功率因数
- 12 条物理一致性规则校验（齿/轭磁密、长径比、槽配合、反电势闭环、槽满率等）
- 按效率 / 温升 / 损耗 / 转矩密度 / 功率排序输出 TopN，并给出 L1 / L2 交接载荷
- 代理模型（v2.2.0，4955 条真实 RMxprt 结果训练）作为效率排序的交叉参考

**做不到（明确的边界）**

- 不调用 RMxprt / Motor-CAD，不跑有限元，不做最终设计签发
- 几何尺寸为经验公式推算值，非最终设计值
- 效率 / 温升为**排序用估算**，绝对值须由 L1 复核（本版物理通道未标定，6 个标定案例偏差 0.35~2.96 pt）
- **高速 PMSM（>10000rpm）不适用**：轭部磁路模型外推失真，门控会拒绝输出方案（`recommended = null`）

**实测性能**

| 算例 | 候选数 | 可行数 | 端到端耗时 |
|---|---|---|---|
| 15kW / 1460rpm / 380V 异步 | 120 | 80 | 16 ms |
| 75kW / 1480rpm / 660V 异步 | 120 | 80 | 4 ms |

单候选均摊 0.01~0.04 ms。完整输出见仓库 [`examples/`](https://github.com/wangxuewangxue98/dsh-motor-ai-l0/tree/main/examples)。

**代理模型精度（v2.2.0，5 折 CV）**

| 机种 | 样本 OD 覆盖 | 样本数 | CV R² | MAE |
|---|---|---|---|---|
| 异步 small / medium / large | 150.6–756.5 / 151.3–1085.4 / 1196–2651 mm | 1291 / 2578 / 1276 | 0.652 / 0.438 / 0.670 | 1.44 / 3.29 / 4.53 pp |
| PMSM 三段 | 612–1178 mm | 367 / 745 / 355 | ≤0.31 → 自动降级公式通道 | — |

**技术规格**

- 运行环境：Node.js ≥ 22 + DSH ≥ 0.1.2
- 零外部依赖：无 Python、无 RMxprt、无 Motor-CAD、无 license
- 确定性输出：同输入必得同结果，无随机抖动
- 隐私：仅本地 `usage.jsonl` 记元数据（次数 / 耗时 / 成败），不记设计参数；遥测默认全关，需显式 opt-in

**适用场景**

- 客户询价 / 投标阶段的快速方案广筛
- 批量机座号比选（同功率档多极数对比）
- 减少 RMxprt / Motor-CAD 求解次数（120 → 5）
- 教学演示、方案评审的初步数据支撑

**层级关系**

```text
L0（本层，free）  经验公式 + 代理模型预筛，毫秒级，全量候选
      ↓ TopN + 交接载荷（无需格式转换）
L1（RMxprt，vip） 磁路法精算，1–3 s/方案
      ↓
L2（Motor-CAD，flagship） 有限元精验，~200 s/方案
```

## 分类与标签

- 一级分类：工程仿真
- 二级分类：电机设计
- 标签：`electric-motor` `PMSM` `induction-motor` `electromagnetic-design` `RMxprt` `Motor-CAD`
  `L0-pre-screen` `motor-sizing` `electric-machine-design` `电机设计` `永磁同步电机` `异步电机`

## 截图清单（市场页 / 发布帖配图）

| # | 内容 | 素材来源 |
|---|---|---|
| 1 | 参数矩阵生成结果（表格视图，含 OD / 极数 / 槽数列） | `examples/01` 第 2 节 |
| 2 | Top5 排序结果（效率降序，带 `solve_mode=l0` 标记） | `examples/01` 第 4 节表格 |
| 3 | 物理校验命中示例（V08 轭磁密 warning / 饱和拦截） | `examples/03` 第 2–3 节 |
| 4 | 反例：高速 PMSM 输出 `recommended = null`（体现门控不硬给答案） | `examples/03` |
| 5 | 耗时对比：120 候选 → 16 ms（终端截图） | `examples/01` 第 2 节 |

## 图标

- 尺寸：512×512 PNG，透明背景；另备 SVG（小尺寸渲染）
- 风格：电机截面简图 + 闪电符号（毫秒级）
- 主色：`#2563EB`（科技蓝）+ `#10B981`（效率绿）

## npm 发布检查清单（0.2.2 已全部通过）

- [x] `npm whoami --registry=https://registry.npmjs.org` 返回用户名（**注意：本机默认 registry 是 npmmirror 且 token 已失效，必须显式带 `--registry=https://registry.npmjs.org`**）
- [x] 包名未被占用
- [x] version 递增（0.2.1 → 0.2.2）
- [x] `files` 白名单含 `lib/` `tools/` `models/` `skills/` `docs/` `examples/` `scripts/`
- [x] README 含 usage example 与真实算例链接
- [x] LICENSE（MIT）
- [x] `npm publish --dry-run` 通过
- [x] 发布后 `sync_l0_plugin.py --apply --commit` 同步到 MotorDesign 镜像（管理端台账 / 门禁 G1–G4 消费）
