# Motor-AI 变更日志

所有 notable 变更记录在此文件中。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
