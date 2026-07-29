# Motor-AI 后端系统软件说明书

**版本**: v2.05.01-dev
**日期**: 2026-07-29
**适用环境**: DEV (开发环境) / TRIAL (试用环境)

---

## 目录

1. [系统概述](#1-系统概述)
2. [系统架构](#2-系统架构)
3. [环境分离机制](#3-环境分离机制)
4. [核心引擎](#4-核心引擎)
5. [智能体体系](#5-智能体体系)
6. [API 接口参考](#6-api-接口参考)
7. [前端面板](#7-前端面板)
8. [配置参数](#8-配置参数)
9. [数据库设计](#9-数据库设计)
10. [部署与运行](#10-部署与运行)
11. [故障排查](#11-故障排查)

---

## 1. 系统概述

### 1.1 定位

Motor-AI 是面向电机设计工程师的 AI 辅助设计系统，以"一人电机设计公司"为理念，将需求解析、方案生成、辩证评审、蒙特卡洛沙盘推演、报告生成等环节自动化，由多个 AI 智能体协同完成电机选型与设计优化。

### 1.2 核心能力

| 能力 | 说明 |
|------|------|
| **D²L 物理内核** | 基于电磁转矩 T ∝ D²L 的物理定律，通过 Y 系列型谱查表 + 类比推算，替代暴力网格扫描 |
| **辩证制衡双轨** | 放大轨（评审推荐师）与收敛轨（四剑客）并行，汇聚于决策门，防止 AI 过度自信 |
| **蒙特卡洛沙盘** | 1000 次推演，5 种失效模式（热失控/磁饱和/铜耗超标/退磁/效率崩塌） |
| **运营数据飞轮** | 双环飞轮：技术飞轮（采纳/拒绝反馈）+ 运营飞轮（效率/成本/质量自进化） |
| **跨设备同步** | ProgressHub SSE 广播，手机操作电脑实时可见 |
| **全本地零上云** | 本地 Ollama Qwen2.5-3B 推理，数据不出机，云端仅弹性补充 |
| **Motor-CAD 自动化** | COM 自动化连接复用，电磁+热分析全自动，支持模拟/手动/自动三模式降级 |

### 1.3 版本信息

| 项目 | DEV 环境 | TRIAL 环境 |
|------|---------|-----------|
| 版本号 | 2.05.01-dev | 2.02.03-trial |
| 端口 | 5000 | 5001 |
| 调试模式 | 开启（热重载） | 关闭 |
| 云端 API | 条件允许 | 强制关闭 |
| 绑定地址 | 127.0.0.1（仅本机） | 0.0.0.0（所有网卡） |

---

## 2. 系统架构

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────────┐
│                    Motor-AI 后端系统                          │
│                   (Flask + SQLite + Ollama)                   │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐ │
│  │  前端面板    │  │  Flask API    │  │   核心服务层        │ │
│  │ (index.html) │←→│ (81 个路由)   │←→│                    │ │
│  │  12 个面板   │  │  SSE 流式     │  │  · AI 服务          │ │
│  └─────────────┘  └──────────────┘  │  · Motor-CAD 工具   │ │
│                                     │  · 数据库工具        │ │
│  ┌─────────────────────────────┐   │  · 任务管理器        │ │
│  │      智能体编排层             │   │  · 进度广播          │ │
│  │  (LangGraph 辩证制衡双轨)     │   │  · 认证/积分         │ │
│  │                              │   │  · 审计日志          │ │
│  │  需求分析师 → 方案生成师      │   │  · 进化引擎          │ │
│  │       ↓              ↓       │   │  · 数据飞轮          │ │
│  │  评审推荐师    收敛四剑客     │   │  · 运营飞轮          │ │
│  │       ↓              ↓       │   └────────────────────┘ │
│  │       决策门 → 报告撰写员     │                           │
│  └─────────────────────────────┘   ┌────────────────────┐ │
│                                     │   物理内核层        │ │
│  ┌─────────────────────────────┐   │  (D²L 类比选型)     │ │
│  │      蒙特卡洛沙盘             │   │  · Y 系列型谱库     │ │
│  │  (1000次 × 5种失效模式)      │   │  · 扭矩/型谱/类比   │ │
│  └─────────────────────────────┘   │  · 条件修正/跃迁    │ │
│                                     └────────────────────┘ │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              外部依赖                                  │   │
│  │  Ollama (本地LLM)  ·  DeepSeek (云端API)             │   │
│  │  Motor-CAD v11.1.5 (COM自动化)  ·  SQLite (WAL)      │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| Web 框架 | Flask | Python 轻量级 Web 框架 |
| 前端 | 原生 HTML/CSS/JS | 单页面应用，无前端框架依赖 |
| 数据库 | SQLite (WAL) | 轻量级嵌入式数据库 |
| 本地 AI | Ollama + Qwen2.5-3B | 本地大语言模型推理 |
| 云端 AI | DeepSeek API | OpenAI 兼容接口 |
| 仿真引擎 | Motor-CAD v11.1.5 | COM 自动化 (win32com) |
| 工作流编排 | LangGraph | 智能体图编排框架 |
| 实时通信 | SSE | Server-Sent Events 流式推送 |

### 2.3 模块加载容错

系统采用带容错的模块导入设计，每个模块有独立的加载状态标志：

| 标志 | 模块 | 失败影响 |
|------|------|---------|
| `AI_OK` | ai_tools | AI 解析/评审/对话不可用 |
| `MOTOR_OK` | motor_tools | Motor-CAD 求解不可用 |
| `DB_OK` | db_tools | 数据持久化不可用 |
| `AUTH_OK` | auth | 用户认证不可用 |
| `POINTS_OK` | points_service | 积分体系不可用 |
| `AGENT_OK` | orchestrator | 多智能体工作流不可用 |
| `AUDIT_OK` | audit_logger | 审计日志不可用 |
| `EVOLUTION_OK` | evolution_engine | 进化引擎不可用 |
| `FLYWHEEL_OK` | flywheel | 数据飞轮不可用 |
| `PROGRESS_HUB_OK` | progress_hub | 跨设备进度同步不可用 |
| `TASK_MANAGER_OK` | task_manager | 任务队列不可用 |

任何模块加载失败不会导致整个应用崩溃，对应 API 会返回错误提示。

---

## 3. 环境分离机制

### 3.1 双轨架构

系统通过 `MOTOR_ENV` 环境变量实现 dev/trial 双轨分离：

```bash
# 开发环境（默认）
set MOTOR_ENV=dev
python app/app.py

# 试用环境
set MOTOR_ENV=trial
python app/app.py
```

### 3.2 配置差异对照

| 参数 | DEV | TRIAL |
|------|-----|-------|
| `SYSTEM_VERSION` | 2.05.01 | 2.02.03 |
| `VERSION_LABEL` | 2.05.01-dev | 2.02.03-trial |
| `FLASK_PORT` | 5000 | 5001 |
| `FLASK_DEBUG` | True | False |
| `BIND_HOST` | 127.0.0.1 | 0.0.0.0 |
| `DB_PATH` | designs_dev.db | designs_trial.db |
| `ALLOW_CLOUD_API` | 条件允许 | 强制关闭 |
| 环境标签 | DEV (橙色 #f59e0b) | BETA (蓝色 #3b82f6) |
| 环境描述 | 调试版 | 发布试用版 |

### 3.3 配置文件优先级

```
.env.{MOTOR_ENV}  >  .env  >  默认值
```

- `.env.dev` — 开发环境配置
- `.env.trial` — 试用环境配置
- `.env` — 通用配置（向后兼容）

---

## 4. 核心引擎

### 4.1 D²L 物理内核 (v2.05.01 新增)

**文件**: `Scripts/physics_kernel.py` (850 行)

基于电磁转矩物理定律 T = k · B_δ · A · D²L，在同系列电机中 B_δ 和 A 为常数，因此 T ∝ D²L（D=定子内径，L=铁心长度）。

#### 核心函数

| 函数 | 功能 |
|------|------|
| `compute_torque(power_kw, speed_rpm)` | 扭矩计算 T = 9550·P/n |
| `compute_d2l(d_mm, l_mm)` | D²L 计算 = (D/1000)²·(L/1000) |
| `find_closest_catalog(torque_nm, poles)` | Y 系列型谱查表（按转矩+极数匹配） |
| `analogy_estimate(torque_target, ref_entry)` | 类比推算 K_V=T_target/T_0, D'=D_0·K_V^(1/3) |
| `apply_condition_factors(specs)` | 三层条件修正（直接/间接/微调） |
| `shift_frame(torque_ratio_deviation)` | 机座号跃迁判定（micro<5%/fine 5-20%/coarse>20%） |
| `determine_scenario(specs)` | 场景识别（catalog 型谱模式 / analogy 类比模式） |
| `focused_scan(specs, count)` | 物理内核聚焦扫描（±10-15%，替代暴力网格扫描） |
| `build_selection_narrative(specs, scenario, design)` | 生成选型路径叙述 |

#### Y 系列型谱数据库

**文件**: `Knowledge/y_series_physics.json` (82KB, 65 型号)

覆盖 Y 系列三相异步电动机，机座号 80-280，2/4/6/8 极，0.55-90kW。

每条记录包含：型号、功率、转速、扭矩、极数、同步转速、机座号、冷却方式、防护等级、定子外径/内径、铁心长度、气隙、长径比 λ、D²L 值、定子/转子槽数、轭厚、效率等级、绝缘等级。

#### 三层条件修正

| 层级 | 影响 | 示例 |
|------|------|------|
| **直接影响层** | 无法使用 Y 系列类比 | 水冷、400Hz、环境>70°C、海拔>4000m |
| **间接修正层** | 系数修正 | S3 断续、IP65、过载、海拔 2000m、60Hz |
| **微调层** | <5% 忽略 | 微小偏差 |

#### 机座号跃迁判定

| 类型 | 扭矩偏差 | 机座号变化 |
|------|---------|-----------|
| micro | <5% | 不变 |
| fine | 5-20% | ±1-2 号 |
| coarse | >20% | +2-3 号 |

### 4.2 辩证制衡双轨

**文件**: `Agents/orchestrator.py` (689 行)

采用 LangGraph 图编排，实现"放大-收敛"辩证制衡：

```
需求分析师 → 方案生成师 → ┌─ 评审推荐师（放大轨）─┐ → 决策门 → 报告撰写员
                          └─ 收敛四剑客（收敛轨）─┘
```

#### 工作流六步骤

| 步骤 | 节点 | 角色 | 说明 |
|------|------|------|------|
| 1 | parse_requirement | 需求分析师 | 三层分层提取 + 场景识别 |
| 2 | generate_designs | 方案生成师 | D²L 聚焦扫描生成 5-15 个候选方案 |
| 3a | review_solutions | 评审推荐师 | 放大：AI 点评 + 物理核验 + 分层对比 |
| 3b | convergence_review | 收敛四剑客 | 收敛：逆向/现实/沙盘/魔鬼并行 |
| 4 | decision_gate | 决策门 | 双报告必读，判定是否通过 |
| 5 | generate_report | 报告撰写员 | 生成最终设计报告 |

#### 收敛四剑客

| 智能体 | 文件 | 职责 |
|--------|------|------|
| 逆向评审师 | reverse_reviewer.py | 从反面论证方案的缺陷 |
| 现实检验师 | reality_checker.py | 检验方案在工程现实中的可行性 |
| 沙盘推演师 | sandbox_simulator.py | 蒙特卡洛模拟失效场景 |
| 魔鬼代言人 | devils_advocate.py | 极端质疑，寻找致命缺陷 |

### 4.3 蒙特卡洛沙盘

**文件**: `Core/monte_carlo.py` (8.2KB)

对每个方案进行 1000 次随机推演，检测 5 种失效模式：

| 失效模式 | 说明 |
|---------|------|
| 热失控 | 绕组温度超过绝缘极限 |
| 磁饱和 | 铁心磁密超过饱和阈值 |
| 铜耗超标 | 铜损超过效率允许上限 |
| 退磁 | 永磁体工作点低于退磁曲线 |
| 效率崩塌 | 负载变化导致效率急剧下降 |

输出：失败率、分位数、最惨情形。

### 4.4 运营数据飞轮 (v2.05.01 新增)

**文件**: `Core/operation_flywheel.py` (709 行)

双环飞轮的第二环——运营飞轮，记录效率/成本/质量指标，驱动智能体自我进化。

#### 三张数据表

| 表 | 用途 |
|----|------|
| `operation_metrics` | 每步骤的耗时/成本/质量指标 |
| `project_summary` | 项目级汇总 |
| `evolution_log` | 进化日志 |

#### 三大进化机制

| 机制 | 函数 | 触发条件 |
|------|------|---------|
| 追问策略进化 | `evolve_question_strategy()` | 低质量项目分析，提取缺失参数模式 |
| 参数域进化 | `evolve_parameter_domain()` | 采纳方案参数分布 95% 置信区间 |
| AI 路由进化 | `evolve_ai_routing()` | 云端成本>3×本地且质量提升<5% → 切本地 |

#### 三阈值检测

| 检测 | 条件 | 动作 |
|------|------|------|
| 质量阈值 | 同一智能体连续 5 次质量 < 70 | 行为重训 |
| 效率阈值 | 同类项目连续 3 次效率 < 50% | 调参 |
| 决策耗时 | 决策耗时连续增长 | UI 优化 |

#### 数据飞轮（第一环）

**文件**: `Core/flywheel.py` (8.9KB)

技术飞轮：采纳反馈 → 技能模板 + 规则挖掘；拒绝反馈 → 失败特征。

---

## 5. 智能体体系

### 5.1 智能体清单

| 文件 | 智能体 | 职责 |
|------|--------|------|
| requirement_agent.py | 需求分析师 | 三层分层提取（L1粗选/L2细调/L3微调）+ 场景识别 + 物理补全 |
| design_agent.py | 设计智能体 | 设计参数生成辅助 |
| review_agent.py | 评审推荐师 | 物理逻辑解释 + 选型路径叙事 + 物理核验表 + 分层对比 |
| report_agent.py | 报告撰写员 | 生成完整设计报告 |
| orchestrator.py | 编排器 | LangGraph 图编排，辩证制衡双轨 |
| reverse_reviewer.py | 逆向评审师 | 收敛阵营：反面论证 |
| reality_checker.py | 现实检验师 | 收敛阵营：工程可行性检验 |
| sandbox_simulator.py | 沙盘推演师 | 收敛阵营：蒙特卡洛模拟 |
| devils_advocate.py | 魔鬼代言人 | 收敛阵营：极端质疑 |

### 5.2 需求分析师三层提取 (v2.05.01)

| 层级 | 参数 | 说明 |
|------|------|------|
| **L1 粗选** | motor_type, power_kw, voltage_v, speed_rpm, torque_nm, poles, cooling | 决定机座号量级 |
| **L2 细调** | duty_cycle, ed_pct, overload_pct, altitude_m, protection_class, ambient_temp_c, frequency_hz | 影响机座号±1-2 |
| **L3 微调** | mounting, insulation_class, shaft_config, bearing_type, application | 同机座号内微调 |

- `_physics_complete()`: 自动计算扭矩（T=9550·P/n）和极数（从转速推算）
- `_assess_confidence()`: 加权评分（power_kw=0.25, voltage_v=0.25, speed_rpm=0.20, torque_nm=0.15, motor_type=0.10, poles=0.05）
- 返回新增字段：`layered_specs`, `scenario_info`, `extraction_notes`

### 5.3 评审推荐师物理分析 (v2.05.01)

`_build_physics_analysis()` 生成四个分析维度：

1. **选型路径叙述** — 从需求到推荐的完整推理链
2. **物理核验表** — 每个方案的 D²L/长径比/扭矩验证
3. **分层对比** — L1/L2/L3 逐层参数对比
4. **机座号变动分析** — 跃迁类型与原因

### 5.4 BaseAgent 基类

所有智能体继承 `BaseAgent`，提供 `safe_run` 方法实现异常捕获和容错。

---

## 6. API 接口参考

系统共 81 个路由（1 页面 + 80 API），按功能模块分类如下。

### 6.1 系统状态

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/api/status` | 获取系统全状态（环境/AI/数据库/Motor-CAD/认证/积分） |
| GET | `/api/env` | 返回当前环境配置信息 |

### 6.2 需求解析与参数生成

| 方法 | 路由 | 说明 |
|------|------|------|
| POST | `/api/parse` | 本地 AI 解析客户需求，失败降级到云端 |
| POST | `/api/preview-params` | 预览参数扫描矩阵（D²L 聚焦扫描） |

### 6.3 批量求解 (SSE 流式)

| 方法 | 路由 | 说明 |
|------|------|------|
| POST | `/api/solve` | **SSE 流式**批量求解，Motor-CAD COM 连接复用/模拟降级/手动模式 |

**SSE 事件类型**: `connecting` → `start` → `progress` → `cancelled` / `done` / `fatal`

### 6.4 Motor-CAD 相关

| 方法 | 路由 | 说明 |
|------|------|------|
| POST | `/api/motorcad/import` | 导入 Motor-CAD 手动求解结果（JSON 或 CSV） |
| POST | `/api/motorcad/export-params` | 导出参数矩阵到 CSV |
| GET | `/api/motorcad/guide` | 获取 Motor-CAD 手动求解操作指南 |
| POST | `/api/motorcad/upload-csv` | 上传 CSV 文件导入求解结果 |
| POST | `/api/motorcad/download-params` | 导出参数 CSV 并返回文件下载 |

### 6.5 半自动求解

| 方法 | 路由 | 说明 |
|------|------|------|
| POST | `/api/semi-auto/solve` | 启动半自动求解工作流（参数映射到 .mot 变量名） |
| GET | `/api/semi-auto/status` | 查询半自动求解状态 |
| POST | `/api/semi-auto/cancel` | 取消半自动求解 |

### 6.6 求解结果

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/api/solve-log` | 获取最近一次求解日志和摘要 |
| GET | `/api/results` | 获取所有求解结果（按 user_id 隔离） |
| GET | `/api/results/top` | 获取 Top N 方案（支持 min_eff 过滤） |
| GET | `/api/export-csv` | 导出全部结果到 CSV |

### 6.7 AI 功能

| 方法 | 路由 | 说明 |
|------|------|------|
| POST | `/api/review` | 本地 AI 对方案做点评 |
| POST | `/api/check-desensitize` | 检查文本是否含敏感信息，返回脱敏后文本 |
| POST | `/api/cloud-analysis` | 云端 AI 深度对比分析（自动脱敏） |
| POST | `/api/ai/chat` | AI 多轮对话（支持 model/temperature 参数） |
| GET | `/api/ai/models` | 获取可用 AI 模型列表 |

### 6.8 报告

| 方法 | 路由 | 说明 |
|------|------|------|
| POST | `/api/report` | 生成完整报告（参数对比/推荐/损耗/热分析） |
| GET | `/api/reports` | 列出所有已保存的报告文件 |
| GET | `/api/report/read` | 读取指定报告内容 |

### 6.9 设置

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/api/settings` | 获取当前设置 |
| POST | `/api/settings` | 更新设置（即时生效） |

### 6.10 多智能体工作流 (SSE 流式)

| 方法 | 路由 | 说明 |
|------|------|------|
| POST | `/api/workflow/run` | **SSE 流式**启动多智能体工作流（辩证制衡双轨） |

**SSE 事件类型**: `start` → `step_start` → `progress` → `step_done` / `step_error` → `fatal` / `done`

### 6.11 AI 团队工作流结果

| 方法 | 路由 | 说明 |
|------|------|------|
| POST | `/api/ai_team/results` | 客户端上传 AI 团队工作流完整结果 |
| GET | `/api/ai_team/results` | 查询 AI 团队工作流结果列表（支持分页） |
| GET | `/api/ai_team/results/<result_id>` | 获取 AI 团队工作流结果详情 |

### 6.12 反馈与飞轮

| 方法 | 路由 | 说明 |
|------|------|------|
| POST | `/api/feedback` | 记录方案采纳/拒绝反馈（驱动数据飞轮） |
| GET | `/api/feedback/stats` | 获取反馈统计 |
| GET | `/api/feedback/recent` | 获取最近反馈记录 |
| GET | `/api/flywheel/status` | 飞轮健康度（采纳率/技能数/规则数/失败特征） |

### 6.13 进化引擎

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/api/evolution/dashboard` | 获取进化仪表盘数据 |
| POST | `/api/evolution/mine-rule` | 从历史数据挖掘新规则（需 3+ 采纳方案） |
| POST | `/api/evolution/create-skill` | 手动创建技能模板 |

### 6.14 蒙特卡洛与收敛

| 方法 | 路由 | 说明 |
|------|------|------|
| POST | `/api/sandbox/simulate` | 蒙特卡洛推演（失败率/分位/最惨情形） |
| POST | `/api/convergence/run` | 独立触发收敛阵营（4 个收敛 agent） |

### 6.15 审计日志

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/api/audit/logs` | 获取审计日志（支持 limit 参数） |
| GET | `/api/audit/stats` | 获取审计统计 |
| GET | `/api/audit/agent-metrics` | 获取智能体性能指标 |

### 6.16 进度广播 (SSE)

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/api/progress/stream` | **SSE 端点**实时接收工作流/求解进度（15 秒心跳） |
| GET | `/api/progress/status` | 获取当前进度状态快照（轮询用） |
| POST | `/api/progress/cancel` | 终止当前正在运行的工作流/求解任务 |

### 6.17 全局任务管理

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/api/tasks` | 获取所有任务列表（支持 status 过滤和 limit） |
| GET | `/api/tasks/active` | 获取活跃任务（queued + running） |
| GET | `/api/task/<task_id>` | 获取单个任务详情 |
| POST | `/api/task/<task_id>/cancel` | 取消指定任务 |
| PUT | `/api/task/<task_id>/priority` | 更新任务优先级（0-10，0 最高） |

### 6.18 网络访问

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/api/network/status` | 获取网络访问状态（IP/Tailscale/端口/绑定） |
| GET | `/api/network/qrcode` | 生成访问二维码（局域网+Tailscale），返回 base64 PNG |
| POST | `/api/network/check-access` | 外部可达性检测 |
| POST | `/api/network/config` | 更新网络绑定配置 |

### 6.19 认证/JWT

| 方法 | 路由 | 说明 |
|------|------|------|
| POST | `/api/auth/register` | 用户注册（用户名+密码+邮箱），返回 JWT |
| POST | `/api/auth/login` | 用户登录，返回 JWT |
| GET | `/api/auth/verify` | 验证 token 有效性 |
| GET | `/api/auth/profile` | 获取当前用户资料 + 会员信息 |
| PUT | `/api/auth/profile` | 更新个人资料 |
| POST | `/api/auth/logout` | 登出 |
| POST | `/api/auth/dev-restore` | dev 环境恢复自动登录 |

### 6.20 管理员

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/api/admin/users` | 获取用户列表 |
| PUT | `/api/admin/user/<user_id>` | 修改用户（角色/会员/状态/积分） |
| POST | `/api/admin/user/<user_id>/points` | 调整用户积分 |
| GET | `/api/admin/clients` | 获取所有客户端列表 |
| PUT | `/api/admin/clients/<client_id>` | 更新客户端配置 |
| GET | `/api/admin/clients/<client_id>` | 获取单个客户端配置 |

### 6.21 客户端设备

| 方法 | 路由 | 说明 |
|------|------|------|
| POST | `/api/client/register` | 客户端注册/心跳（hostname/tailscale_ip/version） |

### 6.22 产品线与项目

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/api/product-lines` | 获取产品线列表 |
| GET | `/api/product-line/<pl_id>` | 获取产品线详情 |
| POST | `/api/product-lines` | 创建产品线（admin only） |
| GET | `/api/projects` | 获取当前用户项目列表 |
| POST | `/api/projects` | 创建项目 |
| GET | `/api/project/<project_id>` | 获取项目详情 |

### 6.23 积分体系

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/api/points/balance` | 获取积分余额 |
| GET | `/api/points/history` | 获取积分变动记录 |
| POST | `/api/points/checkin` | 每日签到 |
| GET | `/api/points/membership` | 获取会员等级信息 |

### 6.24 测试用例

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/api/test-cases` | 返回 5 个内置测试用例（叉车/伺服/无人机/矿用/家电电机） |

---

## 7. 前端面板

前端为单页面应用（`app/templates/index.html`，62.5KB），使用原生 HTML/CSS/JS，无框架依赖。

### 面板清单（12 个）

| 面板 | 功能 |
|------|------|
| 需求输入 | 客户需求文本输入 + 测试用例加载 |
| 参数矩阵 | D²L 聚焦扫描方案预览 |
| 批量求解 | Motor-CAD COM 求解进度流 |
| 结果对比 | 方案性能对比表 |
| AI 评审 | 智能体点评 + 物理核验 |
| AI 对话 | 多轮对话交互 |
| 报告中心 | 报告生成与查看 |
| 蒙特卡洛 | 沙盘推演可视化 |
| 进化看板 | 飞轮/进化仪表盘 |
| 任务调度 | 全局任务队列管理 |
| 网络设置 | 局域网/Tailscale 访问配置 |
| 系统设置 | AI/Motor-CAD/系统配置 |

**DEV 专属**: UI 环境徽章（橙色 DEV 标签），任务调度面板可见。

---

## 8. 配置参数

### 8.1 AI 配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `OLLAMA_BASE_URL` | http://127.0.0.1:11434 | Ollama 服务地址 |
| `LOCAL_MODEL` | qwen2.5:3b-instruct-q4_K_M | 本地模型名称 |
| `USE_GPU` | true | 是否使用 GPU 加速 |
| `DEEPSEEK_API_KEY` | (占位符) | DeepSeek API 密钥 |
| `DEEPSEEK_BASE_URL` | https://api.deepseek.com | DeepSeek API 地址 |
| `DEEPSEEK_MODEL` | deepseek-chat | 云端模型名称 |

### 8.2 Motor-CAD 配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MOTORCAD_EXE` | D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe | 可执行文件路径 |
| `MOTORCAD_PROGID` | MotorCAD.AppAutomation | COM 组件 ProgID |
| `MOTORCAD_MODE` | auto | 模式（auto/simulation/manual） |

### 8.3 数据库配置

| 参数 | DEV | TRIAL |
|------|-----|-------|
| `DB_PATH` | KnowledgeBase/designs_dev.db | KnowledgeBase/designs_trial.db |
| WAL 模式 | 开启 | 开启 |
| busy_timeout | 5000ms | 5000ms |

---

## 9. 数据库设计

### 9.1 核心数据表

| 表名 | 用途 |
|------|------|
| `designs` | 设计方案存储 |
| `users` | 用户信息（多租户） |
| `product_lines` | 产品线 |
| `projects` | 项目管理 |
| `task_queue` | 全局任务队列 |
| `ai_team_results` | AI 团队工作流结果 |
| `operation_metrics` | 运营指标记录 |
| `project_summary` | 项目汇总 |
| `evolution_log` | 进化日志 |

### 9.2 任务状态机

```
queued → running → completed
                 → failed
                 → cancelled
```

优先级：0=最高，10=最低，默认 5。

---

## 10. 部署与运行

### 10.1 环境要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10/11 (64 位) |
| Python | 3.10+ (项目 venv) |
| Motor-CAD | v11.1.5 (32 位, 正式授权) |
| Ollama | 已安装并拉取 qwen2.5:3b-instruct-q4_K_M 模型 |
| 内存 | 16GB+ |
| 显卡 | 可选（GPU 加速本地推理） |

### 10.2 启动方式

```bash
# 开发环境
cd D:\MotorDesign
set MOTOR_ENV=dev
D:\MotorDesign\venv\Scripts\python.exe app\app.py

# 试用环境
cd D:\MotorDesign
set MOTOR_ENV=trial
D:\MotorDesign\venv\Scripts\python.exe app\app.py
```

### 10.3 访问地址

| 环境 | 地址 |
|------|------|
| DEV | http://127.0.0.1:5000 |
| TRIAL | http://localhost:5001 或 http://<本机IP>:5001 |
| 手机访问 | 同一 Wi-Fi 下访问 http://<电脑IP>:5001 |

### 10.4 Motor-CAD 配置要点

1. 菜单"控件注册"改为"单客户端设置"（id=94）
2. 电磁分析前必须调用 `ShowMagneticContext()`
3. 从零启动 COM 约 82 秒
4. Flask 不支持热重载时 COM 连接复用

### 10.5 防火墙配置

运行 `Scripts/setup_firewall.bat` 添加 python.exe 防火墙规则，允许局域网访问。

---

## 11. 故障排查

### 11.1 常见问题

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| `load_dotenv NameError` | app.py 顶部未导入 | 确保 `from dotenv import load_dotenv` 在顶部 |
| COM 连接失败 | Motor-CAD 未启动或未授权 | 先启动 Motor-CAD，检查授权状态 |
| 电磁分析报错 | 未切换电磁上下文 | 确保 `ShowMagneticContext()` 在 `DoMagneticCalculation()` 之前 |
| Ollama 不可用 | 服务未启动 | 运行 `ollama serve`，检查 11434 端口 |
| 手机无法访问 | 防火墙阻止 | 运行防火墙配置脚本，Wi-Fi 选 Public 网络 |
| start.bat 编码错误 | 非 ASCII 字符 | 纯 ASCII + CRLF 编码 |
| LangGraph 并行错误 | fan-out 返回值含 last_value 字段 | 使用 `Annotated[X, operator.add]` |

### 11.2 模块加载失败

查看 `/api/status` 返回的各模块状态标志，确认哪些模块加载失败。失败模块对应的 API 会返回错误提示，但不影响其他功能。

### 11.3 日志位置

- 应用日志：控制台输出
- 任务日志：`/api/solve-log` 查询
- 审计日志：`/api/audit/logs` 查询

---

## 附录 A: 版本历史

| 版本 | 日期 | 环境 | 关键变更 |
|------|------|------|---------|
| 2.05.01 | 2026-07-29 | dev | D²L 物理内核注入 + 运营数据飞轮 |
| 2.04.01 | 2026-07-28 | dev | Motor-CAD 授权检测 + 客户端 AI 团队 + 管理端任务管理 |
| 2.02.03 | 2026-07-28 | trial | 试用版任务调度面板 |
| 2.03.02 | 2026-07-28 | dev | 登录界面 CSS 修复 + JWT 自动附加 |
| 2.02.01 | 2026-07-28 | trial/dev | 多租户基础（认证+产品线+积分+数据隔离） |
| 1.03.03 | 2026-07-27 | trial/dev | 全局任务队列系统 |
| 1.03.01 | 2026-07-27 | dev | 环境分离双轨 |
| 1.02.01 | 2026-07-27 | - | 辩证制衡体系（双轨+四剑客+蒙特卡洛） |
| 1.01.01 | 2026-07-27 | - | 首个正式版本 |
| 1.00.01 | 2026-07-24 | - | 初始项目骨架 |

## 附录 B: 文件结构

```
D:\MotorDesign\
├── app\
│   ├── app.py                    # Flask 主应用 (81 路由)
│   ├── templates\index.html      # 前端单页面 (62.5KB)
│   └── static\
│       ├── app.js                # 前端逻辑 (121KB)
│       └── style.css             # 样式表 (43KB)
├── Agents\                       # 智能体 (11 文件)
│   ├── orchestrator.py           # 编排器 (689 行)
│   ├── requirement_agent.py      # 需求分析师 (245 行)
│   ├── review_agent.py           # 评审推荐师 (317 行)
│   ├── report_agent.py           # 报告撰写员
│   ├── reverse_reviewer.py       # 逆向评审师
│   ├── reality_checker.py        # 现实检验师
│   ├── sandbox_simulator.py      # 沙盘推演师
│   └── devils_advocate.py        # 魔鬼代言人
├── Core\                         # 核心服务 (7 文件)
│   ├── operation_flywheel.py     # 运营飞轮 (709 行)
│   ├── flywheel.py               # 数据飞轮
│   ├── monte_carlo.py            # 蒙特卡洛沙盘
│   ├── task_manager.py           # 任务管理器
│   ├── auth.py                   # JWT 认证
│   ├── points_service.py         # 积分体系
│   ├── audit_logger.py           # 审计日志
│   └── evolution_engine.py       # 进化引擎
├── Scripts\                      # 核心脚本
│   ├── physics_kernel.py         # D²L 物理内核 (850 行)
│   ├── ai_tools.py               # AI 服务封装
│   ├── motor_tools.py            # Motor-CAD 工具 (1772 行)
│   ├── db_tools.py               # 数据库工具
│   ├── gen_y_series_db.py        # Y 系列数据库生成
│   ├── progress_hub.py           # 进度广播
│   └── template_manager.py       # 模板管理
├── Knowledge\
│   └── y_series_physics.json     # Y 系列型谱库 (65 型号, 82KB)
├── config\
│   └── env.py                    # 环境配置
├── .env.dev                       # 开发环境配置
├── .env.trial                     # 试用环境配置
├── VERSION.json                   # 版本信息
└── CHANGELOG.md                   # 变更日志
```

---

*Motor-AI 后端系统 v2.05.01-dev | 2026-07-29*
