"""
报告撰写员智能体 - 生成客户技术报告
大脑: 本地/云端模型

v1.01 更新:
- 方案对比表扩展为详细参数表 (结构/性能/电磁/损耗/热参数)
- 报告包含推荐方案详细论证和Motor-CAD特性曲线说明
"""
import logging
logger = logging.getLogger(__name__)

import os
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
from langchain_ollama import ChatOllama

load_dotenv(r"D:\MotorDesign\.env", override=False)

from base_agent import BaseAgent, extract_ollama_usage
try:
    from workflow_config import get_agent_model
except Exception:
    get_agent_model = lambda name: os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")

# v4.02.104 (用户指令 三.2): 电磁负荷 + 材料利用率计算器 (双路径导入, 缺失时章节自动跳过)
try:
    from material_analysis import compute_all as _ma_compute_all
    from material_analysis import format_md as _ma_format_md
except Exception:
    try:
        from Core.material_analysis import compute_all as _ma_compute_all  # type: ignore
        from Core.material_analysis import format_md as _ma_format_md        # type: ignore
    except Exception:
        _ma_compute_all = None
        _ma_format_md = None


# ═══════════════════════════════════════════════════════════════════════════
# v4.02.103 (P3-C): Markdown → Word 镜像落盘
#   背景: ReportAgent 原仅落 `Reports/report_<ts>.md` (原始产物)。用户以 Word
#         交付为主, 故在 .md 之外镜像一份 .docx 到 `Docs/`。
#   约束: fail-closed —— python-docx 缺失 / 转换异常一律静默跳过并记 warning,
#         绝不影响报告主流程与返回值。
# ═══════════════════════════════════════════════════════════════════════════
import re as _re


def _re_safe_pid(pid: str) -> str:
    """v4.02.104 (问题3): 把 project_id 规范化为可安全用作目录名的字符串。

    仅保留 [A-Za-z0-9_.-], 其余 (含路径分隔符/中文空格) 替换为 '_';
    并剥离前导 '.'/'..' 防目录穿越。超长截断至 80 字符。
    """
    _s = _re.sub(r"[^A-Za-z0-9_.\-]", "_", str(pid or "").strip())
    _s = _s.strip("._")
    return _s[:80]


def _md_inline_runs(paragraph, text):
    """把 `**bold**` 内联标记展开为 runs。"""
    for part in _re.split(r"(\*\*[^*]+\*\*)", text or ""):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            r = paragraph.add_run(part[2:-2])
            r.bold = True
        else:
            paragraph.add_run(part)


def md_to_docx(md_text, out_path, title=None):
    """Markdown 文本 → .docx (标题/表格/列表/粗体)。成功返回 out_path, 失败返回 None。"""
    try:
        from docx import Document
    except Exception as _e:
        logger.warning("[ReportAgent] python-docx 不可用, 跳过 Word 镜像: %s", _e)
        return None
    try:
        doc = Document()
        if title:
            doc.add_heading(str(title), level=0)
        lines = (md_text or "").splitlines()
        i, n = 0, len(lines)
        while i < n:
            raw = lines[i]
            s = raw.strip()
            # ── 表格块 ──
            if s.startswith("|") and s.count("|") >= 2:
                block = []
                while i < n and lines[i].strip().startswith("|"):
                    block.append(lines[i].strip())
                    i += 1
                rows = []
                for b in block:
                    cells = [c.strip() for c in b.strip("|").split("|")]
                    if cells and all(c and set(c) <= set("-: ") for c in cells):
                        continue                      # 分隔行跳过
                    rows.append(cells)
                if rows:
                    cols = max(len(r) for r in rows)
                    tbl = doc.add_table(rows=0, cols=cols)
                    try:
                        tbl.style = "Light Grid Accent 1"
                    except Exception:
                        pass
                    for ri, r in enumerate(rows):
                        cds = tbl.add_row().cells
                        for ci in range(cols):
                            para = cds[ci].paragraphs[0]
                            _md_inline_runs(para, r[ci] if ci < len(r) else "")
                            if ri == 0:
                                for run in para.runs:
                                    run.bold = True
                continue
            # ── 空行 / 分隔线 ──
            if not s:
                i += 1
                continue
            if set(s) <= set("-—_=* ") and len(s) >= 3:
                i += 1
                continue
            # ── 标题 ──
            if s.startswith("### "):
                doc.add_heading(s[4:].strip(), level=3)
            elif s.startswith("## "):
                doc.add_heading(s[3:].strip(), level=2)
            elif s.startswith("# "):
                doc.add_heading(s[2:].strip(), level=1)
            # ── 列表 ──
            elif s.startswith(("- ", "* ", "+ ")):
                _md_inline_runs(doc.add_paragraph(style="List Bullet"), s[2:].strip())
            elif _re.match(r"^\d+[.)]\s+", s):
                _md_inline_runs(doc.add_paragraph(style="List Number"),
                                _re.sub(r"^\d+[.)]\s+", "", s))
            # ── 正文 ──
            else:
                _md_inline_runs(doc.add_paragraph(), s)
            i += 1
        _dir = os.path.dirname(out_path)
        if _dir:
            os.makedirs(_dir, exist_ok=True)
        doc.save(out_path)
        return out_path
    except Exception as e:
        logger.warning("[ReportAgent] md→docx 镜像转换失败: %s", e)
        return None


class ReportAgent(BaseAgent):
    """报告撰写员 - 生成技术报告"""

    def __init__(self):
        super().__init__(
            name="ReportAgent",
            description="生成客户技术报告初稿"
        )
        # v4.00.006: 模型改为每次调用时动态读取, 支持运行时切换
        self.llm = None

        # v4.02.06 H-3 二期 T4: 云端客户端改走统一工厂 (BaseAgent._get_cloud_client),
        # 不再硬编码 deepseek OpenAI 构建 (移除重复构建, 行为不变: 默认仍为 deepseek-chat).
        # cloud_model/cloud_available 由 BaseAgent 工厂辅助方法解析 (惰性, 缺密钥/门控在调用时降级).
        self.cloud_model = self._resolve_cloud_model()
        self.cloud_available = self._cloud_usable()

        # v4.02.104 (问题3): 交付目录迁移 —— 原为固定 `D:\MotorDesign\Reports` /
        #   `D:\MotorDesign\Docs`, 多项目并行时同名/仅时间戳文件互相覆盖、难以追溯。
        #   现统一落到 `D:\MotorDesign\data\reports\<project_id>\`(按项目隔离),
        #   文件名带项目号与轮次 (见 run() 中的 filename 构造)。
        #   兼容: project_id 为空时回退到旧目录, 不改变历史产物位置。
        self.report_dir = r"D:\MotorDesign\Reports"      # 旧目录 (仅作空 project 回退)
        # v4.02.103 (P3-C): Word 镜像输出目录 (交付形态)
        self.docx_dir = r"D:\MotorDesign\Docs"           # 旧目录 (仅作空 project 回退)
        self.deliverables_root = r"D:\MotorDesign\data\reports"
        self._attempt_index = 1                          # v4.02.104: 当前轮次 (由 run() 覆盖)

    # ==================== v4.02.104 (问题3): 交付目录解析 ====================
    def _deliverable_dir(self, project_id: str = "") -> str:
        """返回本项目专属交付目录: ``data/reports/<project_id>``。

        project_id 为空 ⇒ 回退旧目录 (``Reports``), 保证不产生 ``data/reports/<空>``。
        目录不存在时自动创建; 创建失败回退旧目录 (fail-open, 不影响主流程)。
        """
        pid = (project_id or self.project_id or "").strip()
        if not pid:
            return self.report_dir
        # 防目录穿越: 仅保留安全字符
        _safe = _re_safe_pid(pid)
        if not _safe:
            return self.report_dir
        _d = os.path.join(self.deliverables_root, _safe)
        try:
            os.makedirs(_d, exist_ok=True)
            return _d
        except Exception:
            return self.report_dir

    def _get_llm(self):
        """每次调用时动态创建 LLM, 管理端修改模型配置后立即生效 (v5.1 委托 BaseAgent 集中治理 num_ctx/keep_alive)"""
        try:
            return super()._get_llm(temperature=0.4)
        except Exception as e:
            logger.warning("[ReportAgent] LLM 创建失败: %s", e)
            return None
        os.makedirs(self.report_dir, exist_ok=True)

    # ==================== v4.06-P2: 权威电气工作点 ====================
    @staticmethod
    def _resolve_operating_point(specs: dict) -> dict:
        """解析「求解链路实际使用」的电气工作点, 供报告提示词锚定。

        根因: 报告提示词此前不含 frequency_hz/极数/同步速/转差, LLM 只能按工频
          50Hz 臆测 → 产出「6极同步转速1000rpm / 转差率7.5%」这类与真实求解
          (12.9Hz / 同步 258rpm / 3.1%) 矛盾的叙述。此处把确定性推导结果显式化。

        取值优先级: specs 已有值 > 由 f=p·n/(120·(1−s)) 反推 > 空(不臆造)。
        异步机默认转差 3%, 与 Core/shared_knowledge 及 motor_tools.
        resolve_electrical_frequency 保持一致。
        """
        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        mtype = str(specs.get("motor_type") or "")
        is_sync = ("永磁" in mtype or "同步" in mtype or "PMSM" in mtype.upper())

        poles = _f(specs.get("poles"))
        speed = _f(specs.get("speed_rpm"))
        freq = _f(specs.get("frequency_hz"))
        sync = _f(specs.get("sync_speed_rpm"))
        slip = _f(specs.get("slip"))

        # 频率兜底: 异步机假设 3% 转差 (同步机无转差)
        if freq is None and poles and speed:
            denom = 120.0 * (1.0 if is_sync else 0.97)
            freq = round(poles * speed / denom, 1) if denom else None
        # 同步速 = 120f/p
        if sync is None and freq and poles:
            sync = round(120.0 * freq / poles, 1)
        # 转差率 = (n_sync − n_rated) / n_sync
        if slip is None and sync and speed:
            slip = round((sync - speed) / sync, 4) if sync else None

        return {"poles": poles, "speed": speed, "frequency": freq,
                "sync": sync, "slip": slip, "is_sync": is_sync}

    def _build_operating_point_block(self, op: dict) -> str:
        """把电气工作点渲染成提示词片段 (含"禁止按 50Hz 假设"的硬约束)。"""
        bits = []
        if op.get("poles") is not None:
            bits.append("- 极数: %g 极" % op["poles"])
        if op.get("speed") is not None:
            bits.append("- 额定转速: %g rpm" % op["speed"])
        if op.get("frequency") is not None:
            note = "（同步电机, 无转差）" if op.get("is_sync") else "（变频驱动, **非工频 50Hz**）"
            bits.append("- 额定频率: %g Hz%s" % (op["frequency"], note))
        if op.get("sync") is not None:
            bits.append("- 同步转速: %g rpm（= 120×f/p = 120×%g/%g）"
                        % (op["sync"], op.get("frequency"), op.get("poles")))
        if op.get("slip") is not None:
            if op.get("is_sync"):
                bits.append("- 转差率: 0%（同步电机）")
            else:
                bits.append("- 额定转差率: %.2f%%" % (op["slip"] * 100.0))
        if not bits:
            return ""

        block = ("电气工作点（★以下为求解链路实际使用的权威数值，报告正文凡涉及"
                 "频率/同步转速/转差率必须直接引用，严禁按工频 50Hz 自行假设）：\n"
                 + "\n".join(bits))
        f = op.get("frequency")
        if f is not None and not op.get("is_sync") and f < 45:
            block += ("\n※ 该电机为低频大转矩变频驱动，同步转速与 50Hz 工频值无关；"
                      "不得出现「50Hz 同步转速」或据此推断的转差率。")
        return block


    def run(self, specs: dict, results: list, review: str,
            use_cloud: bool = False, **kwargs) -> dict:
        """
        生成技术报告（含详细参数对比表）
        返回: {"report": str, "filepath": str, "filename": str}

        v3.00.28: 支持 project_id/timing_summary/human_reviews kwargs,
        报告尾部追加: AI 团队流程耗时统计 + 人工审核意见汇总
        """
        project_id = kwargs.get("project_id", "")
        timing_summary = kwargs.get("timing_summary", {})
        human_reviews = kwargs.get("human_reviews", [])
        # v4.02.104 (问题3): 轮次 (方案重生第几轮) —— 由 orchestrator 传入, 用于
        #   文件名 <project_id>_R<round>_... ; 缺省/非法值回落 1。
        try:
            self._attempt_index = max(1, int(kwargs.get("attempt_index") or 1))
        except (TypeError, ValueError):
            self._attempt_index = 1
        # v4.02.104 (问题3): 让交付目录以**工作流** project_id 为键 (与人工检查点/
        #   成本事件同一 id), 而非 BaseAgent 自增的临时 id。
        if project_id:
            self.project_id = project_id
        # v4.02.104 (问题4): 质量门禁结果 (含电磁负荷/材料利用率) —— 供报告披露章节
        _qg_result = kwargs.get("quality_gate_result") or {}
        top = results[:10] if results else []
        col_count = len(top)

        # v4.06-P2: 权威电气工作点 (供对比表 + 提示词锚定, 详见 _resolve_operating_point)
        op = self._resolve_operating_point(specs)

        def _sync_of(r):
            """同步转速 = 120·f/p (优先取该方案自身的 frequency_hz)"""
            f = r.get("frequency_hz") or op.get("frequency")
            p = r.get("poles") or op.get("poles")
            try:
                return "%.0f" % (120.0 * float(f) / float(p))
            except (TypeError, ValueError, ZeroDivisionError):
                return "-"

        def _freq_of(r):
            f = r.get("frequency_hz") or op.get("frequency")
            try:
                return "%g" % float(f)
            except (TypeError, ValueError):
                return "-"

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
            "| 定子内径(mm) |" + "|".join(f" {_v(r, 'stator_id')} " for r in top) + "|",
            "| 转子外径(mm) |" + "|".join(f" {_v(r, 'rotor_od')} " for r in top) + "|",
            "| 铁心长度(mm) |" + "|".join(f" {_v(r, 'core_length')} " for r in top) + "|",
            "| 气隙长度(mm) |" + "|".join(f" {_v(r, 'air_gap')} " for r in top) + "|",
            "| 极数 |" + "|".join(f" {_v(r, 'poles')} " for r in top) + "|",
            "| 定子槽数 |" + "|".join(f" {_v(r, 'slots_stator')} " for r in top) + "|",
            "| 转子槽数 |" + "|".join(f" {_v(r, 'slots_rotor')} " for r in top) + "|",
            "| 线圈匝数 |" + "|".join(f" {_v(r, 'turns_per_coil')} " for r in top) + "|",
            f"| **性能参数** |{'|' * col_count}|",
            "| 效率(%) |" + "|".join(f" {_v(r, 'efficiency')} " for r in top) + "|",
            "| 温升(K) |" + "|".join(f" {_v(r, 'max_temp')} " for r in top) + "|",
            "| 转矩(N·m) |" + "|".join(f" {_v(r, 'torque')} " for r in top) + "|",
            "| 功率(kW) |" + "|".join(f" {_v(r, 'power')} " for r in top) + "|",
            # v4.06-P2: 暴露电气工作点, 避免正文按 50Hz 臆测同步速/转差
            "| 额定频率(Hz) |" + "|".join(f" {_freq_of(r)} " for r in top) + "|",
            "| 同步转速(rpm) |" + "|".join(f" {_sync_of(r)} " for r in top) + "|",
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
                f"方案{i+1}: 外径{r.get('stator_od', '?')}mm, 内径{r.get('stator_id', '?')}mm, "
                f"转子外径{r.get('rotor_od', '?')}mm, 铁心长{r.get('core_length', '?')}mm, "
                f"气隙{r.get('air_gap', '?')}mm, {r.get('poles', '?')}极, "
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

        # v4.06-P2: 电气工作点提示词片段 (频率/同步速/转差 权威锚定)
        operating_point = self._build_operating_point_block(op)

        # v4.02.07 H-3 二期 T1: 经系统级网关路由 (云端优先, 失败自动降级本地, 对调用方透明)
        llm = self._get_llm_or_cloud(temperature=0.3) if (use_cloud and self.cloud_available) else None
        if llm is not None:
            response = llm.invoke([
                {"role": "system", "content": """你是电机技术报告撰写专家。你将收到经过脱敏处理的真实仿真数据和方案对比表。

重要声明：
1. 这些数据来自真实的电磁仿真计算，性能指标是真实可信的。
2. 出于数据安全考虑，隐去了材料牌号和客户信息，但所有性能数字均为真实仿真结果。
3. 报告中直接陈述分析结论，严禁使用"虚构"、"假设"、"模拟数据"等字样。
4. 报告语气：专业、客观，如同撰写本公司内部的设计评审报告。
5. 严禁按工频 50Hz 推断频率/同步转速/转差率：这类参数必须原样引用用户消息中
   给出的「电气工作点」；变频驱动低速电机的同步转速与 50Hz 值无关。
6. 引用同步转速/转差率时必须与「电气工作点」完全一致，不得自行换算或估计。

请根据以下信息生成一份专业的电机设计方案技术报告。用中文，Markdown格式。"""},
                {"role": "user", "content": f"""需求规格：
- 电机类型: {specs.get('motor_type', '异步电动机')}
- 功率: {specs.get('power_kw')}kW
- 电压: {specs.get('voltage_v')}V
- 转速: {specs.get('speed_rpm', 'N/A')}rpm
- 冷却: {specs.get('cooling')}
- 效率目标: {specs.get('efficiency_target', 'N/A')}%

{operating_point}

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
            ])
            # v4.06-P1: 捕获真实 token 用量 (云端 _CloudAIMessage / 本地 ChatOllama usage_metadata, 云/地通用)
            _ti, _to = extract_ollama_usage(response)
            report_content = response.content
        else:
            # v4.00.006: 每次调用时动态获取 LLM (支持管理端运行时切换模型)
            llm = self._get_llm()
            if llm is None:
                return {"error": "LLM 不可用"}
            prompt = f"""请根据以下信息生成一份详细的电机设计方案技术报告。用中文，Markdown格式。

需求：{specs.get('motor_type', '异步电动机')}, {specs.get('power_kw')}kW/{specs.get('voltage_v')}V/{specs.get('cooling')}/{specs.get('application', '通用')}

{operating_point}

方案详细数据：
{data_text}

方案对比表：
{data_table}

评审意见：
{review}

报告包含：需求概述、方案设计说明、性能对比分析、推荐方案（详细论证）、风险提示、优化建议、结论。
请从磁密饱和度、电密合理性、损耗分布、热性能等角度进行专业分析。"""
            response = llm.invoke(prompt)
            # v4.06-P1: 捕获本地真实 token 用量 (ChatOllama usage_metadata)
            _ti, _to = extract_ollama_usage(response, prompt)
            report_content = response.content

        # 保存报告
        # v4.02.104 (问题3): 交付目录按项目隔离 + 文件名带项目号与轮次
        #   <project_id>_R<round>_report_<timestamp>.md
        #   例: PROJ_20260921_143022_R2_report_20260921_143500.md
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        _out_dir = self._deliverable_dir(project_id)
        _pid_tag = _re_safe_pid(project_id) or "PROJ"
        _rnd_tag = "R%d" % int(self._attempt_index or 1)
        filename = f"{_pid_tag}_{_rnd_tag}_report_{timestamp}.md"
        filepath = os.path.join(_out_dir, filename)

        header = f"""# 电机设计方案技术报告

**生成时间**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
**项目ID**: {self.project_id or 'N/A'}
**电机类型**: {specs.get('motor_type', '异步电动机')}
**需求**: {specs.get('power_kw')}kW / {specs.get('voltage_v')}V / {specs.get('cooling')} / {specs.get('application', '通用')}

## 电气工作点（权威值）

{self._build_operating_point_block(op) or '（未解析到频率/同步速，正文不再推断）'}

## 方案对比（详细参数表）

{data_table}

---

"""

        full_content = header + report_content

        # v3.00.28: 附加 AI 团队流程耗时统计 + 人工审核意见汇总章节
        extra_sections = ""
        if timing_summary:
            extra_sections += "\n\n" + self._format_timing_section(timing_summary)
        if human_reviews:
            extra_sections += "\n\n" + self._format_human_reviews_section(human_reviews)
        # v4.02.104 (用户指令 三.2): 电磁负荷 + 材料利用率 —— 由程序计算并写入报告
        #   (此前 RUN32 报告该两章标注「程序未直接提供, 单独计算」, 数值不可复现)
        try:
            _ma_md = self._format_material_section(specs, top, _qg_result)
            if _ma_md:
                extra_sections += "\n\n" + _ma_md
        except Exception as _mae:
            logger.warning("[ReportAgent] 电磁负荷/材料利用率章节跳过: %s", _mae)
        full_content += extra_sections

        # 添加报告尾部声明
        footer = "\n\n---\n*本报告基于真实仿真数据生成，部分设计细节已脱敏处理。*\n"
        full_content += footer

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(full_content)

        # v4.02.103 (P3-C): 追加 Word 镜像落盘 (fail-closed, 不影响主产物)
        # v4.02.104 (问题3): 与 .md 同目录 (data/reports/<project_id>/), 文件名同带项目号+轮次
        docx_path = None
        try:
            docx_name = "%s_%s_技术报告_%s.docx" % (_pid_tag, _rnd_tag, timestamp)
            docx_path = md_to_docx(
                full_content, os.path.join(_out_dir, docx_name),
                title="电机设计方案技术报告")
            if docx_path:
                logger.info("[ReportAgent] Word 镜像已落盘: %s", docx_path)
        except Exception as _de:
            logger.warning("[ReportAgent] Word 镜像跳过: %s", _de)
            docx_path = None

        return {
            "report": report_content + extra_sections,
            "filepath": filepath,
            "filename": filename,
            "docx_filepath": docx_path,      # v4.02.103 (P3-C)
            # v4.06-P1: 真实 token 计量 (报告生成 LLM), 供成本审计全链路真实化
            "tokens_in": _ti,
            "tokens_out": _to,
        }

    # ==================== v3.00.28: 报告附加章节 ====================
    def _format_material_section(self, specs: dict, results: list,
                                 qg_result: Optional[dict] = None) -> str:
        """v4.02.104 (用户指令 三.2): 生成「电磁负荷 + 材料利用率」章节。

        方案来源优先级: 门禁选中快照 (qg_result['_best_snapshot']) > results[0]。
        使用门禁快照可保证报告数字与质量门禁评估的是同一方案 (口径不分裂)。
        计算器不可用/数据不足时返回空串 (不产出「未知」占位)。
        """
        if _ma_compute_all is None or _ma_format_md is None:
            return ""
        _qg = qg_result if isinstance(qg_result, dict) else {}
        best = _qg.get("_best_snapshot")
        if not isinstance(best, dict) or not best:
            best = results[0] if results else {}
        if not best:
            return ""
        return _ma_format_md(_ma_compute_all(best, specs or {}, _qg))

    def _format_timing_section(self, timing_summary: dict) -> str:
        """生成 AI 团队流程耗时统计章节"""
        if not timing_summary:
            return ""
        phases = timing_summary.get("phases", [])
        total = timing_summary.get("total", {})
        if not phases and not total:
            return ""

        lines = ["## AI 团队流程耗时统计结果及分析", ""]
        lines.append("本次 AI 团队工作流各阶段时间消耗统计如下:")
        lines.append("")
        lines.append("| 阶段 | 系统自动耗时 | 人工耗时 | 阶段小计 | Token用量 |")
        lines.append("|------|--------------|----------|----------|-----------|")

        def _fmt_ms(ms: int) -> str:
            if not ms:
                return "—"
            seconds = ms / 1000
            if seconds < 60:
                return f"{seconds:.1f}秒"
            minutes = seconds / 60
            rem_seconds = int((ms % 60000) / 1000)
            if minutes < 60:
                return f"{minutes:.1f}分{rem_seconds}秒"
            return f"{minutes/60:.1f}小时{minutes % 60:.0f}分"

        for p in phases:
            phase = p.get("phase", "—")
            sys_ms = p.get("system_ms", 0)
            hum_ms = p.get("human_ms", 0)
            sub_ms = p.get("subtotal_ms", sys_ms + hum_ms)
            tokens = p.get("tokens_total", 0)
            lines.append(f"| {phase} | {_fmt_ms(sys_ms)} | {_fmt_ms(hum_ms)} | "
                         f"{_fmt_ms(sub_ms)} | {tokens/1000:.1f}K |")

        # 总计行
        sys_total = total.get("system_ms", 0)
        hum_total = total.get("human_ms", 0)
        sub_total = total.get("subtotal_ms", sys_total + hum_total)
        tok_total = total.get("tokens_total", 0)
        lines.append(f"| **流程总耗时** | **{_fmt_ms(sys_total)}** | "
                     f"**{_fmt_ms(hum_total)}** | **{_fmt_ms(sub_total)}** | "
                     f"**{tok_total/1000:.1f}K** |")
        lines.append("")
        lines.append("**耗时分析:**")
        lines.append("")
        lines.append(f"- **系统自动耗时** = AI 大模型推理 + 计算机求解(电磁仿真等)耗时, "
                     f"本次总计 **{_fmt_ms(sys_total)}**")
        lines.append(f"- **人工耗时** = 人工确认/审核检查点等待决策耗时, "
                     f"本次总计 **{_fmt_ms(hum_total)}**")
        if sub_total:
            sys_ratio = sys_total / sub_total * 100
            hum_ratio = hum_total / sub_total * 100
            lines.append(f"- 系统耗时占比 {sys_ratio:.1f}%, 人工耗时占比 {hum_ratio:.1f}%")
        if tok_total:
            lines.append(f"- AI Token消耗总计 **{tok_total/1000:.1f}K**, "
                         f"展示了 AI 团队的推理强度")
        lines.append("")
        return "\n".join(lines)

    def _format_human_reviews_section(self, human_reviews: list) -> str:
        """生成人工审核意见汇总及分析章节"""
        if not human_reviews:
            return ""

        lines = ["## 流程中人工审核意见汇总及分析", ""]
        lines.append(f"本次 AI 团队工作流共触发 **{len(human_reviews)}** 个人工审核检查点,")
        lines.append("审核详情如下:")
        lines.append("")
        lines.append("| # | 检查点类型 | 标题 | 决策 | 决策来源 | 状态 | 用户备注 |")
        lines.append("|---|------------|------|------|----------|------|----------|")

        decision_cn = {
            "approve": "批准", "reject": "反对", "override": "覆盖",
            "modify": "修改", "confirm": "确认", "ignore": "忽略",
            "hold": "暂缓", "redo": "重做", "end": "结束",
            "auto_pass": "自动通过", "auto_confirm": "自动确认",
            "auto_first": "自动选择第一", "auto_reject": "自动反对",
            "auto_medium": "自动中庸", "auto_skip": "自动跳过",
            "auto_hold": "自动暂缓", "auto_ignore": "自动忽略",
            "auto_end": "自动结束", "auto_redo": "自动重做",
        }

        # v4.02.104 (问题2): 判定判据统一为**结构化字段 decision_type**
        #   旧写法依赖 decision 的 "auto_" 前缀 —— 但 approve 经 _normalize_decision
        #   归一化后已无该前缀 ⇒ 人工主动批准与超时自动放行在报告里被混为一谈
        #   (人工主动决策数 / 超时放行率 双失真)。现以
        #   human_checkpoints.decision_type ∈ {manual, timeout} 为唯一判据;
        #   兼容老数据: decision_type 为空时退回原 "auto_" 前缀 / status 判据。
        def _is_timeout(cp: dict) -> bool:
            _dt = cp.get("decision_type")
            if _dt in ("manual", "timeout"):
                return _dt == "timeout"
            return (cp.get("status") == "timeout"
                    or (cp.get("decision") or "").startswith("auto_"))

        for i, cp in enumerate(human_reviews, 1):
            cp_type = cp.get("checkpoint_type", "—")
            title = cp.get("title", "—")
            decision = cp.get("decision", "")
            status = cp.get("status", "—")
            comment = (cp.get("comment") or "").replace("\n", " ").replace("|", "/")[:80]
            decision_disp = decision_cn.get(decision, decision or "—")
            _src = "人工主动" if not _is_timeout(cp) else "超时自动"
            if not cp.get("decision"):
                _src = "—"
            lines.append(f"| {i} | {cp_type} | {title} | {decision_disp} | {_src} | "
                         f"{status} | {comment or '—'} |")

        decided = [cp for cp in human_reviews if not _is_timeout(cp)
                   and cp.get("decision")]
        timeout_cps = [cp for cp in human_reviews if _is_timeout(cp)]
        rejected_cps = [cp for cp in human_reviews if cp.get("decision") == "reject"]
        redo_cps = [cp for cp in human_reviews if cp.get("decision") == "redo"]

        lines.append("")
        lines.append("**审核行为分析:**")
        lines.append("")
        lines.append(f"- **总审核检查点**: {len(human_reviews)} 个")
        lines.append(f"- **人工主动决策**: {len(decided)} 次 "
                     f"({len(decided)/max(len(human_reviews),1)*100:.0f}%)")
        if timeout_cps:
            lines.append(f"- **超时自动默认**: {len(timeout_cps)} 次 "
                         f"({len(timeout_cps)/max(len(human_reviews),1)*100:.0f}%) — "
                         f"建议关注AI预判质量或缩短超时阈值")
        if rejected_cps:
            lines.append(f"- **驳回决策**: {len(rejected_cps)} 次, "
                         f"AI 推荐方案可能未完全匹配用户预期")
        if redo_cps:
            lines.append(f"- **重做决策**: {len(redo_cps)} 次, "
                         f"部分环节需要重新生成以满足要求")
        lines.append("")
        # 列出详细审核意见
        detail_comments = [(cp.get("title"), cp.get("comment"))
                           for cp in human_reviews
                           if cp.get("comment") and not _is_timeout(cp)]
        if detail_comments:
            lines.append("**详细审核意见:**")
            lines.append("")
            for title, comment in detail_comments:
                lines.append(f"- **{title}**: {comment}")
            lines.append("")
        return "\n".join(lines)
