# Motor-AI 管理端与客户端软件说明书

**管理端版本**: v1.04.02
**客户端版本**: v1.04.01
**日期**: 2026-07-29

---

## 目录

- [第一部分：Motor-AI Manager 管理端](#第一部分motor-ai-manager-管理端)
  - [1. 系统概述](#1-系统概述)
  - [2. 安装与启动](#2-安装与启动)
  - [3. 主窗口与导航](#3-主窗口与导航)
  - [4. 功能面板详解](#4-功能面板详解)
  - [5. 核心服务模块](#5-核心服务模块)
  - [6. 配置参数](#6-配置参数)
  - [7. 打包与分发](#7-打包与分发)
- [第二部分：Motor-AI Client 客户端](#第二部分motor-ai-client-客户端)
  - [8. 系统概述](#8-系统概述)
  - [9. 安装与启动](#9-安装与启动)
  - [10. 主窗口与导航](#10-主窗口与导航)
  - [11. 功能面板详解](#11-功能面板详解)
  - [12. 双模式求解系统](#12-双模式求解系统)
  - [13. 隐私分级](#13-隐私分级)
  - [14. 私网通信](#14-私网通信)
  - [15. 自动更新](#15-自动更新)
  - [16. 配置参数](#16-配置参数)
  - [17. 打包与安装](#17-打包与安装)
- [第三部分：联机协作](#第三部分联机协作)
  - [18. 系统组网架构](#18-系统组网架构)
  - [19. 故障排查](#19-故障排查)

---

# 第一部分：Motor-AI Manager 管理端

## 1. 系统概述

### 1.1 定位

Motor-AI Manager 是"一人电机设计公司"的总控驾驶舱，替代分散的 .bat 脚本，统一可视化管理调试版（DEV）、试用版（TRIAL）、Ollama LLM 等全部后台服务，并作为私网更新中心向客户端分发升级包。

### 1.2 技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| GUI 框架 | PySide6 (Qt6) | 原生 Windows 桌面应用 |
| 图表绘制 | pyqtgraph | 实时 CPU/内存/磁盘/网络曲线 |
| 系统监控 | psutil | 进程/资源监控 |
| HTTP 客户端 | requests | 与 Flask 后端通信 |
| Web 引擎 | QtWebEngine | 进化看板内嵌网页 |
| 配置存储 | JSON | config/manager_config.json |

### 1.3 版本信息

- **版本号**: 1.04.02
- **入口文件**: `D:\MotorAI_Manager_Dev\main.py`
- **虚拟环境**: `D:\MotorAI_Manager_Dev\venv\`

---

## 2. 安装与启动

### 2.1 环境要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10/11 (64 位) |
| Python | 3.10+ |
| 内存 | 8GB+（同时运行 Flask + Ollama 建议 16GB+） |
| 网络 | 本机 5000/5001/11434/5200 端口可用 |

### 2.2 启动方式

```bash
# 正常模式
D:\MotorAI_Manager_Dev\venv\Scripts\python.exe D:\MotorAI_Manager_Dev\main.py

# 调试模式（详细日志）
D:\MotorAI_Manager_Dev\venv\Scripts\python.exe D:\MotorAI_Manager_Dev\main.py --debug
```

### 2.3 启动流程

1. 加载配置（Config 单例，读取 `config/manager_config.json`）
2. 创建 QApplication，应用深色主题
3. 实例化 MainWindow，初始化所有面板和核心模块
4. 若 `auto_start_services=True`，延迟 2 秒后自动启动所有服务
5. 进入 Qt 事件循环

---

## 3. 主窗口与导航

### 3.1 窗口属性

| 属性 | 值 |
|------|-----|
| 标题 | MotorAI Manager v1.04.02 |
| 默认尺寸 | 1200 × 750 |
| 最小尺寸 | 1000 × 650 |
| 主题 | 深色（背景 #0f172a，文字 #e2e8f0，主色 #3b82f6） |
| 字体 | Microsoft YaHei UI, 9pt |

### 3.2 布局结构

```
┌──────────────────────────────────────────────┐
│  MotorAI Manager v1.04.02            [_][□][×]│
├──────────┬───────────────────────────────────┤
│          │                                   │
│  导航栏   │         内容区                     │
│  (200px) │    (QStackedWidget)               │
│          │                                   │
│  ⚙ 服务管理│                                   │
│  ☰ 任务调度│                                   │
│  ▤ 系统监控│                                   │
│  ☸ 客户端  │                                   │
│  □ 方案库  │                                   │
│  ☺ 用户管理│                                   │
│  ▲ 进化看板│                                   │
│  ≡ 审计日志│                                   │
│  ✭ 系统设置│                                   │
│  ↻ 版本发布│                                   │
│          │                                   │
│  v1.04.02├───────────────────────────────────┤
│          │ 状态栏: 服务 2/3 运行 | CPU 23% | 内存 45%│
└──────────┴───────────────────────────────────┘
```

### 3.3 导航栏（10 个面板）

| 序号 | 标签 | 图标 | 面板类 |
|------|------|------|--------|
| 1 | 服务管理 | ⚙ | ServicePanel |
| 2 | 任务调度 | ☰ | TaskPanel |
| 3 | 系统监控 | ▤ | MonitorPanel |
| 4 | 客户端管理 | ☸ | ClientPanel |
| 5 | 方案库 | □ | LibraryPanel |
| 6 | 用户管理 | ☺ | UserPanel |
| 7 | 进化看板 | ▲ | EvolutionPanel |
| 8 | 审计日志 | ≡ | AuditPanel |
| 9 | 系统设置 | ✭ | SettingsPanel |
| 10 | 版本发布 | ↻ | ReleasePanel |

### 3.4 状态栏

每 3 秒刷新，显示：服务运行数（如 2/3）、CPU 使用率、内存使用率。

### 3.5 窗口行为

- **关闭**: 若 `minimize_to_tray=True`（默认），拦截关闭并最小化到系统托盘
- **退出**: 弹出确认对话框，询问是否停止所有服务
- **托盘**: 右键菜单可显示窗口/启动所有服务/停止所有服务/退出

---

## 4. 功能面板详解

### 4.1 服务管理面板 (ServicePanel)

以卡片形式展示每个后台服务，支持启动/停止/重启/查看日志。

#### 预配置服务

| 服务名 | 显示名 | 端口 | 启动顺序 | 环境变量 |
|--------|--------|------|----------|---------|
| flask_dev | Flask 调试版 | 5000 | 3 | MOTOR_ENV=dev |
| flask_trial | Flask 试用版 | 5001 | 2 | MOTOR_ENV=trial |
| ollama | Ollama LLM | 11434 | 4 | - |

#### 服务卡片功能

- **启动**: 点击"启动"按钮启动服务进程
- **停止**: 点击"停止"按钮停止服务
- **重启**: 先停止再启动
- **查看日志**: 打开日志查看窗口，显示实时日志输出
- **状态指示**: STOPPED（灰）/ STARTING（黄）/ RUNNING（绿）/ ERROR（红）

#### Motor-CAD 集成

- 服务面板有"启动 Motor-CAD"按钮
- 未运行时显示"未运行（点击右侧启动）"琥珀色提示
- 每 60 秒自动检测 Motor-CAD 进程状态

#### v1.04.02 端口领养机制

启动前检测端口是否已响应：
- **端口已响应** → adopt 已有进程（不 spawn 新进程）
- **进程死但端口活** → adopt 不 auto-restart
- **stop 外部进程** → 只 untrack 不 kill

### 4.2 任务调度面板 (TaskPanel)

汇总 DEV/TRIAL 双环境的任务队列。

- 每 3 秒自动刷新，后台线程加载
- 表格展示：任务 ID、类型、状态、进度、来源、创建时间
- 进度条可视化
- 支持取消任务

### 4.3 系统监控面板 (MonitorPanel)

实时系统资源监控。

#### 监控指标

| 指标 | 单位 | 说明 |
|------|------|------|
| CPU | % | 总体 CPU 使用率 |
| 内存 | % | 内存占用率 |
| 磁盘读写 | MB/s | 磁盘 IO 速率 |
| 网络收发 | KB/s | 网络 IO 速率 |

#### 图表特性

- pyqtgraph 实时曲线图
- 30 分钟滚动窗口（900 个数据点）
- 背景 #0f172a，曲线 #94a3b8
- 采集间隔 2 秒

#### 阈值告警

| 阈值 | 默认值 | 持续时间 | 动作 |
|------|--------|---------|------|
| CPU 告警 | 90% | 30 秒 | 发出 threshold_exceeded 信号 |
| 内存告警 | 90% | 30 秒 | 发出 threshold_exceeded 信号 |

### 4.4 客户端管理面板 (ClientPanel)

管理已连接的客户端设备。

#### 显示信息

- 客户端 ID、hostname、Tailscale IP、版本号
- 在线状态、最后活跃时间
- 当前运行中任务

#### 可配置项

| 配置 | 说明 |
|------|------|
| 求解模式 | 允许的求解模式（local/remote） |
| 隐私级别 | strict / full |
| 并发任务数 | 最大并行任务数 |
| AI 模型权限 | 允许使用的 AI 模型 |
| 自动更新 | 是否允许自动更新 |

### 4.5 方案库面板 (LibraryPanel)

查询所有客户端上传的 AI 团队工作流结果。

#### 显示字段

项目 ID、电机类型、方案数、效率、温升、客户端 ID、授权告警、创建时间。

#### 详情查看

- 解析结果（需求结构化）
- 支持报告（评审推荐师）
- 反对报告（收敛阵营）
- 正式报告（最终设计报告）
- 支持下载

#### 筛选

支持按环境（DEV/TRIAL）和客户端 ID 筛选。

### 4.6 用户管理面板 (UserPanel)

查看 DEV/TRIAL 双环境用户列表与积分。

- 每 5 秒自动刷新
- 显示用户名、角色、会员等级、积分余额、注册时间
- 预留完整多租户管理功能

### 4.7 进化看板面板 (EvolutionPanel)

内嵌 Web 页面（QWebEngineView），加载后端进化看板主页面。

- 支持 DEV/TRIAL 环境切换
- 显示飞轮健康度、采纳率、技能数、规则数
- 显示进化日志和阈值告警

### 4.8 审计日志面板 (AuditPanel)

双环境操作日志查询。

- 日期范围筛选
- 关键词搜索
- CSV 导出
- 智能体性能指标查看

### 4.9 系统设置面板 (SettingsPanel)

管理端全局配置，分页式布局。

#### 设置分组

| 分页 | 配置项 |
|------|--------|
| 应用配置 | 轮询间隔、自动启动服务、崩溃自动重启、日志保留天数、最小化到托盘、开机自启 |
| 服务配置 | 服务列表（增删改）、端口、启动顺序 |
| Motor-CAD | 可执行路径、COM ProgID、检测间隔 |
| 更新服务 | 启用状态、监听地址、端口、Tailscale IP、仓库目录 |
| 阈值配置 | CPU/内存告警阈值、持续时间 |

### 4.10 版本发布面板 (ReleasePanel)

版本发布与升级包管理中心。

- 导入升级包（Manager/Client DEV/Client TRIAL）
- 启动/停止 HTTP 更新服务（端口 5200）
- 管理版本清单（manifest.json）
- 查看 SHA256 校验值

---

## 5. 核心服务模块

### 5.1 ServiceManager（服务进程管理器）

| 特性 | 说明 |
|------|------|
| 线程安全 | threading.Lock 保护 |
| 状态枚举 | STOPPED / STARTING / RUNNING / ERROR |
| 自动重启 | crash_auto_restart=True 时崩溃自动重启 |
| 端口领养 | v1.04.02: 端口已响应时 adopt 已有进程 |
| 配置迁移 | 启动时检查服务脚本是否存在，不存在则自动移除 |
| Qt 信号 | status_changed / log_output / service_error |

### 5.2 ResourceMonitor（资源监控器）

| 特性 | 说明 |
|------|------|
| 运行方式 | QThread 后台线程 |
| 采集间隔 | 2 秒 |
| 历史缓冲 | 900 个数据点（30 分钟） |
| 监控指标 | CPU/内存/磁盘读写/网络收发 |
| 阈值跟踪 | CPU 和内存持续超阈值计时 |
| Qt 信号 | metrics_updated / threshold_exceeded |

### 5.3 ApiClient（API 客户端）

| 特性 | 说明 |
|------|------|
| 双环境 | DEV (127.0.0.1:5000) / TRIAL (127.0.0.1:5001) |
| 认证 | JWT Token（Bearer 头，按环境存储） |
| 超时 | 连接 3 秒，读取 10 秒 |
| 在线检测 | Socket connect 快速检测（0.5 秒超时） |
| 状态缓存 | 在线状态缓存 5 秒 |

### 5.4 UpdateServer（更新分发服务器）

| 特性 | 说明 |
|------|------|
| 运行方式 | QThread 后台 HTTP 服务器 |
| 监听端口 | 5200（默认） |
| 技术栈 | Python http.server |
| 版本清单 | update_repo/*/manifest.json |
| 包类型 | manager / client_dev / client_trial |
| 文件校验 | SHA256 哈希 |

#### HTTP API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/version/latest | 返回最新版本信息 |
| GET | /api/version | 同上（兼容） |
| GET | /packages/{filename} | 下载升级包 |
| GET | /api/packages | 返回所有升级包列表 |
| GET | /health | 健康检查 |

### 5.5 TrayIcon（系统托盘）

| 特性 | 说明 |
|------|------|
| 图标 | 动态彩色圆形（绿/黄/红） |
| 工具提示 | MotorAI Manager |
| 右键菜单 | 显示窗口/启动所有服务/停止所有服务/退出 |
| 气泡通知 | 服务状态变化通知 |
| Qt 信号 | show_window_requested / start_all_requested / stop_all_requested / quit_requested |

### 5.6 日志系统

| 特性 | 说明 |
|------|------|
| 日志名称 | MotorAI_Manager |
| 默认级别 | INFO（调试模式 DEBUG） |
| 格式 | 时间 [级别] 模块: 消息 |
| 文件日志 | 按天滚动，保留 30 天 |
| 日志路径 | D:\MotorAI_Manager_Dev\logs\manager_YYYYMMDD.log |

---

## 6. 配置参数

### 6.1 配置文件

**路径**: `D:\MotorAI_Manager_Dev\config\manager_config.json`

采用 Singleton 单例模式，支持运行时修改并自动保存。

### 6.2 完整参数表

#### app（应用配置）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| name | MotorAI Manager | 应用名称 |
| version | 1.04.02 | 版本号 |
| poll_interval | 5 | 轮询间隔（秒） |
| auto_start_services | false | 自动启动服务 |
| crash_auto_restart | true | 崩溃自动重启 |
| log_retention_days | 30 | 日志保留天数 |
| minimize_to_tray | true | 最小化到托盘 |
| auto_start_with_windows | false | 开机自启 |

#### motorcad（Motor-CAD 配置）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| exe_path | D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe | 程序路径 |
| com_progid | MotorCAD.AppAutomation | COM ProgID |
| check_interval | 60 | 检测间隔（秒） |

#### update_server（更新服务）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| enabled | false | 启用状态 |
| host | 0.0.0.0 | 监听地址 |
| port | 5200 | 监听端口 |
| tailscale_ip | (空) | Tailscale 虚拟 IP |
| repo_dir | update_repo/ | 仓库目录 |

#### api（API 客户端）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| dev_base | http://127.0.0.1:5000 | DEV API 地址 |
| trial_base | http://127.0.0.1:5001 | TRIAL API 地址 |
| timeout | 10 | 请求超时（秒） |

#### thresholds（阈值配置）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| cpu_warn | 90 | CPU 告警阈值（%） |
| mem_warn | 90 | 内存告警阈值（%） |
| cpu_duration_sec | 30 | CPU 持续超阈值时间 |
| mem_duration_sec | 30 | 内存持续超阈值时间 |

---

## 7. 打包与分发

### 7.1 打包脚本

**文件**: `D:\MotorAI_Manager_Dev\build.bat`

| 项目 | 说明 |
|------|------|
| 工具 | PyInstaller |
| 模式 | --windowed（无控制台窗口） |
| 图标 | assets\icon.ico |
| 打包数据 | config 目录 |
| 隐藏导入 | PySide6.QtWebEngineWidgets, PySide6.QtWebEngineCore, pyqtgraph |

### 7.2 输出

| 产物 | 路径 | 说明 |
|------|------|------|
| 打包目录 | dist\MotorAI_Manager\ | 可直接运行 |
| 升级包 | update_repo\MotorAI_Manager_update_v{VERSION}.zip | 分发给已安装客户端 |
| 版本清单 | update_repo\manifest_manager_v{VERSION}\manifest.json | 含 SHA256 |

---

# 第二部分：Motor-AI Client 客户端

## 8. 系统概述

### 8.1 定位

Motor-AI Client 是部署在客户电脑上的客户端程序，提供本地 Motor-CAD 求解、远程委托求解、AI 团队工作流交互、本地方案库管理等功能。所有核心设计参数永存客户本地，远程求解时参数可选择性脱敏后上传。

### 8.2 核心特性

| 特性 | 说明 |
|------|------|
| **双模式求解** | 本地求解（自有 Motor-CAD）或委托求解（管理端协调算力） |
| **数据主权** | 所有核心设计参数永存本地 SQLite |
| **隐私分级** | strict（严格脱敏）/ full（完整上传） |
| **私网通信** | Tailscale WireGuard 端到端加密 |
| **自动更新** | 从管理端下载更新包，SHA256 校验，数据不丢失 |
| **离线可用** | 本地方案库完全在本地，不依赖网络 |

### 8.3 技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| GUI 框架 | PySide6 (Qt6) | 原生 Windows 桌面应用 |
| 本地数据库 | SQLite (WAL) | 33+ 字段方案库 |
| Motor-CAD 调用 | win32com (COM) | 本地求解引擎 |
| 私网通信 | Tailscale | WireGuard 端到端加密 |
| HTTP 客户端 | requests | 与管理端 API 通信 |
| 系统监控 | psutil | 进程检测 |

### 8.4 版本信息

- **版本号**: 1.04.01
- **入口文件**: `D:\MotorAI_Client_Dev\main.py`
- **虚拟环境**: `D:\MotorAI_Client_Dev\venv\`
- **安装包**: MotorAI_Client_Setup_v1.04.01.exe (77MB)

---

## 9. 安装与启动

### 9.1 环境要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10/11 (64 位) |
| Python | 3.10+（开发模式） |
| Motor-CAD | v11.1.5（本地求解需要） |
| 内存 | 8GB+ |
| 网络 | Tailscale 可用（委托求解需要） |

### 9.2 安装方式

#### 方式一：安装包（推荐终端用户）

运行 `MotorAI_Client_Setup_v1.04.01.exe`，安装到 LocalAppData，无需管理员权限。

安装包内容：
- MotorAI_Client.exe (49MB)
- Tailscale MSI (34.8MB)
- .mot 模板 (88KB)
- 依赖运行时

#### 方式二：开发模式

```bash
D:\MotorAI_Client_Dev\venv\Scripts\python.exe D:\MotorAI_Client_Dev\main.py
```

### 9.3 启动流程

1. 检测 PyInstaller 冻结模式，确定项目根目录
2. 加载配置（Config 单例，读取 `config/client_config.json`）
3. 自动检测 Motor-CAD 安装路径和 .mot 模板
4. 创建 QApplication，应用深色主题
5. 实例化 MainWindow，初始化所有面板和核心模块
6. Tailscale 静默注册 + 心跳启动
7. 进入 Qt 事件循环

### 9.4 首次运行

1. Tailscale 自动安装（如未安装）和注册
2. Motor-CAD 自动检测（扫描多盘符 + 注册表）
3. .mot 模板自动检测（Motor-CAD 目录 + _MEIPASS）
4. 向管理端注册设备信息

---

## 10. 主窗口与导航

### 10.1 窗口属性

| 属性 | 值 |
|------|-----|
| 标题 | Motor-AI 客户端 v{version} |
| 最小尺寸 | 1000 × 680 |
| 主题 | 深色（背景 #1a1a2e，文字 #e2e8f0） |
| 字体 | Microsoft YaHei UI, 9pt |
| 关闭行为 | 最小化到系统托盘（不退出） |

### 10.2 布局结构

```
┌──────────────────────────────────────────────┐
│  Motor-AI 客户端 v1.04.01             [_][□][×]│
├──────────┬───────────────────────────────────┤
│          │                                   │
│  导航栏   │         内容区                     │
│  (220px) │    (QStackedWidget)               │
│          │                                   │
│  任务列表 │                                   │
│  方案库   │                                   │
│  AI 团队 │                                   │
│  设置     │                                   │
│  更新     │                                   │
│          │                                   │
├──────────┴───────────────────────────────────┤
│ 状态栏: 就绪 | Tailscale: 100.x.x.x | v1.04.01│
└──────────────────────────────────────────────┘
```

### 10.3 导航栏（5 个面板）

| 序号 | 标签 | 面板类 |
|------|------|--------|
| 1 | 任务列表 | TaskPanel |
| 2 | 方案库 | LibraryPanel |
| 3 | AI 团队 | AITeamPanel |
| 4 | 设置 | SettingsPanel |
| 5 | 更新 | UpdatePanel |

---

## 11. 功能面板详解

### 11.1 任务列表面板 (TaskPanel)

展示所有求解任务，支持新建、查看详情、取消。

#### 表格列

| 列 | 说明 |
|----|------|
| 状态 | pending/running/completed/failed/cancelled |
| 项目名 | 用户输入的项目名称 |
| 电机类型 | 电机类型分类 |
| 模式 | local（本地）/ remote（委托） |
| 方案数 | 总方案数 / 已完成数 |
| 进度 | 进度条百分比 |
| 创建时间 | 任务创建时间 |

#### 操作

- **新建任务**: 打开新建任务对话框
- **查看详情**: 查看任务参数和结果
- **取消任务**: 取消正在运行的任务

#### 自动刷新

QTimer 定时刷新任务列表。

### 11.2 方案库面板 (LibraryPanel)

筛选、搜索、对比、导出本地完整设计历史。

#### 数据源

本地 SQLite 数据库，完全离线可用。

#### 功能

- 按项目名/电机类型/求解模式/时间范围筛选
- 关键词搜索
- 方案对比（多选）
- 导出 CSV / JSON 格式
- 查看方案详情（参数 + 性能结果）

### 11.3 AI 团队面板 (AITeamPanel)

多智能体协同工作流界面，通过 SSE 连接管理端 `/api/workflow/run`。

#### 工作流步骤（6 步卡片式展示）

| 步骤 | 角色 | 说明 |
|------|------|------|
| 1. 需求分析 | 需求分析师 | 解析并结构化客户需求 |
| 2. 方案生成 | 方案生成师 | D²L 聚焦扫描生成候选方案 |
| 3a. 评审推荐 | 评审推荐师 | 放大：AI 点评 + 物理核验 |
| 3b. 收敛审查 | 收敛四剑客 | 收敛：逆向/现实/沙盘/魔鬼 |
| 4. 决策门 | 决策门 | 双报告必读，判定是否通过 |
| 5. 报告生成 | 报告撰写员 | 生成最终设计报告 |

#### 实时交互

- SSE 实时日志流
- 步骤进度卡片动画
- 过程交付物查看（解析结果/支持报告/反对报告/正式报告）
- 保存到本地方案库 + 同步后端

#### 端口自动探测

`ai_client.py` 自动探测后端端口（5000/5001），无需手动配置。

### 11.4 设置面板 (SettingsPanel)

4 个设置分组，QScrollArea 包裹，保存按钮固定底部。

#### 求解设置

| 配置 | 选项 | 说明 |
|------|------|------|
| 默认模式 | ask / local / remote | 新建任务时的默认求解模式 |
| 隐私级别 | strict / full | 委托求解时的脱敏级别 |
| Motor-CAD 路径 | (自动检测) | Motor-CAD 可执行文件路径 |
| 模板路径 | (自动检测) | .mot 模板文件路径 |

#### 网络设置

| 配置 | 说明 |
|------|------|
| Tailscale 状态 | 显示连接状态和 IP |
| 管理端地址 | 管理端 Tailscale IP |

#### 系统设置

| 配置 | 说明 |
|------|------|
| 开机自启 | Windows 启动时自动运行 |
| 最小化到托盘 | 关闭窗口时最小化 |

#### 数据管理

| 功能 | 说明 |
|------|------|
| 备份 | 备份本地数据库 |
| 查看统计 | 查看方案库统计信息 |

### 11.5 更新面板 (UpdatePanel)

显示当前版本、检查更新、查看更新日志、下载并安装。

- **当前版本**: 显示版本号和更新日志
- **检查更新**: 后台线程检查管理端是否有新版本
- **下载更新**: 后台线程下载更新包，显示进度
- **安装更新**: 下载完成后安装（备份 → 替换 → 重启）

### 11.6 新建任务对话框 (NewTaskDialog)

| 输入项 | 说明 |
|--------|------|
| 项目名称 | 用户自定义 |
| 电机类型 | 电机类型选择 |
| 需求描述 | 文本描述或直接编辑参数矩阵 JSON |
| 求解模式 | 本地 / 委托 |
| 隐私级别 | 严格脱敏 / 完整上传 |
| 模板文件 | Motor-CAD .mot 模板路径（本地求解时） |

对话框尺寸：640 × 680 固定。

---

## 12. 双模式求解系统

### 12.1 本地求解器 (LocalSolver)

**技术**: Motor-CAD COM 自动化 (win32com)

#### 求解流程

```
连接 COM → 加载 .mot 模板 → 设置参数 → 电磁分析 → 热分析 → 提取结果
```

#### COM 方法（Motor-CAD v11.1.5 实测）

| COM 方法 | 说明 | 耗时 |
|----------|------|------|
| Dispatch("MotorCAD.AppAutomation") | COM 连接 | 80-120s |
| LoadFromFile(path) | 加载 .mot 模型 | - |
| SetVariable / GetVariable | 设置/读取参数 | - |
| ShowMagneticContext() | 切换电磁上下文（必须先调用） | - |
| DoMagneticCalculation() | 电磁分析 | ~190-200s |
| DoSteadyStateAnalysis() | 热分析 | ~2-10s |
| DoMagneticThermalCalculation() | 耦合分析 | ~195s |

#### 结果变量映射

**热分析结果**:

| Motor-CAD 变量 | 客户端字段 |
|---------------|-----------|
| T_[Winding_Average] | winding_temp |
| T_[Magnet_Average] | magnet_temp |
| T_[Housing_Average] | housing_temp |
| T_[Rotor_Average] | rotor_temp |
| T_[End_Winding_Average] | end_winding_temp |

**电磁分析结果**:

| Motor-CAD 变量 | 客户端字段 |
|---------------|-----------|
| OutputPower | output_power |
| InputPower | input_power |
| Shaft_Torque_[pu] | torque_pu |
| Loss_[Total] | total_loss |
| Loss_[Copper] | copper_loss |
| Loss_[Iron] | iron_loss |
| Loss_[Windage] | windage_loss |
| Loss_[Friction] | friction_loss |

#### 状态检测

`check_available()` 检查：
- exe 文件是否存在
- Motor-CAD 进程是否在运行（psutil）

### 12.2 远程求解器 (RemoteSolver)

与管理端通信，将脱敏后的求解任务委托给管理端协调的算力资源池。

#### API 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | /api/client/delegate | 提交委托请求 |
| GET | /api/client/tasks/{id}/progress | 轮询求解进度 |
| GET | /api/client/tasks/{id}/result | 接收脱敏结果 |
| POST | /api/client/tasks/{id}/cancel | 取消委托任务 |
| GET | /api/status | 连通性检测 |

#### 安全设计

所有参数在上传前经过 PrivacyManager 脱敏处理。请求头携带 `Authorization: Bearer {auth_token}`。

### 12.3 降级机制

本地求解连接失败时：
- 若 `auto_fallback_to_remote=True`，触发 `on_fallback` 回调
- 询问用户是否委托远程求解
- 用户确认后切换到 RemoteSolver

### 12.4 任务状态机

```
pending → running → completed
                 → failed
                 → cancelled
```

---

## 13. 隐私分级

### 13.1 两种隐私级别

| 级别 | 说明 | 使用场景 |
|------|------|---------|
| **strict** | 严格脱敏（默认） | 仅上传离散数值参数矩阵，完全剥离材料牌号、几何拓扑、绕组方案、客户信息 |
| **full** | 完整上传 | 上传完整参数集，仅供高度信任场景 |

### 13.2 数据类别对照

| 数据类别 | strict | full |
|----------|--------|------|
| 几何尺寸、槽型、磁钢牌号 | 不上传 | 上传 |
| 材料参数、绕组方案 | 不上传 | 上传 |
| 效率、温升、转矩等性能 | 脱敏摘要 | 完整 |
| 客户身份、项目名 | 上传 | 上传 |

### 13.3 字段分类

#### strict 模式下完全剥离的敏感字段

```
winding_type, winding_scheme, material_grade, magnet_grade,
core_material, slot_type, geometry_topology, tooth_shape,
magnet_shape, rotor_topology, stator_topology,
cooling_type, housing_material, shaft_material,
iron_length_detail, air_gap_detail, winding_layout
```

#### strict 模式下可上传的安全字段（离散数值参数）

```
power, voltage, speed, poles, slots, frequency,
outer_diameter, inner_diameter, core_length, air_gap,
turns, current, torque_target
```

#### strict 模式下只上传摘要的性能字段

```
efficiency, output_power, input_power, torque, max_temp, total_loss
```

### 13.4 保守策略

未知字段在 strict 模式下默认不上传。

---

## 14. 私网通信

### 14.1 Tailscale 集成

**文件**: `src/core/tailscale.py`

#### 功能

1. 检测 Tailscale 安装状态和连接状态
2. 首次启动时静默注册（`tailscale up --authkey=xxx`）
3. 心跳检测与管理端的连通性（30 秒间隔）
4. 断线自动重连
5. 获取本机 Tailscale IP

#### 安全设计

- Auth Key 预置在配置中，带 `tag:client` 标签
- Tailscale ACL 限制客户端只能访问管理端特定端口
- 所有通信走 WireGuard 端到端加密

#### 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| enabled | true | 启用 Tailscale |
| auth_key | (空) | 预授权 Auth Key |
| hostname_prefix | motorai-client | 主机名前缀 |
| check_interval | 30 | 心跳检测间隔（秒） |

### 14.2 AI 客户端端口探测

`ai_client.py` 自动探测后端端口：

| 端口 | 环境 |
|------|------|
| 5000 | DEV |
| 5001 | TRIAL |

- `_probe_port(host, port, timeout)`: socket 层面快速检测
- `_detect_backend_port()`: 自动探测可用端口，优先使用配置端口
- 端口缓存 120 秒

---

## 15. 自动更新

### 15.1 更新流程

```
查询版本 → 下载更新包 → SHA256 校验 → 备份当前版本 → 替换文件 → 重启 → 回滚（如需要）
```

### 15.2 版本查询

向管理端查询最新客户端版本：

```
GET http://{manager_host}:5200/api/version/latest?type=client
```

### 15.3 安全保障

| 保障 | 说明 |
|------|------|
| SHA256 校验 | 下载后校验文件哈希 |
| 数据保留 | data/、config/、backup/ 目录绝不覆盖 |
| 回滚机制 | 新版本连续启动失败时恢复上一版本 |
| 加密传输 | 通过 Tailscale WireGuard 加密信道下载 |

### 15.4 更新包信息

| 项目 | 值 |
|------|-----|
| 版本 | 1.04.01 |
| 大小 | ~48 MB |
| 下载地址 | /packages/MotorAI_Client_update_v1.04.01.zip |
| 检查间隔 | 6 小时 |
| 自动下载 | 默认关闭 |

### 15.5 回调接口

| 回调 | 触发时机 |
|------|---------|
| on_update_available | 发现有新版本 |
| on_download_progress | 下载进度更新 |
| on_update_ready | 更新已就绪 |
| on_error | 发生错误 |

---

## 16. 配置参数

### 16.1 配置文件

**路径**: `config/client_config.json`

采用 Singleton 单例模式，支持 PyInstaller 冻结模式。

### 16.2 完整参数表

#### app（应用配置）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| name | MotorAI Client | 应用名称 |
| version | 1.04.01 | 版本号 |
| client_id | 自动生成 | 格式: {hostname}-{8位hex} |
| poll_interval | 5 | 轮询间隔（秒） |
| minimize_to_tray | true | 最小化到托盘 |
| auto_start_with_windows | false | 开机自启 |
| log_retention_days | 30 | 日志保留天数 |

#### solver（求解配置）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| default_mode | ask | 默认求解模式（ask/local/remote） |
| privacy_level | strict | 隐私级别（strict/full） |
| motorcad_exe | (自动检测) | Motor-CAD 路径 |
| motorcad_progid | MotorCAD.AppAutomation | COM ProgID |
| mot_template | (自动检测) | .mot 模板路径 |
| com_init_wait | 10 | COM 初始化等待（秒） |
| run_thermal | true | 执行热分析 |
| run_emag | true | 执行电磁分析 |
| auto_fallback_to_remote | true | 本地失败自动降级 |

#### manager（管理端配置）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| host | (空) | 管理端 Tailscale IP |
| port | 5200 | 更新服务端口 |
| api_port | 5000 | API 端口 |
| api_timeout | 15 | API 超时（秒） |
| auth_token | (空) | 认证令牌 |
| device_registered | false | 设备注册状态 |

#### tailscale（私网通信）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| enabled | true | 启用 Tailscale |
| auth_key | (空) | 预授权 Auth Key |
| hostname_prefix | motorai-client | 主机名前缀 |
| check_interval | 30 | 心跳间隔（秒） |

#### update（自动更新）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| check_interval_hours | 6 | 检查间隔（小时） |
| auto_download | false | 自动下载 |
| backup_dir | backup/ | 备份目录 |

### 16.3 自动检测功能

| 函数 | 说明 |
|------|------|
| auto_detect_motorcad() | 扫描 C/D/E 盘常见路径 + Windows 注册表 |
| auto_detect_mot_template() | 搜索 Motor-CAD 目录 + _MEIPASS + 项目 templates/ |

### 16.4 便捷属性

| 属性 | 返回值 |
|------|--------|
| manager_base_url | http://{host}:{api_port} |
| manager_update_url | http://{host}:{port} |
| client_id | 客户端唯一标识 |
| db_path | data/motorai_client.db |

---

## 17. 打包与安装

### 17.1 打包脚本

**文件**: `D:\MotorAI_Client_Dev\build.bat`

| 项目 | 说明 |
|------|------|
| 工具 | PyInstaller --onefile --windowed |
| 输出 | 单个 MotorAI_Client.exe (49MB) |
| 图标 | assets/icon.ico |
| 隐藏导入 | pywin32, psutil |

### 17.2 安装包

**文件**: `MotorAI_Client_Installer.iss` (Inno Setup)

| 项目 | 说明 |
|------|------|
| 安装包名 | MotorAI_Client_Setup_v1.04.01.exe |
| 大小 | 77MB |
| 安装位置 | LocalAppData（无需管理员权限） |
| 包含 | MotorAI_Client.exe + Tailscale MSI + .mot 模板 |

### 17.3 安装包集成内容

| 内容 | 大小 | 说明 |
|------|------|------|
| MotorAI_Client.exe | 49MB | 主程序 |
| tailscale-setup.msi | 34.8MB | Tailscale 安装包 |
| default_template.mot | 88KB | Motor-CAD 模板 |

### 17.4 Tailscale 安装

安装包通过 Inno Setup Task `install_tailscale` 使用 `ShellExec('runas')` UAC 提升安装 Tailscale MSI。

---

## 18. 系统组网架构

### 18.1 整体拓扑

```
                    ┌─────────────────────┐
                    │   Tailscale 私网     │
                    │   (WireGuard 加密)   │
                    └──┬───────────────┬──┘
                       │               │
           ┌───────────┴──┐     ┌──────┴──────────┐
           │  管理端 (主机)  │     │  客户端 (客户机)  │
           │                │     │                  │
           │  Manager v1.04 │     │  Client v1.04    │
           │  ├ 服务管理    │     │  ├ 本地求解      │
           │  ├ Flask DEV   │←──→│  ├ 远程委托      │
           │  ├ Flask TRIAL │     │  ├ AI 团队      │
           │  ├ Ollama LLM  │     │  ├ 方案库       │
           │  ├ 更新服务:5200│────→│  ├ 自动更新     │
           │  └ Motor-CAD   │     │  └ Motor-CAD   │
           └────────────────┘     └──────────────────┘
```

### 18.2 通信端口

| 端口 | 服务 | 方向 |
|------|------|------|
| 5000 | Flask DEV | 管理端 → 客户端（DEV API） |
| 5001 | Flask TRIAL | 管理端 → 客户端（TRIAL API） |
| 5200 | 更新服务 | 管理端 → 客户端（升级包下载） |
| 11434 | Ollama | 管理端本机（LLM 推理） |

### 18.3 数据流

```
客户端新建任务 → 选择求解模式
  ├─ 本地求解: 客户端 COM → Motor-CAD → 结果存本地 DB
  └─ 远程委托: 客户端 → 脱敏参数 → 管理端 Flask → Motor-CAD → 脱敏结果 → 客户端

客户端 AI 团队 → SSE 连接管理端 → 多智能体工作流 → 过程交付物 → 保存本地 + 同步后端
```

### 18.4 系统托盘状态指示

| 状态 | 颜色 | RGB | 说明 |
|------|------|-----|------|
| idle | 绿色 | (34, 197, 94) | 空闲 |
| working | 黄色 | (234, 179, 8) | 求解中 |
| error | 红色 | (239, 68, 68) | 异常 |
| offline | 灰色 | (107, 114, 128) | 离线 |

---

## 19. 故障排查

### 19.1 管理端常见问题

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| 服务启动后立即停止 | 端口被占用 | 检查端口 5000/5001/11434 是否被占用 |
| 服务反复重启 | 脚本路径不存在 | 配置迁移会自动移除无效服务，检查 config |
| CPU/内存告警频繁 | 资源不足 | 关闭不必要的进程，调整阈值 |
| API 客户端连接超时 | Flask 未启动 | 先在服务管理面板启动 Flask |
| 进化看板白屏 | QtWebEngine 问题 | 安装 PySide6 QtWebEngineWidgets |
| 更新服务无法启动 | 端口 5200 被占用 | 检查端口占用，或修改配置端口 |

### 19.2 客户端常见问题

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| COM 连接失败 | Motor-CAD 未安装/未启动 | 检查 Motor-CAD 安装，先手动启动 |
| 电磁分析报错 | 未切换电磁上下文 | Motor-CAD 设置"单客户端"模式 |
| Tailscale 连接失败 | 未安装或 Auth Key 无效 | 安装 Tailscale MSI，检查 Auth Key |
| 委托求解超时 | 管理端不可达 | 检查 Tailscale 连接和管理端状态 |
| 更新失败 | SHA256 校验不通过 | 重新下载更新包 |
| 方案库为空 | 数据库未初始化 | 首次运行会自动创建数据库 |
| 深色主题白底白字 | QComboBox/QMenu 样式问题 | v1.04.01 已修复全局样式 |
| 导入错误 | 模块路径问题 | `from ..utils`→`from .utils` 已修复 |

### 19.3 日志位置

| 组件 | 日志路径 |
|------|---------|
| 管理端 | D:\MotorAI_Manager_Dev\logs\manager_YYYYMMDD.log |
| 客户端 | <安装目录>\logs\client_YYYYMMDD.log |
| 后端 DEV | 控制台输出 |
| 后端 TRIAL | 控制台输出 |

### 19.4 日志保留

- 管理端: 30 天（按天滚动）
- 客户端: 30 天（5MB × 5 个 RotatingFileHandler）

---

## 附录 A: 管理端文件结构

```
D:\MotorAI_Manager_Dev\
├── main.py                          # 入口程序
├── requirements.txt                 # 依赖清单
├── build.bat                        # 打包脚本
├── config\
│   └── manager_config.json          # 运行时配置
├── logs\                            # 运行日志
├── update_repo\                     # 升级包仓库
└── src\
    ├── main_window.py               # 主窗口 + 导航
    ├── core\
    │   ├── service_manager.py       # 服务进程管理
    │   ├── api_client.py            # Flask API 客户端
    │   ├── resource_monitor.py      # 系统资源监控
    │   ├── update_server.py         # 更新分发服务器
    │   └── tray_icon.py             # 系统托盘
    ├── ui\                          # 10 个功能面板
    │   ├── service_panel.py         # 服务管理
    │   ├── task_panel.py            # 任务调度
    │   ├── monitor_panel.py         # 系统监控
    │   ├── client_panel.py          # 客户端管理
    │   ├── library_panel.py         # 方案库
    │   ├── user_panel.py            # 用户管理
    │   ├── evolution_panel.py       # 进化看板
    │   ├── audit_panel.py           # 审计日志
    │   ├── settings_panel.py        # 系统设置
    │   └── release_panel.py         # 版本发布
    └── utils\
        ├── config.py                # 配置管理器 (Singleton)
        └── logger.py                # 日志系统
```

## 附录 B: 客户端文件结构

```
D:\MotorAI_Client_Dev\
├── main.py                          # 程序入口
├── build.bat                        # 打包脚本
├── MotorAI_Client.spec              # PyInstaller 配置
├── MotorAI_Client_Installer.iss     # Inno Setup 安装脚本
├── requirements.txt                 # Python 依赖
├── tailscale-setup.msi              # Tailscale 安装包 (35MB)
├── config\
│   ├── client_config.json           # 客户端配置
│   └── ai_team_reports\             # AI 团队报告
├── data\                            # 本地 SQLite 数据库
├── backup\                          # 更新备份
├── logs\                            # 日志目录
├── templates\
│   └── default_template.mot         # Motor-CAD 模板
├── update_repo\
│   └── version_info.json            # 版本信息
└── src\
    ├── main_window.py               # 主窗口
    ├── core\
    │   ├── task_manager.py          # 任务管理与调度
    │   ├── local_solver.py          # 本地求解引擎 (COM)
    │   ├── remote_solver.py         # 委托求解代理
    │   ├── local_db.py              # 本地 SQLite 方案库
    │   ├── privacy.py               # 隐私脱敏模块
    │   ├── tailscale.py             # 私网通信
    │   ├── auto_updater.py          # 自动更新
    │   ├── ai_client.py             # AI 对话客户端
    │   └── tray_icon.py             # 系统托盘
    ├── ui\
    │   ├── task_panel.py            # 任务列表面板
    │   ├── library_panel.py         # 方案库面板
    │   ├── ai_team_panel.py         # AI 团队面板
    │   ├── settings_panel.py        # 设置面板
    │   ├── update_panel.py          # 更新面板
    │   └── new_task_dialog.py       # 新建任务对话框
    └── utils\
        ├── config.py                # 配置管理器 (Singleton)
        └── logger.py                # 日志器
```

## 附录 C: 客户端本地数据库表结构

### designs（设计方案库）

| 字段类别 | 字段 |
|----------|------|
| 基本信息 | id, project_name, motor_type, created_at, updated_at, status |
| 设计参数 | power, voltage, speed, poles, slots, frequency, outer_diameter, inner_diameter, rotor_od, core_length, air_gap, tooth_width, yoke_thickness, shaft_dia, winding_type, turns |
| 性能结果 | efficiency, output_power, input_power, torque, max_temp, winding_temp, total_loss, copper_loss, iron_loss |
| 客户端特有 | solve_mode, privacy_level, task_id, params_json, result_json, notes |

### tasks（任务管理）

| 字段 | 说明 |
|------|------|
| task_uid | 任务唯一标识 (task-{uuid前12位}) |
| project_name | 项目名 |
| motor_type | 电机类型 |
| solve_mode | 求解模式 (local/remote) |
| privacy_level | 隐私级别 (strict/full) |
| status | 状态 (pending/running/completed/failed/cancelled) |
| progress | 进度百分比 |
| total_schemes | 总方案数 |
| completed_schemes | 已完成方案数 |
| error_msg | 错误信息 |
| remote_task_id | 远程任务 ID |

### ai_team_results（AI 团队工作流结果）

| 字段 | 说明 |
|------|------|
| project_id | 项目 ID |
| backend_result_id | 后端结果 ID |
| raw_text | 原始需求文本 |
| motor_type | 电机类型 |
| power_kw / voltage_v / speed_rpm | 核心参数 |
| efficiency / max_temp | 性能指标 |
| specs_json | 规格参数 JSON |
| results_json | 方案结果 JSON |
| review | 评审意见 |
| con_report | 收敛报告 |
| report | 最终报告 |
| recommendation | 推荐建议 |

---

*Motor-AI 管理端 v1.04.02 + 客户端 v1.04.01 | 2026-07-29*
