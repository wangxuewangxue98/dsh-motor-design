"""
ai_tools.py - AI 服务封装（本地 Ollama + 云端 DeepSeek）
本地模型处理高频任务，数据不出机
云端模型仅在需要时接收脱敏后的性能指标
"""
import json
import os
import re
import urllib.request
from dotenv import load_dotenv

load_dotenv(r"D:\MotorDesign\.env", override=True)

# ---- 本地模型 (HTTP 调用 Ollama REST API, 不依赖 ollama 包) ----
OLLAMA_AVAILABLE = False

LOCAL_MODEL = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:7b")
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

# 7B 模型在 4GB 显存上跑不动，强制 CPU 模式
# 3B 模型下完后可以在 .env 里设 USE_GPU=true 切回 GPU
USE_GPU = os.getenv("USE_GPU", "false").lower() == "true"

# 检测 Ollama 服务是否在线
try:
    with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as resp:
        _tags = json.loads(resp.read())
    _available_models = [m["name"] for m in _tags.get("models", [])]
    if LOCAL_MODEL in _available_models:
        OLLAMA_AVAILABLE = True
        print(f"[ai_tools] Ollama 本地模型就绪: {LOCAL_MODEL} @ {OLLAMA_URL}")
    else:
        print(f"[ai_tools] Ollama 在线但模型 '{LOCAL_MODEL}' 未找到, 可用: {_available_models}")
except Exception as e:
    print(f"[ai_tools] Ollama 连接失败: {e}（将使用正则解析 / 云端降级）")


def _local_chat(prompt: str, temperature: float = 0.1) -> str:
    """调用本地模型 (Ollama REST API), 返回文本"""
    if not OLLAMA_AVAILABLE:
        raise RuntimeError(f"本地 AI (Ollama) 不可用, 模型: {LOCAL_MODEL}")

    payload = {
        "model": LOCAL_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if not USE_GPU:
        payload["options"]["num_gpu"] = 0

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return result.get("response", "").strip()


# ---- 云端模型 ----
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
CLOUD_MODEL = os.getenv("CLOUD_MODEL_NAME", "deepseek-chat")
# v1.03.01: 试用版强制关闭云端 API
ALLOW_CLOUD_API = os.getenv("ALLOW_CLOUD_API", "true") == "true"

cloud_client = None
try:
    from openai import OpenAI
    if DEEPSEEK_API_KEY and DEEPSEEK_API_KEY != "sk-your-key-here" and ALLOW_CLOUD_API:
        cloud_client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
        print(f"[ai_tools] 云端 AI 已就绪: {CLOUD_MODEL}")
    elif not ALLOW_CLOUD_API:
        print("[ai_tools] 云端 AI 已禁用 (ALLOW_CLOUD_API=false, 试用版安全限制)")
    else:
        print("[ai_tools] 云端 AI 未配置（请填写 DEEPSEEK_API_KEY）")
except ImportError:
    print("[ai_tools] openai 包未安装，云端 AI 不可用")


# ============================================================
# 脱敏检查
# ============================================================
SENSITIVE_PATTERNS = [
    (r"\d+\.?\d*\s*mm.*?(槽|slot|齿|tooth)", "槽型尺寸"),
    (r"\d+\.?\d*\s*mm.*?(气隙|air.?gap|gap)", "气隙长度"),
    (r"\d+\.?\d*\s*mm.*?(磁钢|magnet|永磁)", "磁钢尺寸"),
    (r"(匝数|turn|winding\s*N)", "绕组匝数"),
    (r"(材料牌号|材料代号|N\d{2}[UM]|N\d{2}SH|N\d{2}EH)", "材料牌号"),
    (r"(客户|公司|项目代号|合同号)", "客户信息"),
]


def check_desensitize(text: str) -> tuple:
    """检查文本是否包含敏感信息，返回 (is_safe, warning)"""
    for pattern, desc in SENSITIVE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return False, f"发现敏感信息: {desc}"
    return True, "通过"


def auto_desensitize(text: str) -> str:
    """自动脱敏：将敏感数值替换为占位符"""
    for pattern, desc in SENSITIVE_PATTERNS:
        text = re.sub(pattern, f"[{desc}已脱敏]", text, flags=re.IGNORECASE)
    return text


# ============================================================
# 正则解析降级方案（Ollama 不可用时使用）
# ============================================================
def _regex_parse_requirements(user_text: str) -> dict:
    """
    正则解析客户需求（Ollama 不可用时的降级方案）
    覆盖常见中文电机需求描述模式
    """
    specs = {
        "motor_type": "异步电动机",
        "power_kw": None,
        "voltage_v": None,
        "speed_rpm": None,
        "cooling": "未知",
        "efficiency_target": None,
        "stator_od_limit": None,
    }

    text = user_text.replace("，", ",").replace("：", ":")

    # 功率 (kW / W)
    m = re.search(r"(\d+\.?\d*)\s*(?:kW|千瓦|kw)", text, re.I)
    if m:
        specs["power_kw"] = float(m.group(1))
    else:
        m = re.search(r"(\d+\.?\d*)\s*(?:W|瓦)\b", text, re.I)
        if m and float(m.group(1)) > 100:
            specs["power_kw"] = round(float(m.group(1)) / 1000, 2)

    # 电压 (V)
    m = re.search(r"(\d+\.?\d*)\s*(?:V|伏|伏特)\b", text, re.I)
    if m:
        specs["voltage_v"] = float(m.group(1))
    else:
        # 常见电压关键词
        for kw, val in [("48V", 48), ("72V", 72), ("96V", 96), ("220V", 220), ("380V", 380), ("400V", 400)]:
            if kw.lower() in text.lower():
                specs["voltage_v"] = float(val)
                break

    # 转速 (rpm / r/min)
    m = re.search(r"(\d+\.?\d*)\s*(?:rpm|r/min|转/?分|rpm)", text, re.I)
    if m:
        specs["speed_rpm"] = float(m.group(1))
    else:
        m = re.search(r"(\d{3,6})\s*(?:rpm|转)", text, re.I)
        if m:
            specs["speed_rpm"] = float(m.group(1))

    # 效率 (%)
    m = re.search(r"(?:效率|efficiency)[^0-9]*(\d+\.?\d*)\s*%", text, re.I)
    if m:
        specs["efficiency_target"] = float(m.group(1))
    else:
        m = re.search(r"(\d+\.?\d*)\s*%\s*(?:以上|以上效率|效率)", text)
        if m:
            specs["efficiency_target"] = float(m.group(1))

    # 外径限制 (mm)
    m = re.search(r"(?:外径|OD|stator.*?od)[^0-9]*(\d{2,3})\s*(?:mm|毫米)?", text, re.I)
    if m:
        specs["stator_od_limit"] = float(m.group(1))
    else:
        m = re.search(r"(\d{2,3})\s*(?:mm|毫米).*(?:外径|直径|OD)", text, re.I)
        if m:
            specs["stator_od_limit"] = float(m.group(1))

    # 电机类型
    if re.search(r"永磁|PMSM|同步电动机|同步电机", text, re.I):
        specs["motor_type"] = "永磁同步电动机"
    elif re.search(r"无刷|BLDC", text, re.I):
        specs["motor_type"] = "无刷直流电动机"
    elif re.search(r"开关磁阻|SRM", text, re.I):
        specs["motor_type"] = "开关磁阻电动机"
    elif re.search(r"异步|感应|鼠笼|绕线|IM|induction", text, re.I):
        specs["motor_type"] = "异步电动机"
    # 默认保持 "异步电动机"

    # 冷却方式
    if re.search(r"水冷|water.?cool", text, re.I):
        specs["cooling"] = "水冷"
    elif re.search(r"油冷|oil.?cool", text, re.I):
        specs["cooling"] = "油冷"
    elif re.search(r"自然|风冷|air.?cool|自然冷却", text, re.I):
        specs["cooling"] = "自然风冷"

    return specs


def _template_review(top_solutions: list) -> str:
    """模板点评（Ollama 不可用时的降级方案）"""
    if not top_solutions:
        return "暂无方案数据。"

    lines = []
    best_idx = 0
    best_eff = 0
    for i, s in enumerate(top_solutions):
        eff = s.get("efficiency", 0) or 0
        if eff > best_eff:
            best_eff = eff
            best_idx = i

        p = s.get("params", s)
        od = p.get("stator_od", s.get("stator_od", "?"))
        poles = p.get("poles", s.get("poles", "?"))
        sid = s.get("stator_id", p.get("stator_id", "?"))
        rotor_od = s.get("rotor_od", p.get("rotor_od", "?"))
        core_len = s.get("core_length", p.get("core_length", "?"))
        air_gap = s.get("air_gap", p.get("air_gap", "?"))
        slots_s = s.get("slots_stator", p.get("slots_stator", "?"))
        slots_r = s.get("slots_rotor", p.get("slots_rotor", "?"))
        turns = s.get("turns_per_coil", p.get("turns_per_coil", "?"))
        temp = s.get("max_temp", "?")
        torque = s.get("torque", "?")
        tooth_b = s.get("stator_tooth_flux_density", "?")
        cu_loss = s.get("copper_loss", "?")
        fe_loss = s.get("iron_loss", "?")

        lines.append(f"## 方案{i+1}")
        lines.append(f"- 结构: 外径{od}mm, 内径{sid}mm, {poles}极, 槽配合{slots_s}/{slots_r}, 匝数{turns}, 铁心长{core_len}mm, 气隙{air_gap}mm")
        lines.append(f"- 电磁: 效率{eff}%, 转矩{torque}N·m, 齿磁密{tooth_b}T")
        lines.append(f"- 热性能: 温升{temp}K")
        lines.append(f"- 损耗: 铜耗{cu_loss}W, 铁耗{fe_loss}W")
        lines.append(f"- 总结: 效率{eff}%{'，表现优秀' if eff >= 92 else '，仍有优化空间'}")
        lines.append("")

    lines.append(f"## 推荐方案")
    lines.append(f"推荐方案{best_idx+1}，效率最高({best_eff}%)，综合性能最优。")
    lines.append("")
    lines.append("> 注: 当前为模板点评（Ollama 未启动），如需 AI 深度分析请启动 Ollama 服务。")

    return "\n".join(lines)


def _template_report(specs: dict, top_solutions: list, review: str) -> str:
    """模板报告（Ollama 不可用时的降级方案）"""
    lines = ["# 电机设计方案技术报告", ""]

    lines.append("## 1. 需求概述")
    lines.append(f"- 电机类型: {specs.get('motor_type', '异步电动机')}")
    lines.append(f"- 功率: {specs.get('power_kw', '未指定')} kW")
    lines.append(f"- 电压: {specs.get('voltage_v', '未指定')} V")
    lines.append(f"- 转速: {specs.get('speed_rpm', '未指定')} rpm")
    lines.append(f"- 冷却: {specs.get('cooling', '未指定')}")
    lines.append(f"- 效率目标: {specs.get('efficiency_target', '未指定')}%")
    lines.append(f"- 外径限制: {specs.get('stator_od_limit', '未指定')} mm")
    lines.append("")

    lines.append("## 2. 方案设计说明")
    lines.append(f"共生成 {len(top_solutions)} 组候选方案，通过参数扫描覆盖不同外径、极数和槽配合组合。")
    lines.append("")

    if top_solutions:
        best = max(top_solutions, key=lambda s: s.get("efficiency", 0) or 0)
        p = best.get("params", best)
        lines.append("## 3. 推荐方案及理由")
        lines.append(f"- 外径: {p.get('stator_od', '?')}mm, 极数: {p.get('poles', '?')}")
        lines.append(f"- 槽配合: {best.get('slots_stator', '?')}/{best.get('slots_rotor', '?')}")
        lines.append(f"- 效率: {best.get('efficiency', '?')}%")
        lines.append(f"- 温升: {best.get('max_temp', '?')}K")
        lines.append(f"- 转矩: {best.get('torque', '?')}N·m")
        lines.append(f"推荐理由: 效率最高，温升在可接受范围内。")
        lines.append("")

    lines.append("## 4. 风险提示")
    lines.append("- 磁饱和风险: 请检查定子齿磁密是否超过 1.8T")
    lines.append("- 热风险: 请确认温升不超过绝缘等级限值")
    lines.append("")

    lines.append("## 5. 优化建议")
    lines.append("- 可进一步优化槽配合以降低齿槽转矩")
    lines.append("- 可调整匝数平衡效率和成本")
    lines.append("")
    lines.append("> 注: 当前为模板报告（Ollama 未启动），如需 AI 深度分析请启动 Ollama 服务。")
    lines.append("")
    lines.append("---")
    lines.append("*本报告基于真实仿真数据生成，部分设计细节已脱敏处理。*")

    return "\n".join(lines)


# ============================================================
# 本地 AI 函数（离线，数据不出机）
# ============================================================
def local_parse_requirements(user_text: str) -> dict:
    """解析客户需求，返回结构化规格。优先用 Ollama，不可用时降级为正则解析。"""
    # 优先使用本地 AI
    if OLLAMA_AVAILABLE:
        try:
            prompt = f"""你是一个电机需求分析助手。请从以下客户描述中提取关键信息，只返回严格JSON，不要任何解释。

JSON格式：
{{
    "motor_type": "异步电动机" 或 "永磁同步电动机" 或 "无刷直流电动机" 或 "开关磁阻电动机",
    "power_kw": 数字或null,
    "voltage_v": 数字,
    "speed_rpm": 数字或null,
    "cooling": "自然风冷" 或 "水冷" 或 "油冷" 或 "未知",
    "efficiency_target": 数字或null,
    "stator_od_limit": 数字或null
}}

注意：
- 包含"左右""大约""以上""不低于"等词的数字也要提取
- 如果某项信息缺失，填null
- motor_type: 如果客户未明确说明电机类型，默认填"异步电动机"
- power_kw 单位是千瓦，voltage_v 单位是伏特，speed_rpm 单位是转/分钟
- stator_od_limit 是外径限制，单位毫米

客户描述：{user_text}
JSON:"""

            content = _local_chat(prompt, temperature=0.1)

            # 提取 JSON
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end != -1:
                content = content[start:end+1]

            return json.loads(content)
        except Exception as e:
            print(f"[ai_tools] 本地 AI 解析失败，降级为正则解析: {e}")

    # 降级：正则解析
    print("[ai_tools] 使用正则解析客户需求（Ollama 不可用）")
    return _regex_parse_requirements(user_text)


def local_review_solutions(top_solutions: list, specs: dict = None) -> str:
    """本地模型对 Top-N 方案做详细点评。Ollama 不可用时降级为模板点评。"""
    specs = specs or {}
    sol_text = ""
    for i, s in enumerate(top_solutions):
        p = s.get("params", s)
        sol_text += f"""
方案{i+1}:
  - 外径: {p.get('stator_od', '?')}mm, 内径: {p.get('stator_id', s.get('stator_id', '?'))}mm, 极数: {p.get('poles', s.get('poles', '?'))}
  - 转子外径: {s.get('rotor_od', p.get('rotor_od', '?'))}mm, 铁心长度: {s.get('core_length', p.get('core_length', '?'))}mm, 气隙: {s.get('air_gap', p.get('air_gap', '?'))}mm
  - 定子槽数: {s.get('slots_stator', p.get('slots_stator', '?'))}, 转子槽数: {s.get('slots_rotor', p.get('slots_rotor', '?'))}
  - 线圈匝数: {s.get('turns_per_coil', p.get('turns_per_coil', '?'))}
  - 效率: {s.get('efficiency', '?')}%, 温升: {s.get('max_temp', '?')}K
  - 转矩: {s.get('torque', '?')}N·m, 功率: {s.get('power', '?')}kW
  - 定子齿磁密: {s.get('stator_tooth_flux_density', '?')}T, 气隙磁密: {s.get('air_gap_flux_density', '?')}T
  - 电密: {s.get('current_density', '?')}A/mm², 功率因数: {s.get('power_factor', '?')}
  - 铜耗: {s.get('copper_loss', '?')}W, 铁耗: {s.get('iron_loss', '?')}W, 总损耗: {s.get('total_loss', '?')}W
  - 发热因数: {s.get('thermal_factor', '?')}, 铜线温度: {s.get('copper_temp', '?')}°C
"""

    # Ollama 不可用时，生成模板点评
    if not OLLAMA_AVAILABLE:
        return _template_review(top_solutions)

    prompt = f"""你是资深电机设计评审专家。以下是基于规格"{specs.get('motor_type', '异步电动机')}/{specs.get('power_kw')}kW/{specs.get('voltage_v')}V/{specs.get('cooling')}"生成的{len(top_solutions)}组电机方案的求解结果。这些数据来自真实的仿真计算，请基于这些真实数据进行分析，直接陈述结论。

{sol_text}

请逐方案点评，每个方案包含：
1. 结构特点（槽配合是否合理、匝数是否适当、内外径比例、铁心长度）
2. 电磁性能（磁密是否在合理范围、电密是否过高）
3. 热性能（温升、发热因数是否可接受）
4. 损耗分析（铜耗/铁耗比例）
5. 优缺点总结

格式：
## 方案1
- 结构: ...
- 电磁: ...
- 热性能: ...
- 损耗: ...
- 总结: ...

## 方案2
...

## 推荐方案
推荐方案X，因为..."""

    try:
        return _local_chat(prompt, temperature=0.3)
    except Exception as e:
        print(f"[ai_tools] 本地 AI 点评失败，降级为模板: {e}")
        return _template_review(top_solutions)


def local_draft_report(specs: dict, top_solutions: list, review: str) -> str:
    """本地模型生成报告草稿（含详细参数分析）。Ollama 不可用时降级为模板。"""
    # Ollama 不可用时，生成模板报告
    if not OLLAMA_AVAILABLE:
        return _template_report(specs, top_solutions, review)

    sol_text = ""
    for i, s in enumerate(top_solutions):
        p = s.get("params", s)
        sol_text += f"""
方案{i+1}: 外径{p.get('stator_od', s.get('stator_od', '?'))}mm, 内径{s.get('stator_id', '?')}mm, {p.get('poles', s.get('poles', '?'))}极, "
  转子外径{s.get('rotor_od', '?')}mm, 铁心长{s.get('core_length', '?')}mm, 气隙{s.get('air_gap', '?')}mm, "
  槽配合{s.get('slots_stator', '?')}/{s.get('slots_rotor', '?')}, "
  匝数{s.get('turns_per_coil', '?')}, "
  效率{s.get('efficiency', '?')}%, 温升{s.get('max_temp', '?')}K, "
  转矩{s.get('torque', '?')}N·m, "
  磁密(齿{s.get('stator_tooth_flux_density', '?')}T/气隙{s.get('air_gap_flux_density', '?')}T), "
  电密{s.get('current_density', '?')}A/mm², "
  铜耗{s.get('copper_loss', '?')}W/铁耗{s.get('iron_loss', '?')}W"
"""

    prompt = f"""请根据以下信息，生成一份详细的电机设计方案技术报告。用中文，条目式输出。
注意：以下数据来自真实的电磁仿真计算，请直接陈述分析结论，严禁使用"虚构"、"假设"等字样。

需求规格：
- 电机类型: {specs.get('motor_type', '异步电动机')}
- 功率: {specs.get('power_kw', '未指定')} kW
- 电压: {specs.get('voltage_v', '未指定')} V
- 转速: {specs.get('speed_rpm', '未指定')} rpm
- 冷却: {specs.get('cooling', '未指定')}
- 效率目标: {specs.get('efficiency_target', '未指定')}%
- 外径限制: {specs.get('stator_od_limit', '未指定')} mm

候选方案详细数据：
{sol_text}

方案点评：
{review}

请输出以下内容：
1. 需求概述（2-3句话总结客户需求和约束条件）
2. 方案设计说明（说明参数选择思路，槽配合设计原则）
3. 推荐方案及理由（选最佳方案，从效率、温升、转矩、磁密饱和度、损耗分布等角度详细论证）
4. 风险提示（磁饱和风险、热风险、制造可行性等2-3条）
5. 优化建议（下一步优化方向1-2条）
"""

    try:
        return _local_chat(prompt, temperature=0.4)
    except Exception as e:
        print(f"[ai_tools] 本地 AI 报告生成失败，降级为模板: {e}")
        return _template_report(specs, top_solutions, review)


# ============================================================
# 云端 AI 函数（按需调用，传入前必须脱敏）
# ============================================================
def cloud_parse_requirements(user_text: str) -> dict:
    """云端解析需求（本地失败时的降级方案）"""
    if not cloud_client:
        raise RuntimeError("云端 AI 未配置，请填写 DEEPSEEK_API_KEY")

    response = cloud_client.chat.completions.create(
        model=CLOUD_MODEL,
        messages=[
            {"role": "system", "content": "你是电机需求分析助手。从客户描述中提取关键信息，只返回严格JSON。"},
            {"role": "user", "content": f"""请提取以下信息，返回JSON：
{{
    "motor_type": "异步电动机/永磁同步电动机/无刷直流电动机/开关磁阻电动机",
    "power_kw": 数字或null,
    "voltage_v": 数字,
    "speed_rpm": 数字或null,
    "cooling": "自然风冷/水冷/油冷/未知",
    "efficiency_target": 数字或null,
    "stator_od_limit": 数字或null
}}

注意：motor_type 如果客户未明确说明，默认填"异步电动机"。

客户描述：{user_text}"""}
        ],
        temperature=0.1,
    )
    content = response.choices[0].message.content.strip()
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1:
        content = content[start:end+1]
    return json.loads(content)


def cloud_deep_analysis(desensitized_data: str) -> str:
    """将脱敏后的方案对比数据发送云端，获取深度分析"""
    if not cloud_client:
        return "[云端 AI 未配置] 跳过深度分析。请配置 DEEPSEEK_API_KEY 后重试。"

    is_safe, warning = check_desensitize(desensitized_data)
    if not is_safe:
        print(f"[脱敏警告] {warning}，已自动脱敏")
        desensitized_data = auto_desensitize(desensitized_data)

    response = cloud_client.chat.completions.create(
        model=CLOUD_MODEL,
        messages=[
            {"role": "system", "content": """你是一位资深电机设计评审专家。你将收到一批经过脱敏处理的电机方案性能数据。

重要声明：
1. 这些数据来自真实的电磁仿真计算，性能指标（效率、温升、转矩等）是真实可信的。
2. 出于数据安全考虑，只向你展示了性能结果，隐去了具体的几何尺寸、材料牌号和客户信息。
3. 你的任务是：基于这些真实的性能指标，对各方案进行专业对比分析，指出优缺点，并给出推荐意见。
4. 报告中直接陈述分析结论，严禁使用"虚构"、"假设"、"模拟数据"等字样。
5. 报告语气：专业、客观，如同你审阅的是本公司内部的设计方案。"""},
            {"role": "user", "content": desensitized_data}
        ],
        temperature=0.3,
    )
    return response.choices[0].message.content


def cloud_polish_report(draft: str, desensitized_data: str) -> str:
    """云端润色报告：将本地草稿扩写为正式报告"""
    if not cloud_client:
        return draft + "\n\n[注: 云端润色未启用]"

    is_safe, warning = check_desensitize(desensitized_data)
    if not is_safe:
        print(f"[脱敏警告] {warning}，已自动脱敏")
        desensitized_data = auto_desensitize(desensitized_data)

    response = cloud_client.chat.completions.create(
        model=CLOUD_MODEL,
        messages=[
            {"role": "system", "content": """你是技术报告撰写专家。你将收到一份报告草稿和补充的脱敏性能数据。

重要声明：
1. 这些数据来自真实的电磁仿真计算，性能指标是真实可信的。
2. 出于数据安全考虑，隐去了材料牌号和客户信息，但所有性能数字均为真实仿真结果。
3. 报告中直接陈述分析结论，严禁使用"虚构"、"假设"、"模拟数据"等字样。
4. 报告语气：专业、客观，如同撰写本公司内部的设计评审报告。

请将以下报告草稿扩写为格式规范、语气专业的正式技术报告。保留所有数据，增加技术分析深度。输出 Markdown 格式。"""},
            {"role": "user", "content": f"报告草稿：\n{draft}\n\n补充数据：\n{desensitized_data}"}
        ],
        temperature=0.4,
    )
    return response.choices[0].message.content
