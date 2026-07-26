# Motor-CAD v11.1.5 COM 自动化项目

通过 COM/ActiveX 接口对接 Motor-CAD 11.1.5, 支持电磁分析与热分析的 Python 自动化项目。

## 项目结构

```
motorcad_auto/
├── __init__.py      # 包入口
├── config.py        # 配置: 路径、超时、变量名映射
├── connector.py     # COM 连接 + ActiveX 注册模式验证
├── params.py        # 参数读写 (处理 v11.1.5 元组返回格式)
├── analysis.py      # 热分析 + 电磁分析执行
├── runner.py        # 高层 API: 单次/批量求解
└── cli.py           # 命令行工具
```

## 依赖

- Python 3.10+ (32 位或 64 位均可)
- pywin32 (`pip install pywin32`)
- Motor-CAD v11.1.5 已安装并授权

## 快速开始

### 命令行

```bash
# 检查 Motor-CAD 状态和 ActiveX 模式
python -m motorcad_auto.cli status

# 仅热分析
python -m motorcad_auto.cli thermal --voltage 350 --current 100

# 完整分析 (热 + 电磁)
python -m motorcad_auto.cli full --voltage 350 --current 100

# 批量参数扫描
python -m motorcad_auto.cli batch --count 5 --voltage 350 --current 100
```

### Python API

```python
from motorcad_auto import MotorCADRunner, MotorCADConfig

# 配置
config = MotorCADConfig(
    mot_file=r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot",
)

# 使用上下文管理器 (自动连接/断开)
with MotorCADRunner(config) as runner:
    # 单次分析
    result = runner.run_single(
        params={"DCBusVoltage": 350, "PeakCurrent": 100}
    )
    print(result.summary())

    # 批量分析
    param_list = [
        {"DCBusVoltage": 350, "PeakCurrent": 100},
        {"DCBusVoltage": 360, "PeakCurrent": 110},
    ]
    results = runner.run_batch(param_list)
```

## 关键技术要点

### 1. ActiveX 注册模式

Motor-CAD v11.1.5 菜单 "默认设置 -> 控件注册" 有三个选项:

| 模式 | 菜单 ID | COM 可用 |
|------|---------|----------|
| 多客户端设置 | 93 | 是 |
| 单客户端设置 | 94 | 是 (推荐) |
| 免注册 | 95 | **否** |

当选中 "免注册" 时, Motor-CAD 不注册 COM class factory, 所有 COM 调用失败
(CO_E_SERVER_EXEC_FAILURE / 0x80080005)。

连接器会自动检测并修复此设置 (发送 WM_COMMAND id=94)。

### 2. COM 返回值格式

Motor-CAD v11.1.5 ActiveX 的 `GetVariable` 返回 `(code, value)` 元组:
- `code >= 0`: 成功, `value` 为实际值
- `code == -1`: 失败
- `code == -2`: 变量不存在

`DoSteadyStateAnalysis` / `DoMagneticCalculation` 直接返回 `int`:
- `0`: 成功
- `-1`: 失败

### 3. 变量名白名单

某些不存在的变量名会导致 `GetVariable` 无限挂起。
本项目使用白名单机制, 仅查询已验证的变量名:

- **模型参数**: Program_Version, Program_Module, Pole_Number, Slot_Number, DCBusVoltage, PeakCurrent
- **热分析结果**: T_[Winding_Average], T_[Winding_Max], T_[Housing_Average], MaxTemperature 等
- **电磁分析结果**: TorqueAverage, EMTorque, Efficiency, TotalLoss 等

### 4. 模块限制

- `BPM-Therm` 模块: 仅支持热分析, DoMagneticCalculation 返回 -1
- `BPM-Mag` 模块: 支持电磁分析
- 需要电磁分析时, 请使用包含电磁几何的 .mot 文件

## 与现有系统集成

本项目可替代 `motor_tools.py` 中的 `_run_motorcad()` 函数:

```python
# motor_tools.py 集成示例
from motorcad_auto import MotorCADRunner, MotorCADConfig

def _run_motorcad_v2(params: dict) -> dict:
    config = MotorCADConfig()
    with MotorCADRunner(config) as runner:
        result = runner.run_single(params={
            "DCBusVoltage": params.get("voltage", 350),
            "PeakCurrent": params.get("peak_current", 100),
        })
        # 映射结果
        return {
            "efficiency": result.emag.get("Efficiency", 0),
            "max_temp": result.thermal.get("T_[Winding_Max]", 0),
            "torque": result.emag.get("TorqueAverage", 0),
            # ...
        }
```

## 已知限制

1. COM 对象不能跨线程使用 (STA 模型)
2. GetActiveObject 不可用 (Motor-CAD 不注册到 ROT)
3. Dispatch 会启动新实例 (启动约 60-75 秒)
4. 某些 .mot 文件仅支持热分析 (BPM-Therm 模块)
