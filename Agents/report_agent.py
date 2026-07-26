"""
报告撰写员智能体 - 生成客户技术报告
大脑: 本地/云端模型

v1.01 更新:
- 方案对比表扩展为详细参数表 (结构/性能/电磁/损耗/热参数)
- 报告包含推荐方案详细论证和Motor-CAD特性曲线说明
"""
import os
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
from langchain_ollama import ChatOllama

load_dotenv(r"D:\MotorDesign\.env", override=True)

from base_agent import BaseAgent


class ReportAgent(BaseAgent):
    """报告撰写员 - 生成技术报告"""

    def __init__(self):
        super().__init__(
            name="ReportAgent",
            description="生成客户技术报告初稿"
        )
        model = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")
        base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        use_gpu = os.getenv("USE_GPU", "true").lower() == "true"

        self.llm = ChatOllama(
            model=model,
            temperature=0.4,
            base_url=base_url,
            **({"num_gpu": 0} if not use_gpu else {})
        )

        self.cloud_available = bool(os.getenv("DEEPSEEK_API_KEY"))
        if self.cloud_available:
            from openai import OpenAI
            self.cloud_client = OpenAI(
                api_key=os.getenv("DEEPSEEK_API_KEY"),
                base_url=os.getenv("CLOUD_API_BASE", "https://api.deepseek.com")
            )

        self.report_dir = r"D:\MotorDesign\Reports"
        os.makedirs(self.report_dir, exist_ok=True)

    def run(self, specs: dict, results: list, review: str,
            use_cloud: bool = False, **kwargs) -> dict:
        """
        生成技术报告（含详细参数对比表）
        返回: {"report": str, "filepath": str, "filename": str}
        """
        top = results[:5] if results else []
        col_count = len(top)

        # 构建详细对比表格
        header = "| 参数 |" + "|".join(f" 方案{i+1} " for i in range(col_count)) + "|"
        separator = "|---|" + "|".join("---" for _ in range(col_count)) + "|"

        def _v(r, key, default="-"):
            v = r.get(key)
            if v is None or v == "":
                return default
            return str(v)

        rows = [
            f"| **结构参数** |{'|' * col_count}|",
            "| 定子外径(mm) |" + "|".join(f" {_v(r, 'stator_od')} " for r in top) + "|",
            "| 极数 |" + "|".join(f" {_v(r, 'poles')} " for r in top) + "|",
            "| 定子槽数 |" + "|".join(f" {_v(r, 'slots_stator')} " for r in top) + "|",
            "| 转子槽数 |" + "|".join(f" {_v(r, 'slots_rotor')} " for r in top) + "|",
            "| 线圈匝数 |" + "|".join(f" {_v(r, 'turns_per_coil')} " for r in top) + "|",
            f"| **性能参数** |{'|' * col_count}|",
            "| 效率(%) |" + "|".join(f" {_v(r, 'efficiency')} " for r in top) + "|",
            "| 温升(K) |" + "|".join(f" {_v(r, 'max_temp')} " for r in top) + "|",
            "| 转矩(N·m) |" + "|".join(f" {_v(r, 'torque')} " for r in top) + "|",
            "| 功率(kW) |" + "|".join(f" {_v(r, 'power')} " for r in top) + "|",
            f"| **电磁参数** |{'|' * col_count}|",
            "| 定子齿磁密(T) |" + "|".join(f" {_v(r, 'stator_tooth_flux_density')} " for r in top) + "|",
            "| 气隙磁密(T) |" + "|".join(f" {_v(r, 'air_gap_flux_density')} " for r in top) + "|",
            "| 电密(A/mm²) |" + "|".join(f" {_v(r, 'current_density')} " for r in top) + "|",
            "| 功率因数 |" + "|".join(f" {_v(r, 'power_factor')} " for r in top) + "|",
            f"| **损耗参数** |{'|' * col_count}|",
            "| 铜耗(W) |" + "|".join(f" {_v(r, 'copper_loss')} " for r in top) + "|",
            "| 铁耗(W) |" + "|".join(f" {_v(r, 'iron_loss')} " for r in top) + "|",
            "| 总损耗(W) |" + "|".join(f" {_v(r, 'total_loss')} " for r in top) + "|",
            f"| **热参数** |{'|' * col_count}|",
            "| 发热因数 |" + "|".join(f" {_v(r, 'thermal_factor')} " for r in top) + "|",
            "| 铜线温度(°C) |" + "|".join(f" {_v(r, 'copper_temp')} " for r in top) + "|",
        ]
        data_table = header + "\n" + separator + "\n" + "\n".join(rows)

        # 方案描述文本
        data_lines = []
        for i, r in enumerate(top):
            data_lines.append(
                f"方案{i+1}: 外径{r.get('stator_od', '?')}mm, {r.get('poles', '?')}极, "
                f"槽{r.get('slots_stator', '?')}/{r.get('slots_rotor', '?')}, "
                f"匝数{r.get('turns_per_coil', '?')}, "
                f"效率{r.get('efficiency', 0):.2f}%, 温升{r.get('max_temp', 0):.1f}K, "
                f"转矩{r.get('torque', 0):.1f}N·m, "
                f"齿磁密{r.get('stator_tooth_flux_density', '?')}T, "
                f"气隙磁密{r.get('air_gap_flux_density', '?')}T, "
                f"电密{r.get('current_density', '?')}A/mm², "
                f"铜耗{r.get('copper_loss', '?')}W, 铁耗{r.get('iron_loss', '?')}W"
            )
        data_text = "\n".join(data_lines)

        if use_cloud and self.cloud_available:
            response = self.cloud_client.chat.completions.create(
                model=os.getenv("CLOUD_MODEL_NAME", "deepseek-chat"),
                messages=[
                    {"role": "system", "content": "你是电机技术报告撰写专家。请根据以下信息生成一份专业的电机设计方案技术报告。用中文，Markdown格式。"},
                    {"role": "user", "content": f"""需求规格：
- 功率: {specs.get('power_kw')}kW
- 电压: {specs.get('voltage_v')}V
- 转速: {specs.get('speed_rpm', 'N/A')}rpm
- 冷却: {specs.get('cooling')}
- 效率目标: {specs.get('efficiency_target', 'N/A')}%

方案详细数据：
{data_text}

方案对比表：
{data_table}

评审意见：
{review}

请生成报告，包含：
1. 需求概述
2. 方案设计说明（槽配合选择、参数设计原则）
3. 性能对比分析（效率、温升、磁密饱和度、损耗分布）
4. 推荐方案（详细论证，含结构/电磁/热参数）
5. 风险提示
6. 优化建议
7. 结论"""}
                ],
                temperature=0.3
            )
            report_content = response.choices[0].message.content
        else:
            prompt = f"""请根据以下信息生成一份详细的电机设计方案技术报告。用中文，Markdown格式。

需求：{specs.get('power_kw')}kW/{specs.get('voltage_v')}V/{specs.get('cooling')}/{specs.get('application', '通用')}

方案详细数据：
{data_text}

方案对比表：
{data_table}

评审意见：
{review}

报告包含：需求概述、方案设计说明、性能对比分析、推荐方案（详细论证）、风险提示、优化建议、结论。
请从磁密饱和度、电密合理性、损耗分布、热性能等角度进行专业分析。"""
            response = self.llm.invoke(prompt)
            report_content = response.content

        # 保存报告
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"report_{timestamp}.md"
        filepath = os.path.join(self.report_dir, filename)

        header = f"""# 电机设计方案技术报告

**生成时间**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
**项目ID**: {self.project_id or 'N/A'}
**需求**: {specs.get('power_kw')}kW / {specs.get('voltage_v')}V / {specs.get('cooling')} / {specs.get('application', '通用')}

## 方案对比（详细参数表）

{data_table}

---

"""

        full_content = header + report_content

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(full_content)

        return {
            "report": report_content,
            "filepath": filepath,
            "filename": filename
        }
