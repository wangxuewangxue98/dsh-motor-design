# dsh-motor-ai-l0 WorkBuddy 市场元数据

## 基本信息
- **插件 ID**：dsh-motor-ai-l0
- **名称**：Motor-AI 电机设计专家 · L0 快速预筛
- **版本**：0.1.0
- **作者**：Motor-AI
- **许可证**：MIT
- **等级**：l0（免费层）
- **付费等级**：free

## 一句话简介（listing title）
毫秒级电机设计预筛：纯 JS 经验公式，零求解器依赖，TopN 交接 RMxprt/Motor-CAD

## 详细描述（listing description）
Motor-AI「电机设计专家」插件的 L0 快速预筛层，专为 PMSM（永磁同步）与异步电机设计场景打造。

**能做到**：
- 输入功率/转速/电压/极数，毫秒级生成上百个参数组合矩阵
- 用纯 JS 经验公式估算效率、温升、损耗拆分、转矩密度
- 12 条物理一致性规则校验（齿/轭磁密、长径比、槽配合等）
- 按效率降序 TopN 排序，输出 L1/L2 精算所需交接载荷

**做不到**：
- 不调用 RMxprt / Motor-CAD，不跑有限元
- 几何尺寸为经验公式推算值，非最终设计值
- 效率/温升为估算范围，仅供排序参考

**技术规格**：
- 运行环境：Node.js >= 22.0.0 + DSH >= 0.1.2
- 零外部依赖：无 Python、无 RMxprt、无 Motor-CAD
- 确定性输出：同输入必得同结果，无随机抖动
- 回归质量门：6 案例标定集，DEBT 状态（效率偏差 0.35~2.96pt，温升越限需 L1 确认）

**适用场景**：
- 客户询价阶段的快速方案广筛
- 批量机座号比选（同一功率档位多极数对比）
- 教学演示/方案评审的初步数据支撑

**层级关系**：
- L0（本层，free）：经验公式预筛 → L1（RMxprt，vip）→ L2（Motor-CAD，flagship）
- L0 输出直接作为 L1/L2 的入参矩阵，无需格式转换

## 分类标签
- 一级分类：工程仿真
- 二级分类：电机设计
- 标签：electric-motor, PMSM, induction-motor, electromagnetic-design, RMxprt, Motor-CAD, L0-pre-screen

## 截图建议（待补充）
1. 参数矩阵生成截图（table 视图，show OD/poles/efficiency columns）
2. TopN 排序结果截图（efficiency 降序，show solve_mode=l0 badge）
3. 物理校验规则命中截图（V08 轭磁密 warning 示例）
4. L0→L1 交接载荷字段截图（param_schema 对齐示意）

## 图标建议
- 尺寸：512×512 PNG，透明背景
- 风格：电机截面简图 + 闪电符号（毫秒级）
- 主色：#2563EB（科技蓝）+ #10B981（效率绿）
- 备选：SVG vector 版本（用于小尺寸渲染）

## npm registry 元数据
- **包名**：dsh-motor-ai-l0
- **作用域**：建议 `@motor-ai/l0`（私有 registry）或裸名 `dsh-motor-ai-l0`（公开 registry）
- **当前 registry**：https://registry.npmmirror.com（镜像，非官方）
- **publish 前检查清单**：
  - [ ] npm token 已配置（`npm whoami` 返回用户名）
  - [ ] package.json name 未在目标 registry 上被占用
  - [ ] version 递增（当前 0.1.0）
  - [ ] files 白名单含 skills/ models/（已补）
  - [ ] README 含 usage example
  - [ ] LICENSE 文件存在（MIT）
