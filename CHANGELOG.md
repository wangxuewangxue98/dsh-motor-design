# Motor-AI 变更日志

所有 notable 变更记录在此文件中。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [2.05.03] - 2026-07-29 (DEV)

### Added — 人机协作 + 成本核算
- **成本核算模块** (`Core/cost_tracker.py`): `TaskCostTracker`类, 三维度计费 (AI Token: 本地0.6/1.2元, 云端3/6元; 电脑1元/h; 人工20元/h), `cost_events`表, `generate_report()`输出完整成本报告, `format_report_markdown()`格式化Markdown
- **人机协作检查点** (`Core/human_checkpoint.py`): 9种检查点类型 (design_final_select 24h / customer_service_confirm 2h / 等), 对应超时默认动作 (auto_first / auto_confirm / auto_reject / 等), `human_checkpoints`表, `create_checkpoint()`→`wait_for_decision()`→`submit_decision()`完整决策链
- **运营分析师智能体** (`Agents/operation_analyst.py`): 6维度分析 (人工耗时/AI成本/电脑耗时/历史均值对比/超时默认/决策效率), `risk_level`风险评估 (low/medium/high), `analyze_cost_report()`独立入口
- **编排器human_approval节点** (`Agents/orchestrator.py`): 在`decision_gate`和`generate_report`之间插入阻塞节点, 调用`hc.wait_for_decision()`等待人工决策, 各节点集成`cost_tracker`成本记录, SSE新增`human_input_required`事件
- **5个Flask API端点** (`app/app.py`): `POST /api/workflow/human_input` (提交决策), `GET /api/human/checkpoints` (待处理列表), `GET /api/human/checkpoint/<id>` (详情), `GET /api/cost/report/<pid>` (成本报告), `GET /api/cost/analyze/<pid>` (运营分析)

### Added — 客户端人机协作交换区
- **ai_client.py**: 新增`submit_human_decision()`方法, 调用`POST /api/workflow/human_input`提交人工决策
- **ai_team_panel.py 交换区**: 在步骤卡片区和日志区之间插入人机协作交换区卡片, 展示检查点标题/描述/上下文/成本统计/超时默认提示, 超时倒计时 (QTimer每秒刷新), 批准/拒绝/覆盖三按钮+备注输入框
- **ai_team_panel.py STEPS**: 新增`human_approval`步骤 (与decision_gate共编号4), 步骤4改为双卡片容器
- **ai_team_panel.py 事件处理**: `_handle_human_input_required()`展示交换区, `_submit_decision()`后台提交, `_on_decision_submitted()`回调, `_hide_exchange()`清理

### Fixed
- `Core/cost_tracker.py`: `format_report_markdown()`函数缩进错误修复 (8空格→4空格)

---

## [2.05.02] - 2026-07-29 (DEV)

### Added — 新增进化机制
- **评审推荐师报告结构优化** (`evolve_report_structure()`): 分析决策耗时>30分钟→关键指标前置+可视化图表标记+段落字数限制; 分析魔鬼代言人质量<60→提示词增强(要求量化风险/失效模式/量产风险)
- **决策界面信息优先级** (`evolve_decision_ui()`): 参数被修改≥3次→提升到界面顶部; 参数被直接通过≥5次→自动折叠+默认值
- **UI交互记录** (`record_ui_interaction()`): 记录参数修改/通过行为, 新增`ui_interactions`表
- **报告阅读记录** (`record_report_metric()`): 记录章节停留时间/滚动次数, 新增`report_metrics`表
- **5个Flask API端点**: `/api/operation/dashboard`, `/api/operation/evolution-log`, `/api/operation/ui-feedback`, `/api/operation/report-feedback`, `/api/operation/trigger-evolution`

### Changed — 闭环集成 (飞轮规则→智能体)
- `requirement_agent.py`: 加载`forced_questions.json`, `_apply_forced_questions()`检测用户描述中的关键词并自动标记需追问的参数
- `motor_tools.py`: 加载`efficient_param_domain.json`, `_get_flywheel_design_count()`根据超时降量规则调整方案数量, `_filter_by_flywheel_exclusions()`过滤低效参数区间
- `orchestrator.py`: `_apply_routing_rules()`根据飞轮AI路由规则自动切换本地/云端; 收敛四剑客(逆向/现实/沙盘/魔鬼)各agent独立记录运营指标
- `review_agent.py`: 加载`report_structure_rules.json`, 启用关键指标前置时在报告最前面输出参数速览表
- `devils_advocate.py`: 加载飞轮提示词增强, `_get_devils_prompt_boost()`注入量化风险分析要求

### Enhanced — 现有进化机制增强
- `evolve_question_strategy()`: 扩展领域关键词(重载/起动/破碎机/搅拌机/中频等), 新增`_param_alias_match()`中文别名匹配
- `evolve_parameter_domain()`: 新增低效区间剔除(质量<40的方案参数值标记排除), 新增超时降量(生成耗时>2分钟的项目类型方案数缩减到70%)
- `evolve_ai_routing()`: 新增连续5次阈值判定(`ROUTING_CONSECUTIVE_TRIGGER`), 避免偶发波动误判
- `run_all_evolution()`: 从6项进化扩展到8项(新增report_structure + decision_ui)
- `get_operation_dashboard()`: 新增UI交互统计/进化日志/报告结构规则/UI优先级规则

---

## [2.05.01] - 2026-07-29 (DEV)

### D²L物理内核注入 — 分层类比选型法 + 运营数据飞轮

将"分层类比选型法"完整编码到 Motor-AI 的三个核心智能体中，使系统不再是黑箱参数扫描器，而是一个懂得"为何选这个机座号"的物理知识引擎。

#### Added — 物理内核基础
- `Knowledge/y_series_physics.json` (82KB, 65型号): Y系列三相异步电动机型谱物理数据库，覆盖机座号80-280、2/4/6/8极、0.55-90kW，每条记录含扭矩/长径比/D²L/定子内外径/铁心长/气隙/槽配合/效率/绝缘等物理量
- `Scripts/gen_y_series_db.py` (214行): Y系列数据库生成脚本，含65条RAW_DATA + 扭矩(T=9550·P/n)/长径比(λ=L/D)/D²L=((D/1000)²·(L/1000))自动计算 + 槽配合映射
- `Scripts/physics_kernel.py` (850行): D²L物理内核模块，14+核心函数：
  - `compute_torque()` / `compute_d2l()`: 基础物理量计算
  - `find_closest_catalog()`: 按扭矩(极数过滤)查找最近Y系列型号
  - `analogy_estimate()`: 类比推算 K_V=T_target/T_0; D'=D_0·K_V^(1/3); L'=L_0·K_V^(1/3)
  - `apply_condition_factors()`: 三层条件修正 (直接影响层→阻断/间接修正层→系数/微调层→忽略)
  - `shift_frame()`: 机座号跃迁判定 (微调<5%/细调5-20%±1-2/粗选>20%+2-3)
  - `determine_scenario()`: 场景识别 (型谱模式 vs 类比模式)
  - `focused_scan()`: 物理内核聚焦扫描 (±10-15%范围, 替代暴力网格扫描)
  - `build_selection_narrative()`: 选型路径叙事生成

#### Added — 运营数据飞轮
- `Core/operation_flywheel.py` (709行): 运营数据飞轮引擎，双环飞轮的第二环
  - SQLite三表: `operation_metrics`(步骤级) / `project_summary`(项目级) / `evolution_log`(进化日志)
  - 三大进化机制: `evolve_question_strategy()`(追问策略) / `evolve_parameter_domain()`(参数域) / `evolve_ai_routing()`(AI路由)
  - 三阈值检测: 质量(连续5次<70→行为重训) / 效率(连续3次<50%→调参) / 决策耗时(连续增长→UI优化)
  - `get_operation_dashboard()`: 前端仪表盘数据聚合

#### Changed — 智能体改造
- `Agents/requirement_agent.py` (97→245行): 完全重写
  - 三层分层提取: L1粗选(功率/电压/转速/扭矩/极数/冷却) + L2细调(工作制/过载/海拔/防护) + L3微调(安装/绝缘/轴承)
  - 场景识别: 调用`determine_scenario()`判定型谱模式或类比模式
  - 物理补全: 缺失扭矩时自动T=9550·P/n, 缺失极数时从转速推算
  - 置信度评估: 加权评分(功率0.25/电压0.25/转速0.20/扭矩0.15/类型0.10/极数0.05)
- `Scripts/motor_tools.py`: `generate_param_matrix()` 替换为物理内核聚焦扫描
  - 调用`focused_scan()`生成5-15个物理聚焦方案(替代原30-50个暴力网格扫描)
  - 每个方案含`_physics`字段: d_squared_l/lambda/estimated_torque/ref_model/scenario
  - 异常时自动回退到`_generate_param_matrix_legacy()`(旧暴力扫描保留)
- `Agents/review_agent.py` (166→317行): 新增物理逻辑解释
  - `_build_physics_analysis()`: 选型路径叙事 + 物理核验表 + 分层对比(L1/L2/L3) + 机座号跃迁分析
  - LLM提示词增加物理核验要求，要求每个方案附"物理核验"段落
  - 返回结果新增`physics_analysis`字段(独立于LLM评审)
- `Agents/orchestrator.py`: LangGraph工作流集成
  - `WorkflowState`扩展: `scenario_info: dict` + `layered_specs: dict`
  - 三节点(`_node_parse_requirement`/`_node_generate_designs`/`_node_generate_report`)集成运营指标记录
  - 工作流末尾异步触发`run_all_evolution()`(非阻塞, 异常静默)

#### Fixed
- `physics_kernel.py`: 同步转速公式 `60*50//poles` → `120*50/poles` (原公式混淆极数与极对数, 4极误算750rpm应为1500rpm)

#### Architecture
- 新增4个文件, 修改4个文件, 总计 ~7,700 行代码
- 物理内核作为独立模块, 所有智能体通过import调用, 不改动现有架构
- 旧暴力扫描保留为legacy回退, 确保向后兼容

---

## [2.04.01] - 2026-07-28 (DEV)

### Motor-CAD授权错误检测 + 客户端AI团队工作流 + 管理端任务管理 + 端口自动探测

#### Added
- `motor_tools.py`: `_detect_licence_error()` 新增 `activex_not_licensed` 类别，识别 "Dongle not licensed for Motor-CAD ActiveX Technology" 错误
- `motor_tools.py`: 电磁分析后检测全零结果（授权失败的典型特征），抛出异常而非静默返回零值
- `orchestrator.py`: 批量求解后检查 `_last_summary["licence_error"]`，通过 SSE 推送 `level=error` 事件
- `task_manager.py`: 新增 `update_task_priority(task_id, priority)` 函数
- `app.py`: 新增 `PUT /api/task/<task_id>/priority` 端点
- 客户端 `ai_team_panel.py`: 多智能体协同工作流面板（需求输入/方案数量/6步骤状态卡片/实时日志/SSE流式）
- 客户端 `ai_client.py`: `start_workflow()` SSE 流式 + `cancel_workflow()` + 端口自动探测(5000/5001) + 异步 `register_client()`
- 管理端 `client_panel.py`: 运行中任务区域（任务ID/类型/状态/步骤/优先级/停止按钮/排序按钮）
- 管理端 `api_client.py`: `get_active_tasks()` 方法 + 在线检测 `_is_backend_online()`

#### Fixed
- `motor_tools.py`: "Dongle not licensed for ActiveX" 错误被错误归类为 `dongle_busy`（可重试），现在正确识别为 `activex_not_licensed`（永久不可重试）
- `orchestrator.py`: Motor-CAD 授权失败时工作流静默降级为模拟模式，客户端和管理端无任何错误提示
- 客户端 `ai_client.py`: `register_client()` 中 `hostname` 字段误传 `client_id` 而非机器名
- 客户端 `ai_client.py`: `register_client()` 在主线程同步调用，后端不在线时阻塞 UI 10 秒
- 客户端 `config.py`: 默认 `api_port` 为 5001（trial），但用户运行 dev 后端（5000），导致连接拒绝
- 管理端 `config.py`: `license_broker` 服务指向不存在的文件，进程崩溃→自动重启→无限循环
- 管理端 `main_window.py`: `_check_motorcad()` 调用 `EnsureDispatch` 实际启动 Motor-CAD GUI（80+秒），状态一直显示"检测中"
- 管理端 `api_client.py`: `cancel_task`/`reorder_task` URL 路径错误（`/api/tasks/` 应为 `/api/task/`）
- 管理端 `task_panel`/`client_panel`/`user_panel`: QTimer 在主线程同步调用 API，后端不在线时阻塞 UI

#### Changed
- 客户端 `config.py`: 默认 `api_port` 5001→5000；`manager_base_url` 改为 `@property` 动态获取
- 管理端 `task_panel`/`client_panel`/`user_panel`: 定时刷新改为后台线程+Signal异步模式，UI 零阻塞
- 管理端 `api_client.py`: 所有 API 调用前先 `_is_backend_online()` 检测（socket 0.5s+5s缓存），离线时立即返回空

---

## [2.02.03] - 2026-07-28 (TRIAL)

### 试用版任务调度面板开放

#### Added
- 解除 trial 环境下「任务调度」导航入口的隐藏限制，任务调度面板在 dev + trial 双环境均可见
- 任务调度面板支持：任务状态筛选（全部/运行中/排队中/已完成/已取消）、实时进度条、2 秒自动刷新、终止任务按钮

#### Changed
- `app/static/app.js`：移除 `navTasks.style.display = isDev ? '' : 'none'` 的 trial 隐藏逻辑
- `app/templates/index.html`：更新任务调度面板注释，去掉"仅调试版"限定

#### Security
- 保留 trial 环境原有安全限制：禁用 Flask Debug 模式、禁用云端 API、Motor-CAD 默认模拟模式
- 任务取消 API 仍通过 ProgressHub 广播终止信号，确保运行中任务可优雅停止

---

## [2.03.02] - 2026-07-28 (DEV)

### 修复登录界面 + fetch 自动认证

#### Fixed
- 修复登录/注册模态框输入框不可见：原内联样式使用了未定义的 CSS 变量（`--border-color`、`--bg-secondary`、`--text-primary`、`--accent`）
- 在 `:root` 中补充兼容性别名，并将登录/注册输入框统一为 `.input` 类
- 修复 trial 登录成功后用户状态不更新：前端未将 JWT token 附加到后续 API 请求
- 新增全局 `fetch` 拦截，自动从 `localStorage` 读取 `motorai_token` 并附加 `Authorization: Bearer <token>`

---

## [2.02.02] - 2026-07-28 (TRIAL)

### 试用版登录界面紧急修复

#### Fixed
- 修复 trial 环境登录模态框输入框不可见的问题
- 修复 trial 登录后 token 未随请求发送，导致侧边栏用户卡不显示的问题

---

## [2.03.01] - 2026-07-28 (DEV)

### 调试版继续开发 — 后续修改调试仅限 dev 环境

#### Changed
- 版本号从 v2.02.01-dev 升级为 v2.03.01-dev
- v2.02.01 代码冻结为试用版 (v2.02.01-trial)

#### Notes
- **重要规则**: 后续修改调试仅对调试版 (dev) 进行，如需对试用版 (trial) 修改调试，须经用户确认

---

## [2.02.01] - 2026-07-28 (TRIAL)

### 试用版发布 — 基于 v2.02.01-dev 冻结，含多租户基础

#### Added
- 试用版版本号从 v1.03.03 升级为 v2.02.01
- 含多租户基础功能：JWT 认证 + 产品线配置 + 积分体系 + 数据隔离

#### Security
- 试用版安全限制保持不变：禁用 Debug 模式、禁用云端 API、Motor-CAD 模拟模式

---

## [2.02.01] - 2026-07-28 (DEV)

### 多租户基础 — 认证系统 + 产品线配置 + 积分体系 + 数据隔离

#### Added
- **Core/auth.py**: JWT 认证模块
  - 密码哈希 (werkzeug.security pbkdf2:sha256)
  - JWT token 生成/验证 (PyJWT, 7天有效期)
  - 角色分级: guest/user/vip/flagship/admin
  - 会员等级: trial/standard/vip/flagship
  - @require_auth / @require_role / @require_admin 装饰器
  - dev 环境自动创建 admin 用户并自动登录
- **Core/points_service.py**: 积分体系
  - 每日签到 (+10), 积分获取/消耗记录
  - 签到防重 (每日仅一次)
  - 积分余额查询/变动历史
- **数据库升级**: users 表扩展 8 个字段 (password_hash/email/membership_level/points_balance/status/last_login/expires_at/phone)
- **新建表**: product_lines (4条默认产品线 A/B/C/D) + projects (关联用户与产品线)
- **designs 表**: 增加 user_id 列, save_result/get_all_results/get_stats 支持 user_id 过滤
- **11 个新 API 端点**:
  - 认证: /api/auth/register, /api/auth/login, /api/auth/verify, /api/auth/profile (GET+PUT), /api/auth/logout
  - 管理: /api/admin/users, /api/admin/user/{id} (PUT), /api/admin/user/{id}/points (POST)
  - 产品线: /api/product-lines (GET+POST), /api/product-line/{id}
  - 项目: /api/projects (GET+POST), /api/project/{id}
  - 积分: /api/points/balance, /api/points/history, /api/points/checkin, /api/points/membership
- **数据隔离**: /api/results, /api/results/top 按 user_id 过滤 (admin 可看全部)
- **前端**: 用户信息卡 (头像/名称/角色/积分/登出) + 登录注册模态框
- **前端**: 我的积分面板 (余额/等级/签到/历史记录)
- **前端**: 用户管理面板 (admin only, 角色修改/积分调整)
- **前端**: 角色分级导航 (用户管理面板仅 admin 可见)

---

## [2.01.01] - 2026-07-28 (DEV)

### 新主版本起点 — 版本号按环境分离

#### Added
- 版本号按环境分离：dev 使用 2.x 系列，trial 冻结 1.x 系列
- config/env.py：SYSTEM_VERSION 按 IS_DEV 返回不同版本号（dev=2.01.01, trial=1.03.03）
- index.html 版本号改为动态显示：通过 JS 从 /api/status 加载，不再硬编码
- app.js loadStatus() 动态填充副标题、版本信息、仪表盘描述、页面标题
- 仪表盘快捷操作增加「AI 团队」入口
- 左侧导航栏「AI 团队」移到「需求解析」前面
- 旧数据库 designs.db 270 条记录迁移到 designs_dev.db

#### Changed
- 试用版后缀从 `-beta.X` 改为 `-trial`
- VERSION.json version_suffix.trial: `"-trial"`
- start_dev.bat / start_trial.bat 版本号更新
- .env.dev 版本升级到 2.01.01，.env.trial 保持 1.03.03 但后缀改为 -trial

#### Fixed
- `load_dotenv` 未在 app.py 导入导致保存设置返回 HTML 错误页（NameError）
- QR 码生成失败：服务器需用项目 venv Python 启动（托管 Python 缺 qrcode/Pillow）

---

## [1.03.03] - 2026-07-28 (TRIAL)

### 试用版发布 — 基于 v1.03.03-dev 冻结

#### Added
- 从 v1.03.03-dev 冻结为试用版，标签改为 `1.03.03-trial`（原 `-beta.1`）

#### Security
- 试用版安全限制保持不变：禁用 Debug 模式、禁用云端 API、Motor-CAD 模拟模式

---

## [1.03.03] - 2026-07-27

### 全局任务队列系统 + 调试版UI标识

#### Added
- 全局任务队列系统：task_queue 表持久化所有耗时任务的生命周期
- Core/task_manager.py：任务管理器模块，实现创建/更新/查询/取消
- SQLite WAL 模式：减少并发写入锁冲突
- 4个任务管理API：/api/tasks、/api/tasks/active、/api/task/{id}、/api/task/{id}/cancel
- /api/solve 和 /api/workflow/run 集成任务追踪：自动创建/更新/完成任务记录
- 任务调度面板：仅调试版可见，展示任务卡片+进度条+终止按钮
- 2秒自动轮询：活跃任务存在时自动刷新任务列表
- 调试版UI标识：标题DEV脉冲徽章 + 顶部渐变光条
- 任务状态筛选：全部/运行中/排队中/已完成/已取消

#### Changed
- db_tools.py：init_db() 启用 WAL 模式 + 新增 task_queue 表 + 索引
- app.py：导入 task_manager，SSE 流中集成任务创建/更新/完成
- index.html：新增任务调度面板 + 调试版标识 + 版本号更新
- app.js：任务列表渲染 + loadTasks/filterTasks/cancelTask 函数
- style.css：任务卡片样式 + DEV脉冲动画 + 调试版顶部光条

#### Security
- 任务取消API同时触发 ProgressHub 取消信号，确保运行中的任务被终止
- 任务调度面板仅调试版(dev)可见，试用版隐藏

---

## [1.03.02] - 2026-07-27

### 跨设备进度同步 + 任务终止机制

### Added
- **ProgressHub 全局进度广播**: 新增 `Scripts/progress_hub.py`，线程安全的进度广播器，支持多客户端订阅
- **跨设备实时同步**: 手机端发起工作流/求解时，电脑端自动显示远程会话横幅 + 实时进度镜像
- **`/api/progress/stream` (SSE)**: 任何客户端可订阅的实时事件流，含心跳保活
- **`/api/progress/status`**: 轮询端点，返回当前任务状态快照（active/step/progress/messages）
- **`/api/progress/cancel`**: 终止当前正在运行的工作流/求解任务
- **远程会话横幅**: 页面顶部显示当前远程会话状态（类型/步骤/进度/耗时），含终止按钮
- **任务终止机制**: 批量求解循环和编排器主循环均检查取消标志，收到信号后优雅停止
- **前端 EventSource 连接**: 页面加载自动连接进度广播，断线5秒后自动重连

### Changed
- `app.py` 的 `/api/solve` 和 `/api/workflow/run` SSE 流通过 `_publish_stream()` 包装器发布事件到 ProgressHub
- `orchestrator.py` 的 `run_streaming` 主循环增加取消标志检查
- `app.js` 新增 `_myWorkflowActive` 标志，避免本地事件与远程事件重复处理

---

## [1.03.01] - 2026-07-27

### 环境分离双轨 — 调试版与发布试用版并行

### Added
- **环境分离机制**: 支持 `dev`（调试版）和 `trial`（发布试用版）双环境，由 `MOTOR_ENV` 环境变量控制
- **config/env.py 配置模块**: 集中管理环境配置，提供 `Config` 类统一暴露所有配置项
- **环境级配置文件**: `.env.dev`（开发环境）和 `.env.trial`（试用环境），端口/数据库/云端API各自隔离
- **启动脚本分离**: `start_dev.bat`（port 5000, debug 模式）和 `start_trial.bat`（port 5001, 生产模式）
- **数据库按环境隔离**: `designs_dev.db` / `designs_trial.db` 互不影响，自动创建
- **前端环境标识标签**: 侧边栏底部显示 `DEV`（橙色）或 `BETA`（蓝色）标签，tooltip 显示详细信息
- **`/api/env` 端点**: 返回当前环境配置摘要，供前端和运维查询
- **多租户预留**: `g.user` 请求上下文（当前 `None`）、`users`/`points` 空表结构、`CLIENT_SECRET` 字段
- **试用版安全限制**: 强制关闭 Flask Debug 模式、强制禁用云端 API 调用

### Changed
- 配置系统从单一 `.env` 升级为多环境配置文件（`.env.dev` / `.env.trial`），向后兼容旧 `.env`
- 版本号规则：调试版带 `-dev` 后缀，试用版带 `-beta.X` 后缀
- `ai_tools.py` / `review_agent.py` / `report_agent.py` 云端门控增加 `ALLOW_CLOUD_API` 检查
- `app.py` 启动逻辑改用 `Config` 类获取端口、绑定地址、Debug 开关
- `db_tools.py` 数据库路径从 `Config` 获取，`init_db()` 预建 `users`/`points` 表

### Security
- 试用版禁用 Flask 热重载（`FLASK_DEBUG=false`），避免错误页面暴露代码
- 试用版限制云端 API 调用权限（`ALLOW_CLOUD_API=false`），防止客户原始数据外泄
- 试用版默认模拟模式（`MOTORCAD_USE_MODE=simulation`），避免 COM 冲突

### Deprecated
- 旧版单一 `start_ui.bat` 启动方式，现按环境分脚本启动（旧脚本仍可用，默认 dev 环境）

---

## [1.02.01] - 2026-07-27

### 辩证制衡体系 — 放大火花，举起镜子

### Added
- **辩证双轨编排器** (orchestrator.py 重写): 放大轨(评审推荐师→支持报告)与收敛轨(四剑客→反对报告)并行 fan-out，汇聚于决策门 (decision_gate)，须双报告齐备方可放行
- **收敛四剑客** (Agents/ 新增4模块):
  - **逆向评审师** (reverse_reviewer.py): 在被淘汰方案中寻找"异类品味"，防止单一设计风格独断
  - **现实检验师** (reality_checker.py): TRL 1-9 技术成熟度评估，把"理论可行"与"能量产"分开
  - **沙盘推演师** (sandbox_simulator.py): 调用蒙特卡洛引擎，展示最惨失败情形
  - **魔鬼代言人** (devils_advocate.py): 假设方案已失败，倒推"遗书"挑战一厢情愿
- **蒙特卡洛沙盘引擎** (Core/monte_carlo.py): 1000 次参数扰动 (±5% 高斯噪声)，5 种失效模式分类 (热失控/磁饱和/铜耗超标/退磁/效率崩塌)，P5/P50/P95 百分位输出
- **数据飞轮引擎** (Core/flywheel.py): 采纳→技能模板生成+规则挖掘+模板权重提升; 拒绝→失败特征记录+模板权重惩罚
- **决策门 UI**: 双复选框强制阅读放大/收敛报告后方可解锁采纳/拒绝按钮
- **战略雷达面板**: 机会信号 (高效低温方案/技能覆盖率) + 能力缺口 (失败特征/规则缺口) 扫描
- **进化仪表盘扩展**: 新增 AI 接管率 + 失败特征数指标卡片
- **沙盘推演面板**: 候选方案列表 + N 次试验输入 + 汇总卡片 (平均/最差/最佳失效率) + 详情行
- **Flask API 新增**: `/api/flywheel/status`, `/api/sandbox/simulate`, `/api/convergence/run`

### Changed
- LangGraph StateGraph 扩展为双轨并行: `generate_designs → [review_pro, review_con] → decision_gate → generate_report`
- SSE 工作流新增 `convergence_review` 和 `decision_gate` 步骤事件
- 前端 AI 团队流水线扩展为分叉布局 (pipeline-fork → pipeline-pro / pipeline-con)
- 辩证报告区双栏布局: 绿色边框(放大报告) + 红色边框(收敛报告)

### Fixed
- **云端门控漏洞**: review_agent.py / report_agent.py 的 `cloud_available = bool(os.getenv("DEEPSEEK_API_KEY"))` 对占位符 `sk-your-key-here` 返回 True → 严格排除占位符，确保全本地零上云
- **v102.js 脚本引用缺失**: index.html 未加载 `/static/v102.js` → 已补充 `<script>` 标签

### Security
- **全本地零上云验证通过**: 所有智能体 (含4个新收敛剑客) 运行于本地 Ollama Qwen2.5-3B，零云端 API 调用
- .env 中 DEEPSEEK_API_KEY 保持占位符 `sk-your-key-here`，云端路径为死代码

---

## [1.01.01] - 2026-07-27

### 首个正式版本快照 — 完整流程跑通

### Added
- **Motor-CAD COM 自动化引擎** (motorcad_auto 包): 6 模块架构 (config/connector/params/analysis/runner/cli)
- **电磁分析**: ShowMagneticContext() + DoMagneticCalculation() (~200s/组)
- **热分析**: ShowThermalContext() + DoSteadyStateAnalysis() (~10s/组)
- **耦合分析**: DoMagneticThermalCalculation() (~195s/组)
- **变量白名单机制**: 仅查询已验证变量名，防止 GetVariable 无限挂起
- **模板自动管理** (template_manager.py): 自动检测/搜索/复制 .mot 文件
- **COM 诊断工具** (com_diagnose.py): 7 步检查链路 (Python/pywin32/注册表/ActiveX/Dispatch/模板/COM方法)
- **Web UI 批量求解**: SSE 流式进度推送，支持 auto/manual/simulation 三种模式
- **本地 AI 模型集成**: Ollama + Qwen2.5-3B，需求解析/方案评审/报告生成
- **反馈闭环**: feedback_store 模块，记录用户评价驱动进化
- **审计日志**: audit_logger 模块，全流程可追溯
- **知识库**: 本体 JSON、材料数据库、历史技能库

### Changed
- COM 连接流程重写为**预启动模式**: 手动启动 Motor-CAD exe → 等待 90s 完全加载 → Dispatch 连接已运行实例，绕过 Dispatch 60s 超时
- Web UI generate() 改为**单连接复用模式**: 一次 COM 连接求解所有方案，完成后断开
- ActiveX 注册模式自动检测与修复: "免注册" → "单客户端设置" (WM_COMMAND id=94)
- 版本号规范从两段式 (1.04) 迁移到三段式 (1.01.01)

### Fixed
- **COM Dispatch 60s 超时** (CO_E_SERVER_EXEC_FAILURE): Motor-CAD v11.1.5 需 80-120s 启动，Dispatch 内部超时仅 60s → 预启动模式绕过
- **Web UI 双重连接 bug**: generate() 先测试 COM (实例#1 不释放) 再循环求解 (实例#2) → 两个实例冲突 → 改为单连接复用
- **ActiveX "免注册" 模式导致 COM 不可用**: 自动检测并通过 WM_COMMAND id=94 切换为 "单客户端设置"
- **GetVariable 返回格式**: 正确处理 (code, value) 元组，code>=0 为成功，-2 为变量不存在
- **变量名挂起问题**: OperatingSpeed、MaxTemperature、T_[Housing_Average] 等变量导致无限挂起 → 白名单机制规避
- **Flask 线程中 COM 调用**: CoInitialize 在 connect() 中调用，STA 模型线程内可用

### Technical Notes
- Motor-CAD v11.1.5 (32位, 2017-02-12 发布)，正式授权
- COM ProgID: MotorCAD.AppAutomation (CLSID {4ACD06EB-5486-42AF-91DB-FB752E64C465})
- Python: 64位 + pywin32 (COM 跨位数可用)
- 从零启动 Motor-CAD + Dispatch 连接: 约 82 秒
- Motor-CAD 已运行时 Dispatch: 秒连

---

## [1.00.01] - 2026-07-24

### Added
- 初始项目骨架，包含 Motor-CAD 批量求解脚本和本地 3B 模型测试
- Motor-CAD 11.1.5 安装路径配置
- Web 界面基础框架 (Flask + SSE)
- 数据库存储 (SQLite)
