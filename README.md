# Motor-AI 设计辅助系统（纯 Windows 版）

本地 3B/7B 模型常驻 + 云端大模型按需补充，完全不依赖 WSL/Linux。

## 目录结构

```
D:\MotorDesign\
├── .env                  # 环境配置（模型名、API Key）
├── start.bat             # 一键启动器（双击即可）
├── venv\                 # Python 虚拟环境
├── Scripts\              # 代码
│   ├── motor_tools.py    # Motor-CAD 求解封装（含模拟模式）
│   ├── ai_tools.py       # AI 服务（本地 Ollama + 云端 DeepSeek）
│   ├── db_tools.py       # SQLite 存储与 CSV 导出
│   ├── workflow.py       # 主流程脚本
│   └── requirements.txt  # Python 依赖
├── Templates\            # Motor-CAD 模板文件（.mot）
├── KnowledgeBase\        # SQLite 数据库
└── Reports\              # 生成的报告和 CSV
```

## 快速开始

### 双击启动（推荐）

双击 `D:\MotorDesign\start.bat`，首次运行会自动创建 venv 并安装依赖。

### 命令行启动

```cmd
cd D:\MotorDesign\Scripts
D:\MotorDesign\venv\Scripts\activate.bat
python workflow.py --test    :: 测试模式（2组参数，模拟数据）
python workflow.py --demo    :: 5个用例演示
python workflow.py           :: 交互式（粘贴客户需求）
```

## 三个运行模式

| 模式 | 命令 | 用途 |
|------|------|------|
| 测试 | `python workflow.py --test` | 2组参数 + 模拟数据，验证流程 |
| 演示 | `python workflow.py --demo` | 5个用例，看各种需求解析效果 |
| 交互 | `python workflow.py` | 粘贴客户需求，走完整流程 |

## 配置说明

编辑 `D:\MotorDesign\.env`：

```ini
# 本地模型（3B 下载完后切换）
LOCAL_MODEL_NAME=qwen2.5:7b
USE_GPU=false              # 7B 必须 false（4GB显存不够），3B 可设 true

# 云端模型（填入 DeepSeek API Key 后启用）
DEEPSEEK_API_KEY=sk-your-key-here
```

### 切换到 3B 模型（推荐）

3B 模型下载完成后，编辑 `.env`：
```ini
LOCAL_MODEL_NAME=qwen2.5:3b-instruct-q4_K_M
USE_GPU=true
```

3B 模型只需 2.5GB 显存，可以用 GPU 跑，速度比 7B CPU 模式快 5-10 倍。

## 接入真实 Motor-CAD

1. 安装 Motor-CAD 软件
2. 把你的 `.mot` 模板文件放到 `D:\MotorDesign\Templates\` 目录
3. 打开 `Scripts\motor_tools.py`，修改 `MOTORCAD_PYTHON_PATH` 指向你的 Motor-CAD 安装路径
4. 修改 `_run_motorcad()` 函数中的 `set_variable` 参数名，对照你的模板变量名

没有 Motor-CAD 时，系统自动使用模拟数据，方便测试流程。

## 配置 DeepSeek 云端 AI

1. 去 https://platform.deepseek.com/ 注册
2. 充值 10 元
3. 创建 API Key
4. 编辑 `.env`，填入 `DEEPSEEK_API_KEY=sk-xxx`
5. 在 DeepSeek 控制台勾选"数据不用于模型训练"

配好后，workflow.py 的"云端深度分析"和"报告润色"功能自动启用。

## 数据安全

| 数据类型 | 存储位置 | AI 接触权限 |
|----------|----------|-------------|
| 客户邮件原文 | 本地 | 本地模型可读；云端仅传文字描述 |
| 电机几何模型/图纸 | D:\MotorDesign\Projects\ | 禁止任何 AI 接触 |
| 求解性能数字 | SQLite 数据库 | 脱敏后可发云端分析 |
| 最终报告 | D:\MotorDesign\Reports\ | 本地生成，发布前人工审查 |

调用云端前的脱敏自检：不含槽型尺寸、磁钢厚度、气隙长度、材料牌号、绕组匝数。只保留功率、效率、温升、转矩密度等指标。

## 定时自动运行

用 Windows 任务计划程序，每晚 11 点自动跑：

1. 打开"任务计划程序"（开始菜单搜索）
2. 创建基本任务 → 名称填 "Motor-AI 每日运行"
3. 触发器 → 每天 → 23:00
4. 操作 → 启动程序 → 浏览选择 `D:\MotorDesign\start.bat`
5. 完成

注意：定时运行需要先把客户需求写在 workflow.py 的 `raw_text` 变量里，或者改造为从文件读取。

## 显存管理

T600 只有 4GB 显存：
- 3B 模型：2.5GB 显存，可同时画 CAD
- 7B 模型：4.7GB，超出显存，必须用 CPU 模式（`USE_GPU=false`）
- 跑 Maxwell 大仿真前：托盘右键退出 Ollama，释放显存
- 仿真结束后：重新启动 Ollama 服务

## 验收标准

- [x] 断网状态下本地模型能从需求文字中提取功率、电压、冷却方式
- [x] 脚本能自动跑完至少 20 个方案（模拟模式已验证）
- [x] 一键发送脱敏数据给云端 AI 生成对比分析报告（需配置 Key）
- [x] 所有敏感设计数据从未离开 P15v
