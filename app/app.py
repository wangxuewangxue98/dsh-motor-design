"""
Motor-AI Web 界面后端
版本: 1.03.01 | 更新时间: 2026-07-27 22:00
Flask 应用，提供所有 API 端点，连接现有的 ai_tools / motor_tools / db_tools 模块

v1.03.01: 环境分离 (dev/trial) + 多租户预留
"""
import os
import sys
import json
import glob
import queue
import traceback
import threading as _sse_thread
import time as _sse_time
from datetime import datetime
import logging
logger = logging.getLogger(__name__)
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, g

# ============================================================
# 环境配置 (v1.03.01: 从 config.env 加载)
# ============================================================
# 将项目根目录加入 sys.path 以导入 config 包
_PROJECT_ROOT = r"D:\MotorDesign"
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config.env import Config

# v4.01.030: 启动即加载当前环境的 .env (dev/trial), 确保 os.environ 含
# LOCAL_MODEL_NAME 等变量, 避免 SSE/配置接口回退到硬编码默认模型
try:
    from dotenv import load_dotenv
    _DOTENV_OK = True
except Exception:
    _DOTENV_OK = False
    load_dotenv = None  # v4.02.38: 显式绑定 None, 避免 L1773/L3036 触发 NameError

# 兼容性别名: 旧代码引用 SYSTEM_NAME / SYSTEM_VERSION 等
SYSTEM_NAME = Config.SYSTEM_NAME
SYSTEM_VERSION = Config.SYSTEM_VERSION
VERSION_DATE = Config.VERSION_DATE
# ============================================================
# 路径配置 (从 Config 获取)
# ============================================================
BASE_DIR = Config.BASE_DIR
SCRIPTS_DIR = Config.SCRIPTS_DIR
AGENTS_DIR = Config.AGENTS_DIR
CORE_DIR = Config.CORE_DIR
KNOWLEDGE_DIR = Config.KNOWLEDGE_DIR
REPORTS_DIR = Config.REPORTS_DIR

sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, AGENTS_DIR)
sys.path.insert(0, CORE_DIR)
sys.path.insert(0, KNOWLEDGE_DIR)

# .env 文件路径 (向后兼容: 读写网络配置时使用当前环境的 .env)
ENV_PATH = os.path.join(BASE_DIR, f".env.{Config.ENV}")
if not os.path.exists(ENV_PATH):
    ENV_PATH = os.path.join(BASE_DIR, ".env")

# v4.01.030: 进程启动时把 .env 变量载入 os.environ。
# 语义改为 override=False (#858 修复): 已存在的进程环境变量优先于 .env (docker -e /
# systemd / 测试脚本注入的 ALLOW_CLOUD_API=false 应被尊重); .env 仅填补进程环境
# 未设置的项。修复前 override=True 会把运行期注入的 ALLOW_CLOUD_API=false 重新覆盖
# 回 .env 的 true, 致云端门控失效。LOCAL_MODEL_NAME 一致性由 _resolve_local_model_name()
# 直接读 .env.<ENV> 文件保证, 不依赖 os.environ, 故 override=False 不影响其可用性。
if _DOTENV_OK:
    load_dotenv(ENV_PATH, override=False)


# v4.01.030: 调用时解析当前模型名 (不依赖进程启动时的 os.environ)。
# 直接读取已解析的 .env.<ENV> 文件为首选, 因为 ai_tools 等模块在导入时会再次
# load_dotenv() 加载基础 .env 并覆盖 os.environ 中的 LOCAL_MODEL_NAME, 导致
# os.getenv 取到被污染的旧默认值。文件读取保证 SSE model 字段、
# /api/admin/workflow-config 的 default_model、管理端显示三处一致。
def _resolve_local_model_name():
    # 1) 首选: 直接从 .env.<ENV> 文件读取 (不被 os.environ 污染)
    try:
        _p = ENV_PATH
        if os.path.exists(_p):
            with open(_p, "r", encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _, _v = _line.partition("=")
                    if _k.strip() == "LOCAL_MODEL_NAME":
                        _v = _v.strip().strip('"').strip("'")
                        if _v:
                            return _v
    except Exception:
        pass
    # 2) 兜底: 环境变量的显式覆盖 (若用户在进程外直接 export)
    val = os.getenv("LOCAL_MODEL_NAME")
    if val:
        return val
    return "qwen2.5:3b-instruct-q4_K_M"

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"),
    static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
)
app.config["JSON_AS_ASCII"] = False

# P1 (2026-08-24): 请求体上限护栏。
# 根因: werkzeug/serving.py 中 self.rfile.read(10_000_000) 对请求体做 10MB 缓冲分配,
# 无 MAX_CONTENT_LENGTH 时进程内存耗尽 -> MemoryError -> flask_dev exit code=3221225725(栈溢出)。
# 设 16MB 上限: 超限请求直接 413, 不进入 10MB 缓冲分配, 根除该 MemoryError 路径。
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

# ============================================================
# v2.02.01: 多租户认证 — before_request 钩子在模块导入后定义
# (见下方 AUTH_OK 定义之后)
# ============================================================

# 环境信息注入模板上下文
@app.context_processor
def _inject_env_info():
    return {
        "ENV_LABEL": Config.env_label(),
        "ENV_COLOR": Config.env_color(),
        "ENV_DESC": Config.env_desc(),
        "VERSION_LABEL": Config.VERSION_LABEL,
    }

# ============================================================
# 导入现有模块（带容错）
# ============================================================
AI_OK = True
AI_ERR = ""
try:
    import ai_tools
    from ai_tools import (
        local_parse_requirements, local_review_solutions, local_draft_report,
        cloud_parse_requirements, cloud_deep_analysis, cloud_polish_report,
        check_desensitize, auto_desensitize,
        ai_chat, get_available_models,
    )
except Exception as e:
    AI_OK = False
    AI_ERR = str(e)

MOTOR_OK = True
try:
    import motor_tools
    from motor_tools import (
        generate_param_matrix, run_one_design, batch_solve,
        get_solve_log, get_last_summary, reset_solve_log,
        import_motorcad_result, import_motorcad_csv,
        export_params_csv, generate_motorcad_guide,
        semi_auto_solve_start, semi_auto_solve_status, semi_auto_solve_cancel,
        check_solver_status,
    )
except Exception as e:
    MOTOR_OK = False

DB_OK = True
try:
    from db_tools import (
        init_db, save_result, get_all_results, get_top_solutions, export_csv, get_stats,
        get_user_by_username, get_user_by_email, get_user_by_id, create_user,
        get_all_users, update_user, get_product_lines, get_product_line_by_id,
        create_project, get_projects_by_user, get_project_by_id,
        save_ai_team_result, get_ai_team_results, get_ai_team_result,
    )
    init_db()
    # v4.03: 案例电机数据中心表初始化
    try:
        import sys as _sys
        _sys.path.insert(0, CORE_DIR)
        from case_motor_db import init_case_motor_tables
        init_case_motor_tables()
    except Exception as _cm_err:
        print(f"[app] 案例电机表初始化失败: {_cm_err}")
except Exception as e:
    DB_OK = False

# ---- v2.02.01: 认证模块 ----
AUTH_OK = True
try:
    from auth import (
        load_user_context, generate_token, verify_token, hash_password,
        verify_password, require_auth, require_role, require_admin,
        current_user_id, current_user_dict, get_membership_info,
        extract_token_from_request, is_path_public, ROLE_LEVELS, MEMBERSHIP_LEVELS,
        is_dev_auto_login, should_show_admin_hint,
        can_use_host_motorcad, check_motorcad_permission,
    )
except Exception as e:
    AUTH_OK = False
    print(f"[WARNING] 认证模块加载失败: {e}")

# ---- v2.02.01: 积分服务 ----
POINTS_OK = True
try:
    from points_service import (
        add_points, spend_points, get_balance, get_points_history,
        check_daily_checkin, do_daily_checkin,
        POINTS_DAILY_CHECKIN, POINTS_SOLVE_WEB,
    )
except Exception as e:
    POINTS_OK = False

# ---- v2.02.01: 注册 before_request 用户上下文钩子 ----
@app.before_request
def _load_user_context():
    if AUTH_OK:
        load_user_context()
    else:
        g.user = None

# ---- 新增模块：多智能体 / 审计 / 进化引擎 / 反馈 ----
AGENT_OK = True
AGENT_ERR = ""
try:
    from orchestrator import MotorAIOrchestrator
    _orchestrator = MotorAIOrchestrator()
except Exception as e:
    AGENT_OK = False
    AGENT_ERR = str(e)
    _orchestrator = None

# v4.00.004: AI 团队工作流集中配置
WC_OK = True
try:
    sys.path.insert(0, r"D:\MotorDesign\Core")
    from workflow_config import load_config as _wc_load
    from workflow_config import save_config as _wc_save
    from workflow_config import get_config as _wc_get
    from workflow_config import DEFAULT_WORKFLOW_CONFIG as _WC_DEFAULT
except Exception as e:
    WC_OK = False
    print(f"[app] workflow_config 模块加载失败: {e}")
    _wc_load = None
    _wc_save = None
    _wc_get = None
    _WC_DEFAULT = {}

AUDIT_OK = True
try:
    import audit_logger
except Exception:
    AUDIT_OK = False

# v4.00.013: AI 流程设置 (5 段流程策略 + 量化评分权重模板 + 版本回滚)
AFC_OK = True
try:
    import ai_flow_config as _afc
except Exception as e:
    AFC_OK = False
    print(f"[app] ai_flow_config 模块加载失败: {e}")
    _afc = None

# v4.02.06 H-3 二期 S1: 云服务(API)配置
CSC_OK = True
try:
    import cloud_services_config as _csc
except Exception as e:
    CSC_OK = False
    print(f"[app] cloud_services_config 模块加载失败: {e}")
    _csc = None

EVOLUTION_OK = True
try:
    from evolution_engine import get_evolution_dashboard, create_skill_template, mine_design_rule
except Exception:
    EVOLUTION_OK = False

FEEDBACK_OK = True
try:
    from feedback_store import record_feedback, get_feedback_stats, get_recent_feedback
except Exception:
    FEEDBACK_OK = False

FLYWHEEL_OK = True
try:
    from flywheel import on_adopt as _flywheel_adopt, on_reject as _flywheel_reject, get_flywheel_status
except Exception as e:
    FLYWHEEL_OK = False

# ---- v2.05.03: 成本核算 / 运营分析师 / 人工检查点 ----
COST_OK = True
COST_ERR = ""
try:
    from cost_tracker import get_project_cost_report, format_report_markdown
except Exception as e:
    COST_OK = False
    COST_ERR = str(e)

ANALYST_OK = True
ANALYST_ERR = ""
try:
    from operation_analyst import analyze_cost_report
except Exception as e:
    ANALYST_OK = False
    ANALYST_ERR = str(e)

HUMAN_CP_OK = True
HUMAN_CP_ERR = ""
try:
    import human_checkpoint as hc
except Exception as e:
    HUMAN_CP_OK = False
    HUMAN_CP_ERR = str(e)

# ---- v4.03 (F4): 工作流作业管理器 — 工作流与 SSE 解耦 ----
#   工作流在独立后台线程执行, 事件落入带 event_id 的内存缓冲;
#   SSE 连接只是订阅者, 断开不再终止工作流 (含检查点等待/超时放行)。
JOB_MGR_OK = True
JOB_MGR_ERR = ""
try:
    from workflow_job_manager import get_job_manager as _get_job_manager
    _job_manager = _get_job_manager()
except Exception as e:
    JOB_MGR_OK = False
    JOB_MGR_ERR = str(e)
    _job_manager = None

# ---- v4.03 (F3): 启动「孤儿检查点」清扫线程 (与工作流线程解耦) ----
#   防止工作流线程随断流/进程重启消失后, 其 pending 检查点无人放行导致 UI 僵尸弹窗。
if HUMAN_CP_OK and os.environ.get("MOTORAI_DISABLE_HC_SWEEPER", "") not in ("1", "true", "True"):
    try:
        hc.start_sweeper()
    except Exception as _e:
        print(f"[app] 启动检查点清扫器失败: {_e}")

# ---- v4.02.76: 几何截图存储治理 (改进方案 v1 的方案 A: TTL 7 天 + 数量上限 1000) ----
#   背景: Scripts/rmxprt_images/ 此前无任何清理策略, 2 天从 131 张/980MB 涨到 152 张/1150MB。
#   策略: mtime 超 TTL 删除 → 仍超数量上限则按最旧优先裁 → 回收空 run 目录;
#         宽限 grace(默认 60min) 内的文件一律跳过, 保护本轮工作流正在引用的图。
#   实现: Core/geometry_image_store.py (线程模型镜像 hc.start_sweeper)。
GEOM_STORE_OK = True
GEOM_STORE_ERR = ""
try:
    _core_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Core")
    if _core_dir not in sys.path:
        sys.path.insert(0, _core_dir)
    import geometry_image_store as _geom_store
except Exception as _e:
    _geom_store = None
    GEOM_STORE_OK = False
    GEOM_STORE_ERR = str(_e)
    print(f"[app] 几何图存储模块加载失败(忽略): {_e}")

if GEOM_STORE_OK:
    try:
        _geom_store.start_image_sweeper()
    except Exception as _e:
        print(f"[app] 启动几何图清扫器失败: {_e}")


def _geom_store_status():
    """几何截图存储现状 (供 /api/status 可观测性出口; 失败返回 {'available': False})。"""
    if not GEOM_STORE_OK:
        return {"available": False, "error": GEOM_STORE_ERR}
    try:
        out = _geom_store.image_store_stats()
        out["available"] = True
        return out
    except Exception as _e:
        return {"available": False, "error": str(_e)}

# ---- v4.01.029: 运营质量指标 (人工修正率 / 返工率) ----
QM_OK = True
QM_ERR = ""
try:
    from quality_metrics import (
        get_project_metrics as qm_project_metrics,
        get_global_metrics as qm_global_metrics,
        promote_corrections_to_questions as qm_promote_questions,
    )
except Exception as e:
    QM_OK = False
    QM_ERR = str(e)

# ---- v1.03.02: 全局进度广播 ----
PROGRESS_HUB_OK = True
try:
    from progress_hub import progress_hub
except Exception:
    PROGRESS_HUB_OK = False
    progress_hub = None

# ---- v1.03.03: 全局任务管理 ----
TASK_MANAGER_OK = True
try:
    from task_manager import (
        create_task, update_task, get_task, get_active_tasks,
        get_all_tasks, get_tasks_by_status, cancel_task as _cancel_task,
        update_task_priority as _update_task_priority,
        cleanup_interrupted_tasks as _cleanup_interrupted,
        STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED,
        STATUS_INTERRUPTED,
        TYPE_SOLVE, TYPE_WORKFLOW,
    )
except Exception as e:
    TASK_MANAGER_OK = False
    print(f"[app] task_manager 加载失败: {e}")

# v2.05.04: 启动时清理上次未完成的任务 (服务重启后标记为中断)
if TASK_MANAGER_OK:
    try:
        _interrupted_count = _cleanup_interrupted()
        if _interrupted_count > 0:
            print(f"[app] 启动清理: {_interrupted_count} 个中断任务已标记")
    except Exception as _e:
        print(f"[app] 启动清理失败: {_e}")


# ============================================================
# v1.03.02: SSE 流包装器 — 拦截事件并广播到 ProgressHub
# ============================================================
def _publish_stream(gen):
    """包装 SSE 生成器, 将每个事件发布到 progress_hub, 实现跨设备同步"""
    for line in gen:
        if PROGRESS_HUB_OK and line.startswith("data: "):
            try:
                event = json.loads(line[6:].strip())
                progress_hub.publish(event)
            except Exception:
                pass
        yield line
    if PROGRESS_HUB_OK:
        progress_hub.finish()


def _iter_sse_events(chunk_iter):
    """把 generate() 产出的 SSE 文本行解析回事件 dict, 并广播到 ProgressHub。

    v4.03 (F4) 说明: 为把工作流迁出响应生成器, 需要一个「事件对象」出口。此处保留
    原 generate() 的全部事件构造逻辑**逐字不变**(约 200 行、已长期验证), 只在其外层
    把 'data: {...}' 文本还原为事件对象交给作业缓冲 —— 相比重写事件构造, 这样不会
    引入契约漂移; JSON 往返在数百级事件量下开销可忽略。
    """
    for line in chunk_iter:
        if not line or not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        try:
            event = json.loads(payload)
        except Exception:
            continue
        if PROGRESS_HUB_OK and progress_hub:
            try:
                progress_hub.publish(event)
            except Exception:
                pass
        yield event


def _sse_from_job(job, from_event_id=0):
    """把作业事件缓冲以 SSE 文本流输出。

    断流(生成器被关闭)只会终止本生成器, **不影响作业线程** —— 这是 F4 的核心:
    工作流不再依附于某一条 SSE 连接。重连时从 from_event_id 之后续传, 不重复投递。
    """
    for ev in job.subscribe(from_event_id):
        yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"


def _run_streaming_with_heartbeat(stream_gen, heartbeat_interval=15):
    """将 orchestrator.run_streaming 的逐个 update 透传, 并在空闲超过 heartbeat_interval
    秒时注入心跳哨兵 ({"type": "__heartbeat__"}), 防止代理/客户端因空闲超时静默断流。

    v4.02.20 (#1083 根因③): 原主 workflow SSE 流无心跳, 单次 RMxprt 求解阻塞期间代理/
    客户端静默断流且无自愈 → UI 冻结至 5 分钟轮询兜底。改用后台生产者线程迭代
    run_streaming, 主生成器在队列空闲超时后注入心跳, 并透传生产者异常 (不静默吞掉)。
    """
    _out = queue.Queue()

    def _producer():
        try:
            for _u in stream_gen:
                _out.put(_u)
        except Exception as _pe:  # 生产者异常也透传, 不静默吞掉
            _out.put({"type": "__producer_error__", "error": str(_pe)})
        finally:
            _out.put(None)  # stop sentinel

    _t = _sse_thread.Thread(target=_producer, daemon=True)
    _t.start()
    while True:
        try:
            _u = _out.get(timeout=heartbeat_interval)
        except queue.Empty:
            yield {"type": "__heartbeat__"}
            continue
        if _u is None:
            break
        yield _u


# ============================================================
# 辅助函数
# ============================================================
def read_env_file():
    """读取 .env 文件，返回字典"""
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip()
    return env


def write_env_file(updates: dict):
    """更新 .env 文件中的指定键值"""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            existing = f.read()
    else:
        existing = ""

    updated_keys = set()
    for line in existing.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                lines.append(f"{key}={updates[key]}")
                updated_keys.add(key)
            else:
                lines.append(line)
        else:
            lines.append(line)

    # 追加新键
    for key, value in updates.items():
        if key not in updated_keys:
            lines.append(f"{key}={value}")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ============================================================
# 路由：页面
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")


# ============================================================
# API：系统状态
# ============================================================
@app.route("/api/status")
def api_status():
    """获取系统全状态"""
    status = {
        "system": {
            "name": SYSTEM_NAME,
            "version": SYSTEM_VERSION,
            "version_label": Config.VERSION_LABEL,
            "version_date": VERSION_DATE,
        },
        "env": Config.to_dict(),
        "ollama": {"running": False, "model": "", "gpu_mode": False, "models": []},
        "cloud": {"configured": False, "model": "", "allowed": False, "vendor": ""},
        "db": {"available": DB_OK, "total": 0, "max_efficiency": 0, "avg_efficiency": 0, "path": Config.DB_PATH},
        "motorcad": {"available": False, "simulation_mode": True, "version": "11.1.5", "exe_path": ""},
        "auth": {"enabled": AUTH_OK, "user": current_user_dict() if AUTH_OK else None,
                 "auto_login": AUTH_OK and is_dev_auto_login() if AUTH_OK else False,
                 "admin_hint": AUTH_OK and should_show_admin_hint() if AUTH_OK else False},
        "points": {"enabled": POINTS_OK},
        "geometry_images": _geom_store_status(),
        "errors": [],
    }

    # Ollama 状态 (用 HTTP 请求, 不依赖 ollama Python 包)
    try:
        import urllib.request
        ollama_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        with urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=3) as resp:
            tags_data = json.loads(resp.read())
        model_list = [m["name"] for m in tags_data.get("models", [])]
        status["ollama"]["running"] = True
        status["ollama"]["models"] = model_list
        status["ollama"]["model"] = _resolve_local_model_name()
        status["ollama"]["gpu_mode"] = os.getenv("USE_GPU", "false").lower() == "true"
    except Exception as e:
        status["errors"].append(f"Ollama: {e}")

    # 云端 API (v4.02.07 修复 2026-08-25: 改读云服务配置 _csc 单一真源,
    # 不再依赖 .env 占位 DEEPSEEK_API_KEY; 与 cloud_services_config 设计一致)
    try:
        from Core import cloud_services_config as _csc_mod
        from Core.cloud_llm import cloud_gated as _cloud_gated
    except Exception:
        _csc_mod = None
        _cloud_gated = None
    if _cloud_gated is not None:
        status["cloud"]["allowed"] = _cloud_gated() is None
    if _csc_mod is not None:
        _cred = _csc_mod.get_vendor_credential("deepseek") or {}
        if (_cred.get("api_key") or "").strip():
            status["cloud"]["configured"] = True
            status["cloud"]["model"] = _cred.get("default_model") or os.getenv("CLOUD_MODEL_NAME", "deepseek-chat")
            status["cloud"]["vendor"] = "deepseek"
    # v4.02.85: 自定义厂商卡 (custom:*) 或其它内置卡启用且有密钥时, 同样视为"云端已配置"。
    # 原实现硬编码只查 deepseek -> 用户只用自定义卡时 /api/status 恒报未配置, 与真实路由口径不一致。
    # 保守扩展: 仅在上一步未命中时兜底, 只会 false -> true, 不会把既有 true 改坏 (零回归)。
    if _csc_mod is not None and not status["cloud"]["configured"]:
        try:
            for _vn, _v in ((_csc_mod.get_all() or {}).get("vendors") or {}).items():
                if not _v.get("enabled", False):
                    continue
                if not ((_v.get("api_key") or "").strip()):
                    continue
                status["cloud"]["configured"] = True
                status["cloud"]["model"] = ((_v.get("default_model") or "").strip()
                                            or os.getenv("CLOUD_MODEL_NAME", ""))
                status["cloud"]["vendor"] = _vn
                break
        except Exception as e:  # pragma: no cover
            logger.debug("[api/status] 云端配置兜底扫描失败: %s", e)
    # 回退: .env 真实密钥 (DEEPSEEK_API_KEY 非占位) 仍能标志已配置
    if not status["cloud"]["configured"]:
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if api_key and api_key != "sk-your-key-here":
            status["cloud"]["configured"] = True
            status["cloud"]["model"] = os.getenv("CLOUD_MODEL_NAME", "deepseek-chat")
            status["cloud"]["vendor"] = status["cloud"]["vendor"] or "deepseek(env)"

    # 数据库
    if DB_OK:
        try:
            stats = get_stats()
            status["db"]["total"] = stats.get("total", 0)
            status["db"]["max_efficiency"] = stats.get("max_efficiency", 0)
            status["db"]["avg_efficiency"] = stats.get("avg_efficiency", 0)
        except Exception:
            pass

    # Motor-CAD
    if MOTOR_OK:
        mc_status = motor_tools.check_motorcad_status()
        status["motorcad"]["available"] = mc_status.get("com_connected", False)
        status["motorcad"]["installed"] = mc_status.get("installed", False)
        status["motorcad"]["simulation_mode"] = not status["motorcad"]["available"]
        status["motorcad"]["version"] = mc_status.get("version", "11.1.5")
        status["motorcad"]["exe_path"] = mc_status.get("exe_path", "")
        status["motorcad"]["progid"] = mc_status.get("progid", "MotorCAD.MotorCAD")
        status["motorcad"]["com_connected"] = mc_status.get("com_connected", False)
        status["motorcad"]["template_exists"] = mc_status.get("template_exists", False)
        status["motorcad"]["template_path"] = mc_status.get("template_path", "")
        status["motorcad"]["template_dir"] = mc_status.get("template_dir", "")
        status["motorcad"]["is_demo"] = mc_status.get("is_demo", False)
        status["motorcad"]["automation_supported"] = mc_status.get("automation_supported", True)
        status["motorcad"]["licence_note"] = mc_status.get("licence_note", "")
        status["motorcad"]["use_mode"] = motor_tools.MOTORCAD_USE_MODE
        status["motorcad"]["com_skip"] = motor_tools._com_skip
        status["motorcad"]["last_fallback_reason"] = mc_status.get("last_fallback_reason", "")
        # 手动模式标志
        if motor_tools.MOTORCAD_USE_MODE == "manual":
            status["motorcad"]["manual_mode"] = True
            status["motorcad"]["manual_guide"] = "在 Motor-CAD GUI 中手动求解后，通过结果导入功能上传 CSV 文件"
        else:
            status["motorcad"]["manual_mode"] = False

    if not AI_OK:
        status["errors"].append(f"AI模块: {AI_ERR}")

    return jsonify(status)


# ============================================================
# API：环境信息 (v1.03.01)
# ============================================================
@app.route("/api/env")
def api_env():
    """返回当前环境配置信息"""
    return jsonify(Config.to_dict())


# ============================================================
# API：需求解析
# ============================================================
@app.route("/api/parse", methods=["POST"])
def api_parse():
    """本地 AI 解析客户需求"""
    data = request.get_json()
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "请输入客户需求描述"}), 400

    try:
        specs = local_parse_requirements(text)
        return jsonify({"specs": specs})
    except Exception as e:
        # 尝试云端降级
        try:
            if ai_tools.cloud_client:
                specs = cloud_parse_requirements(text)
                return jsonify({"specs": specs, "source": "cloud"})
        except Exception:
            pass
        return jsonify({"error": f"解析失败: {e}"}), 500


# ============================================================
# API：参数矩阵预览
# ============================================================
@app.route("/api/preview-params", methods=["POST"])
def api_preview_params():
    """预览参数扫描矩阵"""
    data = request.get_json()
    specs = data.get("specs", {})
    count = data.get("count", 30)

    try:
        params, _ = generate_param_matrix(specs, count=count)
        return jsonify({"params": params, "count": len(params)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# API：批量求解（SSE 流式）
# ============================================================
@app.route("/api/solve", methods=["POST"])
def api_solve():
    """批量求解，返回 SSE 流"""
    data = request.get_json()
    specs = data.get("specs", {})
    count = data.get("count", 30)
    raw_text = data.get("raw_text", "")

    # v1.03.03: 创建全局任务记录
    client_id = request.headers.get("User-Agent", "unknown")[:60]
    _task_id = None
    if TASK_MANAGER_OK:
        _task_id = create_task(
            TYPE_SOLVE, source=Config.ENV, specs={"specs": specs, "count": count}
        )

    # v1.03.02: 启动进度广播
    # v1.04.15: 求解作业使用独立 job_id ⇒ 取消按作业隔离, 不再误杀同进程并发作业
    _solve_job_id = ("task_%s" % _task_id) if _task_id else ("solve_%d" % int(_sse_time.time() * 1000))
    if PROGRESS_HUB_OK:
        progress_hub.start("solve", client=client_id, total=count, job_id=_solve_job_id)
        progress_hub.register_job(_solve_job_id, kind="solve",
                                  task_id=_task_id, client=client_id)

    def generate():
        # v1.04.15: 绑定本求解作业(job_id)到当前线程 ⇒ is_cancelled() 仅反映本作业
        if PROGRESS_HUB_OK:
            progress_hub.bind_job(_solve_job_id)
        try:
            if TASK_MANAGER_OK and _task_id:
                update_task(_task_id, status=STATUS_RUNNING, current_step="初始化求解",
                           log_entry=f"求解任务启动: {count} 个方案")
            if MOTOR_OK:
                reset_solve_log()

            param_list, _ = generate_param_matrix(specs, count=count)
            total = len(param_list)
            results = []

            use_mode = motor_tools.MOTORCAD_USE_MODE if MOTOR_OK else "auto"
            mc_connector = None
            mc_available = False
            connect_reason = ""

            # ---- 决定求解模式 ----
            if use_mode == "simulation":
                connect_reason = "MOTORCAD_USE_MODE=simulation，使用模拟数据"
                motor_tools._log("MOTORCAD_USE_MODE=simulation，跳过 COM 调用")
            elif use_mode == "manual":
                connect_reason = "MOTORCAD_USE_MODE=manual，等待手动导入"
                motor_tools._log("MOTORCAD_USE_MODE=manual，跳过 COM 调用")
            elif MOTOR_OK and motor_tools._motorcad_available:
                # 尝试建立 COM 连接 (仅一次, 复用于所有方案)
                # 先发送 connecting 事件, 让前端显示连接进度
                yield f"data: {json.dumps({'type': 'connecting', 'message': '正在连接 Motor-CAD COM (约 2-3 分钟, 请耐心等待)...'}, ensure_ascii=False, default=str)}\n\n"

                try:
                    mc_connector = motor_tools._get_motorcad_instance()
                    if mc_connector is not None:
                        mc_available = True
                        motor_tools._log("COM 连接成功, 将复用于所有方案")
                    else:
                        connect_reason = "Motor-CAD COM 连接返回 None"
                        motor_tools._com_skip = True
                except Exception as e:
                    connect_reason = f"Motor-CAD COM 连接失败: {e}"
                    motor_tools._log(f"COM 连接失败: {e}", "warn")
                    motor_tools._log("将使用模拟模式求解全部方案", "warn")
                    motor_tools._com_skip = True
                    mc_connector = None
            else:
                connect_reason = "Motor-CAD 未安装或 motor_tools 不可用"

            # 发送开始事件
            start_data = {
                'type': 'start',
                'total': total,
                'motorcad_available': mc_available,
                'use_mode': use_mode,
            }
            if not mc_available and use_mode not in ("simulation", "manual"):
                start_data['warning'] = connect_reason or "Motor-CAD COM 不可用，使用模拟模式"
            elif use_mode == "simulation":
                start_data['warning'] = connect_reason
            elif use_mode == "manual":
                start_data['warning'] = connect_reason

            yield f"data: {json.dumps(start_data, ensure_ascii=False, default=str)}\n\n"

            # ---- 逐个求解 (复用同一 COM 连接) ----
            for i, params in enumerate(param_list):
                # v1.03.02: 检查取消标志
                if PROGRESS_HUB_OK and progress_hub.is_cancelled():
                    if TASK_MANAGER_OK and _task_id:
                        update_task(_task_id, status=STATUS_CANCELLED,
                                   progress=i/total if total > 0 else 0,
                                   current_step=f"用户终止 (已完成 {i}/{total})")
                    yield f"data: {json.dumps({'type': 'cancelled', 'message': f'用户已终止求解 (已完成 {i}/{total})', 'completed': i, 'total': total}, ensure_ascii=False, default=str)}\n\n"
                    break
                try:
                    if mc_available and mc_connector is not None:
                        # 使用已有连接求解
                        try:
                            result = motor_tools._run_motorcad_with_instance(mc_connector, params)
                            result["solve_mode"] = "motorcad"
                            if MOTOR_OK:
                                motor_tools._last_summary["motorcad"] += 1
                        except Exception as e:
                            reason = str(e)
                            motor_tools._log(f"[{i+1}/{total}] COM 求解失败: {reason}", "warn")
                            # 单个方案失败, 降级为模拟
                            result = motor_tools._run_simulated(params)
                            result["solve_mode"] = "simulation"
                            result["fallback_reason"] = reason
                            if MOTOR_OK:
                                motor_tools._last_summary["simulation"] += 1
                                if not motor_tools._last_summary["fallback_reason"]:
                                    motor_tools._last_summary["fallback_reason"] = reason
                    elif use_mode == "manual":
                        result = motor_tools._run_simulated(params)
                        result["solve_mode"] = "pending"
                        result["fallback_reason"] = "MOTORCAD_USE_MODE=manual，请手动导入"
                        if MOTOR_OK:
                            motor_tools._last_summary["simulation"] += 1
                    else:
                        # 模拟模式
                        result = motor_tools._run_simulated(params)
                        result["solve_mode"] = "simulation"
                        if MOTOR_OK:
                            motor_tools._last_summary["simulation"] += 1
                            if not motor_tools._last_summary["fallback_reason"]:
                                motor_tools._last_summary["fallback_reason"] = connect_reason

                    result["params"] = params
                    result["index"] = i + 1
                    results.append(result)

                    # 存入数据库
                    if DB_OK:
                        save_params = params.copy()
                        save_params["power_kw"] = specs.get("power_kw")
                        save_result(save_params, result, user_id=current_user_id())

                    yield f"data: {json.dumps({'type': 'progress', 'index': i + 1, 'total': total, 'result': result}, ensure_ascii=False, default=str)}\n\n"

                    # v1.03.03: 更新任务进度
                    if TASK_MANAGER_OK and _task_id:
                        update_task(_task_id, progress=(i + 1) / total,
                                   current_step=f"求解 {i+1}/{total}",
                                   log_entry=f"方案 {i+1}/{total} 完成: 效率={result.get('efficiency', '?')}%")

                except Exception as e:
                    yield f"data: {json.dumps({'type': 'error', 'index': i + 1, 'total': total, 'error': str(e)})}\n\n"
                    if TASK_MANAGER_OK and _task_id:
                        update_task(_task_id, log_entry=f"方案 {i+1} 错误: {str(e)}")

            # ---- 断开 COM 连接 (释放资源) ----
            if mc_connector is not None:
                try:
                    mc_connector.disconnect()
                    motor_tools._log("Motor-CAD COM 连接已断开")
                except Exception:
                    pass

            # 求解摘要
            summary = get_last_summary() if MOTOR_OK else {}
            solve_logs = get_solve_log() if MOTOR_OK else []
            mc_count = summary.get("motorcad", 0)
            sim_count = summary.get("simulation", 0)
            fallback_reason = summary.get("fallback_reason", "")

            yield f"data: {json.dumps({
                'type': 'done',
                'total': total,
                'count': len(results),
                'summary': {
                    'motorcad_count': mc_count,
                    'simulation_count': sim_count,
                    'fallback_reason': fallback_reason,
                    'all_simulation': mc_count == 0 and sim_count > 0,
                    'use_mode': motor_tools.MOTORCAD_USE_MODE if MOTOR_OK else 'auto',
                },
                'solve_logs': solve_logs[-10:] if solve_logs else [],
            }, ensure_ascii=False, default=str)}\n\n"

            # v1.03.03: 任务完成
            if TASK_MANAGER_OK and _task_id:
                _cancelled = PROGRESS_HUB_OK and progress_hub and progress_hub.is_cancelled()
                update_task(_task_id,
                           status=STATUS_CANCELLED if _cancelled else STATUS_COMPLETED,
                           progress=1.0 if not _cancelled else None,
                           current_step="已完成" if not _cancelled else "已终止",
                           result_summary=f"MC:{mc_count}/Sim:{sim_count}/Total:{len(results)}")

        except Exception as e:
            yield f"data: {json.dumps({'type': 'fatal', 'error': str(e)})}\n\n"
            if TASK_MANAGER_OK and _task_id:
                update_task(_task_id, status=STATUS_FAILED,
                           current_step=f"错误: {str(e)[:100]}")
        finally:
            # v1.04.15: 解绑 + 注销本求解作业(取消隔离, 防注册表泄漏)
            if PROGRESS_HUB_OK:
                progress_hub.unbind_job()
                progress_hub.unregister_job(_solve_job_id)

    return Response(
        stream_with_context(_publish_stream(generate())),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# API：手动导入 Motor-CAD 求解结果
# ============================================================
@app.route("/api/motorcad/import", methods=["POST"])
def api_motorcad_import():
    """导入单组或多组 Motor-CAD 手动求解结果"""
    if not MOTOR_OK:
        return jsonify({"error": "Motor-CAD 模块未加载"}), 500

    data = request.get_json()
    specs = data.get("specs", {})

    # 批量 CSV 导入
    csv_path = data.get("csv_path")
    if csv_path:
        try:
            imported = import_motorcad_csv(csv_path)
            saved_ids = []
            for res in imported:
                params = res.get("params", {})
                params["power_kw"] = specs.get("power_kw", params.get("power_kw"))
                sid = save_result(params, res, user_id=current_user_id())
                saved_ids.append(sid)
            return jsonify({
                "success": True,
                "imported_count": len(imported),
                "saved_ids": saved_ids,
                "solve_mode": "manual_import",
            })
        except Exception as e:
            return jsonify({"error": f"CSV 导入失败: {str(e)}"}), 500

    # 单条 JSON 导入
    params = data.get("params", {})
    result_data = data.get("result", {})
    if not params or not result_data:
        return jsonify({"error": "缺少 params 或 result 字段"}), 400

    try:
        res = import_motorcad_result(params, result_data)
        save_params = params.copy()
        save_params["power_kw"] = specs.get("power_kw", params.get("power_kw"))
        sid = save_result(save_params, res, user_id=current_user_id())
        return jsonify({
            "success": True,
            "saved_id": sid,
            "result": res,
            "solve_mode": "manual_import",
        })
    except Exception as e:
        return jsonify({"error": f"导入失败: {str(e)}"}), 500


# ============================================================
# API：导出参数 CSV 和手动操作指南
# ============================================================
@app.route("/api/motorcad/export-params", methods=["POST"])
def api_motorcad_export_params():
    """导出参数矩阵到 CSV 文件，供用户在 Motor-CAD GUI 中手动设置参数"""
    if not MOTOR_OK:
        return jsonify({"error": "Motor-CAD 模块未加载"}), 500

    data = request.get_json()
    specs = data.get("specs", {})
    count = data.get("count", 30)

    try:
        param_list, _ = generate_param_matrix(specs, count)
        csv_path = export_params_csv(param_list)
        return jsonify({
            "success": True,
            "csv_path": csv_path,
            "param_count": len(param_list),
            "params_preview": param_list[:5],
            "message": f"已导出 {len(param_list)} 组参数到 {csv_path}",
        })
    except Exception as e:
        return jsonify({"error": f"导出失败: {str(e)}"}), 500


@app.route("/api/motorcad/guide", methods=["GET"])
def api_motorcad_guide():
    """获取 Motor-CAD 手动求解操作指南"""
    if not MOTOR_OK:
        return jsonify({"error": "Motor-CAD 模块未加载"}), 500

    try:
        guide = generate_motorcad_guide()
        return jsonify(guide)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# 半自动求解工作流 API
# ============================================================
# 参数映射: 内部参数名 -> Motor-CAD .mot 变量名
_PARAM_TO_MOT = {
    "stator_od": "Stator_Lamination_Dia",
    "poles": "Poles",
    "voltage": "Supply_Voltage",
    "speed": "Shaft_Speed_[RPM]",
    "slots_stator": "Slots",
    "turns_per_coil": "Turns_per_Coil",
    "copper_loss": "Stator_Copper_Loss_@Ref_Speed",
    "wj_inlet_temp": "WJ_Fluid_Inlet_Temperature",
    "wj_flow_rate": "WJ_Fluid_Volume_Flow_Rate",
}


@app.route("/api/semi-auto/solve", methods=["POST"])
def api_semi_auto_solve():
    """启动半自动求解工作流"""
    if not MOTOR_OK:
        return jsonify({"error": "Motor-CAD 模块未加载"}), 500

    data = request.get_json(force=True)
    params = data.get("params", {})

    if not params:
        return jsonify({"error": "缺少参数"}), 400

    # 映射参数到 .mot 变量名
    mot_params = {}
    for key, val in params.items():
        mot_key = _PARAM_TO_MOT.get(key, key)
        mot_params[mot_key] = val

    result = semi_auto_solve_start(mot_params)
    if result == "started":
        return jsonify({"status": "started", "message": "半自动求解工作流已启动"})
    else:
        return jsonify({"error": result}), 400


@app.route("/api/semi-auto/status", methods=["GET"])
def api_semi_auto_status():
    """查询半自动求解工作流状态"""
    if not MOTOR_OK:
        return jsonify({"error": "Motor-CAD 模块未加载"}), 500

    status = semi_auto_solve_status()
    return jsonify(status)


@app.route("/api/semi-auto/cancel", methods=["POST"])
def api_semi_auto_cancel():
    """取消半自动求解工作流"""
    if not MOTOR_OK:
        return jsonify({"error": "Motor-CAD 模块未加载"}), 500

    result = semi_auto_solve_cancel()
    return jsonify({"status": result})


@app.route("/api/motorcad/upload-csv", methods=["POST"])
def api_motorcad_upload_csv():
    """上传 CSV 文件导入 Motor-CAD 求解结果"""
    if not MOTOR_OK:
        return jsonify({"error": "Motor-CAD 模块未加载"}), 500

    if "file" not in request.files:
        return jsonify({"error": "未上传文件"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "文件名为空"}), 400

    if not file.filename.lower().endswith(".csv"):
        return jsonify({"error": "仅支持 CSV 文件"}), 400

    try:
        # 保存上传的文件
        upload_dir = os.path.join(BASE_DIR, "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        csv_path = os.path.join(upload_dir, f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        file.save(csv_path)

        # 导入结果
        imported = import_motorcad_csv(csv_path)
        saved_ids = []
        for res in imported:
            params = res.get("params", {})
            sid = save_result(params, res, user_id=current_user_id())
            saved_ids.append(sid)

        return jsonify({
            "success": True,
            "imported_count": len(imported),
            "saved_ids": saved_ids,
            "csv_path": csv_path,
            "solve_mode": "manual_import",
            "message": f"成功导入 {len(imported)} 组 Motor-CAD 求解结果",
        })
    except Exception as e:
        return jsonify({"error": f"导入失败: {str(e)}"}), 500


@app.route("/api/motorcad/download-params", methods=["POST"])
def api_motorcad_download_params():
    """导出参数 CSV 并返回文件下载"""
    if not MOTOR_OK:
        return jsonify({"error": "Motor-CAD 模块未加载"}), 500

    data = request.get_json() or {}
    specs = data.get("specs", {})
    count = data.get("count", 30)

    try:
        param_list, _ = generate_param_matrix(specs, count)
        csv_path = export_params_csv(param_list)

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            csv_content = f.read()

        return Response(
            csv_content,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=motorcad_params_{datetime.now().strftime('%Y%m%d')}.csv"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# API：获取求解日志
# ============================================================
@app.route("/api/solve-log", methods=["GET"])
def api_solve_log():
    """获取最近一次求解的日志"""
    if not MOTOR_OK:
        return jsonify({"logs": [], "summary": {}})
    return jsonify({
        "logs": get_solve_log(),
        "summary": get_last_summary(),
    })


# ============================================================
# API：查询结果
# ============================================================
@app.route("/api/results")
def api_results():
    """获取所有求解结果 (v2.02.01: 按 user_id 隔离)"""
    if not DB_OK:
        return jsonify({"error": "数据库不可用"}), 500
    try:
        uid = current_user_id()
        # admin 可看全部, 普通用户只看自己的
        if g.user and g.user.get("role") == "admin":
            uid = None
        results = get_all_results(user_id=uid)
        stats = get_stats(user_id=uid)
        return jsonify({"results": results, "stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/results/top")
def api_results_top():
    """获取 Top N 方案 (v2.02.01: 按 user_id 隔离)"""
    n = int(request.args.get("n", 10))
    min_eff = float(request.args.get("min_eff", 0))
    if not DB_OK:
        return jsonify({"error": "数据库不可用"}), 500
    try:
        uid = current_user_id()
        if g.user and g.user.get("role") == "admin":
            uid = None
        results = get_top_solutions(n=n, min_efficiency=min_eff, user_id=uid)
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# API：AI 方案点评
# ============================================================
@app.route("/api/review", methods=["POST"])
def api_review():
    """本地 AI 对方案做一句话点评"""
    data = request.get_json()
    solutions = data.get("solutions", [])

    # 适配数据格式：数据库里 params 是 JSON 字符串，需解析为 dict
    adapted = []
    for s in solutions:
        s = dict(s)
        params = s.get("params")
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except:
                params = {}
        elif params is None:
            params = {}
        if "stator_od" not in params:
            params["stator_od"] = s.get("stator_od")
        if "poles" not in params:
            params["poles"] = s.get("poles")
        s["params"] = params
        adapted.append(s)

    try:
        review = local_review_solutions(adapted)
        return jsonify({"review": review})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# API：脱敏检查
# ============================================================
@app.route("/api/check-desensitize", methods=["POST"])
def api_check_desensitize():
    """检查文本是否包含敏感信息"""
    data = request.get_json()
    text = data.get("text", "")

    is_safe, warning = check_desensitize(text)
    auto_text = auto_desensitize(text) if not is_safe else text

    return jsonify({
        "is_safe": is_safe,
        "warning": warning,
        "original": text,
        "desensitized": auto_text,
    })


# ============================================================
# API：云端深度分析
# ============================================================
@app.route("/api/cloud-analysis", methods=["POST"])
def api_cloud_analysis():
    """云端 AI 深度对比分析"""
    data = request.get_json()
    text = data.get("text", "")

    # 脱敏检查
    is_safe, warning = check_desensitize(text)
    if not is_safe:
        text = auto_desensitize(text)

    try:
        analysis = cloud_deep_analysis(text)
        return jsonify({"analysis": analysis, "desensitized": text, "was_desensitized": not is_safe})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# API：AI 对话 (供客户端 AI 团队面板)
# ============================================================
@app.route("/api/ai/chat", methods=["POST"])
def api_ai_chat():
    """AI 对话接口，支持多轮对话"""
    if not AI_OK:
        return jsonify({"error": f"AI 模块未加载: {AI_ERR}"}), 500

    data = request.get_json()
    messages = data.get("messages", [])
    model = data.get("model", "auto")
    temperature = data.get("temperature", 0.7)

    if not messages:
        return jsonify({"error": "消息不能为空"}), 400

    try:
        result = ai_chat(messages, model=model, temperature=temperature)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "response": "", "model_used": "", "error": str(e)}), 500


@app.route("/api/ai/models", methods=["GET"])
def api_ai_models():
    """获取可用的 AI 模型列表"""
    if not AI_OK:
        return jsonify({"models": [], "error": f"AI 模块未加载: {AI_ERR}"})
    try:
        models = get_available_models()
        return jsonify({"models": models, "ai_ok": AI_OK})
    except Exception as e:
        return jsonify({"models": [], "error": str(e)})


@app.route("/api/ollama/models", methods=["GET"])
def api_ollama_models():
    """获取 Ollama 已安装的所有本地模型列表（动态查询 /api/tags）"""
    import urllib.request as _urllib_req
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    try:
        with _urllib_req.urlopen(f"{ollama_url}/api/tags", timeout=5) as resp:
            tags_data = json.loads(resp.read())
        models = []
        for m in tags_data.get("models", []):
            size_bytes = m.get("size", 0)
            size_gb = size_bytes / (1024 ** 3) if size_bytes else 0
            details = m.get("details", {})
            param_size = details.get("parameter_size", "")
            quant_level = details.get("quantization_level", "")
            family = details.get("family", "")
            desc_parts = []
            if param_size:
                desc_parts.append(param_size)
            if size_gb > 0:
                desc_parts.append(f"{size_gb:.1f}GB")
            if quant_level:
                desc_parts.append(quant_level)
            if family:
                desc_parts.append(family)
            models.append({
                "name": m.get("name", ""),
                "size": size_bytes,
                "size_gb": round(size_gb, 2),
                "parameter_size": param_size,
                "quantization": quant_level,
                "family": family,
                "modified_at": m.get("modified_at", ""),
                "description": ", ".join(desc_parts),
            })
        return jsonify({"models": models, "ollama_url": ollama_url, "status": "ok"})
    except Exception as e:
        return jsonify({"models": [], "ollama_url": ollama_url, "status": "error", "error": str(e)})


@app.route("/api/ollama/load", methods=["POST"])
def api_ollama_load():
    """预加载指定模型到 Ollama 内存（GPU/CPU），切换模型后立即生效"""
    import urllib.request as _urllib_req
    data = request.get_json() or {}
    model_name = data.get("model_name", "")
    use_gpu = data.get("use_gpu", False)

    if not model_name:
        return jsonify({"status": "error", "error": "model_name is required"})

    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

    try:
        # 1. 调用 /api/generate 预热模型（空生成，仅加载到内存）
        #    keep_alive=30m 确保模型在内存中保留30分钟
        #    num_gpu=-1: 尽可能多的层放到GPU显存（use_gpu=true时）
        #    num_gpu=0: 强制CPU模式（use_gpu=false时）
        #    num_ctx: 根据模型大小动态调整，减少KV cache显存占用
        gen_payload = {
            "model": model_name,
            "prompt": " ",
            "stream": False,
            "keep_alive": "30m",
        }
        if use_gpu:
            # GPU模式: 所有层放GPU + 根据模型大小限制上下文长度
            # T600 4GB显存: 3B模型用8K上下文(100%放GPU), 4B用4K(部分放GPU), 7B+关闭GPU
            model_lower = model_name.lower()
            if "14b" in model_lower or "13b" in model_lower:
                num_ctx = 2048  # 大模型用最小上下文
            elif "7b" in model_lower or "8b" in model_lower:
                num_ctx = 4096
            elif "4b" in model_lower:
                num_ctx = 4096  # 4B模型用4K上下文，尽量多放GPU
            else:
                num_ctx = 8192  # 3B及以下用8K上下文
            gen_payload["options"] = {"num_gpu": -1, "num_ctx": num_ctx}
        else:
            # CPU模式: 不使用GPU
            gen_payload["options"] = {"num_gpu": 0}

        req = _urllib_req.Request(
            f"{ollama_url}/api/generate",
            data=json.dumps(gen_payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _urllib_req.urlopen(req, timeout=120) as resp:
            gen_result = json.loads(resp.read())

        # 2. 查询 /api/ps 确认模型已加载并获取 VRAM 信息
        with _urllib_req.urlopen(f"{ollama_url}/api/ps", timeout=5) as resp:
            ps_data = json.loads(resp.read())

        loaded_models = [m.get("name") for m in ps_data.get("models", [])]
        is_loaded = model_name in loaded_models

        vram_gb = 0.0
        total_gb = 0.0
        gpu_pct = 0.0
        for m in ps_data.get("models", []):
            if m.get("name") == model_name:
                vram_gb = m.get("size_vram", 0) / (1024 ** 3)
                total_gb = m.get("size", 0) / (1024 ** 3)
                if total_gb > 0:
                    gpu_pct = (vram_gb / total_gb) * 100

        device = "GPU" if vram_gb > 0.1 else "CPU"

        # 构建设备信息描述
        if device == "GPU":
            gpu_info = f" (VRAM: {vram_gb:.1f}GB/{total_gb:.1f}GB, {gpu_pct:.0f}%在GPU)"
        else:
            gpu_info = f" (内存: {total_gb:.1f}GB)"

        return jsonify({
            "status": "ok",
            "model": model_name,
            "loaded": is_loaded,
            "device": device,
            "vram_gb": round(vram_gb, 2),
            "total_gb": round(total_gb, 2),
            "gpu_pct": round(gpu_pct, 0),
            "message": f"模型 {model_name} 已加载到 {device}{gpu_info}"
        })
    except _urllib_req.HTTPError as e:
        err_body = ""
        try:
            err_body = json.loads(e.read()).get("error", "")
        except Exception:
            pass
        return jsonify({
            "status": "error",
            "error": f"Ollama HTTP {e.code}: {err_body or e.reason}",
            "model": model_name
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e), "model": model_name})


@app.route("/api/ollama/unload", methods=["POST"])
def api_ollama_unload():
    """卸载 Ollama 内存中的模型，释放显存"""
    import urllib.request as _urllib_req
    data = request.get_json() or {}
    model_name = data.get("model_name", "")
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

    if not model_name:
        # 卸载所有模型
        try:
            with _urllib_req.urlopen(f"{ollama_url}/api/ps", timeout=5) as resp:
                ps_data = json.loads(resp.read())
            for m in ps_data.get("models", []):
                name = m.get("name", "")
                payload = json.dumps({"model": name, "keep_alive": 0}).encode()
                req = _urllib_req.Request(
                    f"{ollama_url}/api/generate",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                _urllib_req.urlopen(req, timeout=10)
            return jsonify({"status": "ok", "message": "已卸载所有模型"})
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)})
    else:
        try:
            payload = json.dumps({"model": model_name, "keep_alive": 0}).encode()
            req = _urllib_req.Request(
                f"{ollama_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            _urllib_req.urlopen(req, timeout=10)
            return jsonify({"status": "ok", "message": f"已卸载模型 {model_name}"})
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)})


@app.route("/api/ollama/vram", methods=["GET"])
def api_ollama_vram():
    """只读: 返回 Ollama /api/ps 的 size_vram 总和与各模型明细 (供看门狗/死锁排查)"""
    import urllib.request as _urllib_req
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    try:
        with _urllib_req.urlopen(f"{ollama_url}/api/ps", timeout=5) as resp:
            ps = json.loads(resp.read())
        models = []
        total = 0.0
        for m in ps.get("models", []):
            vram = float(m.get("size_vram", 0) or 0) / (1024 ** 3)
            total += vram
            models.append({
                "name": m.get("name"),
                "size_vram_gb": round(vram, 2),
                "size_gb": round(float(m.get("size", 0) or 0) / (1024 ** 3), 2),
                "context_length": m.get("context_length", 0),
            })
        try:
            from Core.workflow_config import get_llm_runtime
            wd = get_llm_runtime().get("watchdog", {})
        except Exception:
            wd = {}
        return jsonify({
            "status": "ok",
            "total_vram_gb": round(total, 2),
            "threshold_gb": wd.get("vram_threshold_gb", 3.5),
            "watchdog_enabled": wd.get("enabled", True),
            "models": models,
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})


@app.route("/api/report", methods=["POST"])
def api_report():
    """生成完整报告（含详细参数对比表）"""
    data = request.get_json()
    specs = data.get("specs", {})
    solutions = data.get("solutions", [])
    review = data.get("review", "")
    analysis = data.get("analysis", "")
    raw_text = data.get("raw_text", "")

    # 适配数据格式：数据库里 params 是 JSON 字符串，需解析为 dict
    adapted = []
    for s in solutions:
        s = dict(s)
        params = s.get("params")
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except:
                params = {}
        elif params is None:
            params = {}
        if "stator_od" not in params:
            params["stator_od"] = s.get("stator_od")
        if "poles" not in params:
            params["poles"] = s.get("poles")
        if "slots_stator" not in params:
            params["slots_stator"] = s.get("slots_stator")
        if "slots_rotor" not in params:
            params["slots_rotor"] = s.get("slots_rotor")
        if "turns_per_coil" not in params:
            params["turns_per_coil"] = s.get("turns_per_coil")
        s["params"] = params
        adapted.append(s)

    try:
        # 生成本地草稿
        draft = local_draft_report(specs, adapted, review) if review else ""

        # 如果云端可用且已有分析，润色报告
        if ai_tools.cloud_client and analysis and not analysis.startswith("[云端"):
            try:
                final_report = cloud_polish_report(draft, analysis)
            except Exception:
                final_report = draft
        else:
            final_report = draft

        # ---- 构建详细对比表格 ----
        def _fmt(val, unit="", fmt=".2f"):
            if val is None or val == "" or (isinstance(val, float) and val == 0.0 and unit == ""):
                return "-"
            try:
                return f"{val:{fmt}}{unit}" if isinstance(val, (int, float)) else f"{val}{unit}"
            except:
                return f"{val}{unit}"

        def _fmt_int(val):
            if val is None or val == "":
                return "-"
            try:
                return str(int(val))
            except:
                return str(val)

        # 表头
        col_count = len(adapted)
        header = "| 参数 |" + "|".join(f" 方案{i+1} " for i in range(col_count)) + "|"
        separator = "|---|" + "|".join("---" for _ in range(col_count)) + "|"

        rows = []

        # 结构参数
        rows.append(f"| **结构参数** |{'|' * col_count}|")
        rows.append("| 定子外径 (mm) |" + "|".join(f" {_fmt_int(a.get('params', {}).get('stator_od', a.get('stator_od')))} " for a in adapted) + "|")
        rows.append("| 极数 |" + "|".join(f" {_fmt_int(a.get('params', {}).get('poles', a.get('poles')))} " for a in adapted) + "|")
        rows.append("| 定子槽数 |" + "|".join(f" {_fmt_int(a.get('slots_stator') or a.get('params', {}).get('slots_stator'))} " for a in adapted) + "|")
        rows.append("| 转子槽数 |" + "|".join(f" {_fmt_int(a.get('slots_rotor') or a.get('params', {}).get('slots_rotor'))} " for a in adapted) + "|")
        rows.append("| 线圈匝数 |" + "|".join(f" {_fmt_int(a.get('turns_per_coil') or a.get('params', {}).get('turns_per_coil'))} " for a in adapted) + "|")
        rows.append("| 电压 (V) |" + "|".join(f" {_fmt(a.get('voltage_v') or a.get('params', {}).get('voltage'), fmt='.0f')} " for a in adapted) + "|")
        rows.append("| 转速 (rpm) |" + "|".join(f" {_fmt(a.get('speed_rpm') or a.get('params', {}).get('speed'), fmt='.0f')} " for a in adapted) + "|")

        # 性能参数
        rows.append(f"| **性能参数** |{'|' * col_count}|")
        rows.append("| 效率 (%) |" + "|".join(f" {_fmt(a.get('efficiency'), '%')} " for a in adapted) + "|")
        rows.append("| 温升 (K) |" + "|".join(f" {_fmt(a.get('max_temp'), 'K')} " for a in adapted) + "|")
        rows.append("| 转矩 (N·m) |" + "|".join(f" {_fmt(a.get('torque'), 'N·m')} " for a in adapted) + "|")
        rows.append("| 功率 (kW) |" + "|".join(f" {_fmt(a.get('power'), 'kW')} " for a in adapted) + "|")
        rows.append("| 功率因数 |" + "|".join(f" {_fmt(a.get('power_factor'))} " for a in adapted) + "|")

        # 电磁参数
        rows.append(f"| **电磁参数** |{'|' * col_count}|")
        rows.append("| 定子齿磁密 (T) |" + "|".join(f" {_fmt(a.get('stator_tooth_flux_density'), 'T', '.3f')} " for a in adapted) + "|")
        rows.append("| 定子轭磁密 (T) |" + "|".join(f" {_fmt(a.get('stator_yoke_flux_density'), 'T', '.3f')} " for a in adapted) + "|")
        rows.append("| 转子轭磁密 (T) |" + "|".join(f" {_fmt(a.get('rotor_yoke_flux_density'), 'T', '.3f')} " for a in adapted) + "|")
        rows.append("| 气隙磁密 (T) |" + "|".join(f" {_fmt(a.get('air_gap_flux_density'), 'T', '.3f')} " for a in adapted) + "|")
        rows.append("| 电密 (A/mm²) |" + "|".join(f" {_fmt(a.get('current_density'), 'A/mm²')} " for a in adapted) + "|")

        # 损耗参数
        rows.append(f"| **损耗参数** |{'|' * col_count}|")
        rows.append("| 铜耗 (W) |" + "|".join(f" {_fmt(a.get('copper_loss'), 'W', '.1f')} " for a in adapted) + "|")
        rows.append("| 铁耗 (W) |" + "|".join(f" {_fmt(a.get('iron_loss'), 'W', '.1f')} " for a in adapted) + "|")
        rows.append("| 总损耗 (W) |" + "|".join(f" {_fmt(a.get('total_loss'), 'W', '.1f')} " for a in adapted) + "|")

        # 热参数
        rows.append(f"| **热参数** |{'|' * col_count}|")
        rows.append("| 发热因数 |" + "|".join(f" {_fmt(a.get('thermal_factor'), '', '.1f')} " for a in adapted) + "|")
        rows.append("| 铜线温度 (°C) |" + "|".join(f" {_fmt(a.get('copper_temp'), '°C', '.1f')} " for a in adapted) + "|")
        rows.append("| 磁钢温度 (°C) |" + "|".join(f" {_fmt(a.get('magnet_temp'), '°C', '.1f')} " for a in adapted) + "|")

        # 求解模式
        rows.append(f"| **求解信息** |{'|' * col_count}|")
        rows.append("| 求解模式 |" + "|".join(f" {'Motor-CAD' if a.get('solve_mode') == 'motorcad' else '模拟数据'} " for a in adapted) + "|")

        comparison_table = header + "\n" + separator + "\n" + "\n".join(rows)

        # ---- 构建推荐方案详细说明 ----
        recommendation_section = ""
        if adapted:
            # 选择推荐方案: 综合效率、温升、转矩
            best = max(adapted, key=lambda x: (
                x.get("efficiency", 0) * 0.4 +
                (130 - x.get("max_temp", 100)) * 0.3 +
                x.get("torque", 0) * 0.3
            ))
            best_idx = adapted.index(best) + 1
            best_od = best.get("params", {}).get("stator_od", best.get("stator_od", "?"))
            best_poles = best.get("params", {}).get("poles", best.get("poles", "?"))
            best_slots = best.get("slots_stator") or best.get("params", {}).get("slots_stator", "?")
            best_turns = best.get("turns_per_coil") or best.get("params", {}).get("turns_per_coil", "?")

            recommendation_section = f"""
## 推荐方案详细说明

### 推荐方案：方案{best_idx}

**推荐理由**：方案{best_idx}在效率、温升和转矩之间取得了最佳平衡。

#### 结构参数
| 参数 | 值 |
|---|---|
| 定子外径 | {best_od} mm |
| 极数 | {best_poles} |
| 定子槽数 | {best_slots} |
| 转子槽数 | {best.get('slots_rotor', '?')} |
| 线圈匝数 | {best_turns} 匝 |
| 供电电压 | {best.get('voltage_v') or best.get('params', {}).get('voltage', '?')} V |
| 额定转速 | {best.get('speed_rpm') or best.get('params', {}).get('speed', '?')} rpm |

#### 性能指标
| 参数 | 值 | 评价 |
|---|---|---|
| 效率 | {_fmt(best.get('efficiency'), '%')} | {'优秀' if best.get('efficiency', 0) >= 92 else '良好' if best.get('efficiency', 0) >= 88 else '一般'} |
| 温升 | {_fmt(best.get('max_temp'), 'K')} | {'低' if best.get('max_temp', 100) < 70 else '中等' if best.get('max_temp', 100) < 100 else '偏高'} |
| 转矩 | {_fmt(best.get('torque'), 'N·m')} | - |
| 功率 | {_fmt(best.get('power'), 'kW')} | - |
| 功率因数 | {_fmt(best.get('power_factor'))} | - |

#### 电磁参数
| 参数 | 值 | 参考范围 |
|---|---|---|
| 定子齿磁密 | {_fmt(best.get('stator_tooth_flux_density'), 'T', '.3f')} | 1.5~1.9 T |
| 定子轭磁密 | {_fmt(best.get('stator_yoke_flux_density'), 'T', '.3f')} | 1.2~1.6 T |
| 转子轭磁密 | {_fmt(best.get('rotor_yoke_flux_density'), 'T', '.3f')} | 1.3~1.7 T |
| 气隙磁密 | {_fmt(best.get('air_gap_flux_density'), 'T', '.3f')} | 0.7~1.0 T |
| 电密 | {_fmt(best.get('current_density'), 'A/mm²')} | 3~8 A/mm² |

#### 损耗分析
| 损耗类型 | 值 (W) | 占比 |
|---|---|---|
| 铜耗 | {_fmt(best.get('copper_loss'), 'W', '.1f')} | {_fmt((best.get('copper_loss', 0) / max(best.get('total_loss', 1), 1)) * 100, '%', '.1f') if best.get('copper_loss') else '-'} |
| 铁耗 | {_fmt(best.get('iron_loss'), 'W', '.1f')} | {_fmt((best.get('iron_loss', 0) / max(best.get('total_loss', 1), 1)) * 100, '%', '.1f') if best.get('iron_loss') else '-'} |
| 总损耗 | {_fmt(best.get('total_loss'), 'W', '.1f')} | 100% |

#### 热分析
| 参数 | 值 |
|---|---|
| 发热因数 | {_fmt(best.get('thermal_factor'), '', '.1f')} |
| 铜线温度 | {_fmt(best.get('copper_temp'), '°C', '.1f')} |
| 磁钢温度 | {_fmt(best.get('magnet_temp'), '°C', '.1f')} |
| 绕组最高温升 | {_fmt(best.get('max_temp'), 'K')} |

### Motor-CAD 特性曲线与图谱

> 以下图表在 Motor-CAD 实际求解模式下可自动生成。当前{ '已使用 Motor-CAD 求解' if any(a.get('solve_mode') == 'motorcad' for a in adapted) else '使用模拟数据，暂无实际图表。接入 Motor-CAD 后将自动生成以下内容'}：

1. **空载磁密分布图** - 定转子磁密云图，展示磁饱和区域
2. **负载磁密分布图** - 额定负载下的磁密分布
3. **空载反电势波形** - 不同转速下的反电势曲线
4. **转矩-转速曲线** - 全速域转矩输出特性
5. **效率 MAP 图** - 全工况效率分布
6. **温升分布图** - 绕组及磁钢温度场
7. **损耗-转速曲线** - 铜耗、铁耗随转速变化
"""

        # ---- 组装完整报告 ----
        full_report = f"""# 电机设计方案报告

**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**系统版本**: Motor-AI (v{Config.VERSION_LABEL})

---

## 1. 原始需求

{raw_text}

## 2. 提取规格

| 参数 | 值 |
|---|---|
| 额定功率 | {specs.get('power_kw', '未指定')} kW |
| 供电电压 | {specs.get('voltage_v', '未指定')} V |
| 转速 | {specs.get('speed_rpm', '未指定')} rpm |
| 冷却方式 | {specs.get('cooling', '未指定')} |
| 效率目标 | {specs.get('efficiency_target', '未指定')}% |
| 外径限制 | {specs.get('stator_od_limit', '未指定')} mm |

## 3. 方案对比（详细参数表）

{comparison_table}

## 4. 方案点评

{review if review else '（未生成点评）'}

## 5. 深度分析

{analysis if analysis else '（未进行云端深度分析）'}

{recommendation_section}

## 6. 报告正文

{final_report if final_report else '（本地模型报告生成中...）'}

---

*本报告由 Motor-AI v{Config.VERSION_LABEL} 自动生成 | {datetime.now().strftime('%Y-%m-%d %H:%M')}*
"""

        # 保存报告
        os.makedirs(REPORTS_DIR, exist_ok=True)
        report_path = os.path.join(
            REPORTS_DIR,
            f"analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
        )
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(full_report)

        return jsonify({"report": full_report, "path": report_path})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ============================================================
# API：报告列表
# ============================================================
@app.route("/api/reports")
def api_reports():
    """列出所有已保存的报告"""
    reports = []
    if os.path.exists(REPORTS_DIR):
        for f in sorted(glob.glob(os.path.join(REPORTS_DIR, "analysis_*.md")), reverse=True):
            reports.append({
                "filename": os.path.basename(f),
                "path": f,
                "size": os.path.getsize(f),
                "modified": datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M:%S"),
            })
    return jsonify({"reports": reports})


@app.route("/api/report/read")
def api_report_read():
    """读取指定报告内容"""
    path = request.args.get("path", "")
    if not os.path.exists(path):
        return jsonify({"error": "文件不存在"}), 404
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return jsonify({"content": content})


# ============================================================
# API：导出 CSV
# ============================================================
@app.route("/api/export-csv")
def api_export_csv():
    """导出全部结果到 CSV"""
    if not DB_OK:
        return jsonify({"error": "数据库不可用"}), 500
    try:
        path = export_csv()
        if path:
            return jsonify({"path": path})
        return jsonify({"error": "无数据可导出"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# API：设置
# ============================================================
@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """获取当前设置"""
    env = read_env_file()
    return jsonify({
        "ollama_base_url": env.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        "local_model_name": env.get("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M"),
        "use_gpu": env.get("USE_GPU", "false").lower() == "true",
        "deepseek_api_key": env.get("DEEPSEEK_API_KEY", ""),
        "deepseek_base_url": env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "cloud_model_name": env.get("CLOUD_MODEL_NAME", "deepseek-chat"),
        "motorcad_exe_path": env.get("MOTORCAD_EXE_PATH", r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"),
        "motorcad_progid": env.get("MOTORCAD_PROGID", "MotorCAD.MotorCAD"),
        "motorcad_use_mode": env.get("MOTORCAD_USE_MODE", "auto"),
        "system_name": env.get("SYSTEM_NAME", "Motor-AI"),
        "system_version": env.get("SYSTEM_VERSION", Config.SYSTEM_VERSION),
        "version_date": env.get("VERSION_DATE", "2026-07-27 07:00"),
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_set():
    """更新设置"""
    data = request.get_json()
    updates = {}

    if "local_model_name" in data:
        updates["LOCAL_MODEL_NAME"] = data["local_model_name"]
    if "use_gpu" in data:
        updates["USE_GPU"] = "true" if data["use_gpu"] else "false"
    if "deepseek_api_key" in data:
        updates["DEEPSEEK_API_KEY"] = data["deepseek_api_key"]
    if "cloud_model_name" in data:
        updates["CLOUD_MODEL_NAME"] = data["cloud_model_name"]
    if "motorcad_use_mode" in data:
        updates["MOTORCAD_USE_MODE"] = data["motorcad_use_mode"]

    try:
        write_env_file(updates)
    except PermissionError as e:
        return jsonify({"status": "error", "error": f"无法写入配置文件: {e}"}), 500
    except Exception as e:
        return jsonify({"status": "error", "error": f"保存配置失败: {e}"}), 500

    # 重新加载 .env 到环境变量，使 os.getenv 获取最新值
    load_dotenv(ENV_PATH, override=False)

    # 运行时更新 AI 模块变量
    if AI_OK:
        if "LOCAL_MODEL_NAME" in updates:
            ai_tools.LOCAL_MODEL = updates["LOCAL_MODEL_NAME"]
        if "USE_GPU" in updates:
            ai_tools.USE_GPU = updates["USE_GPU"] == "true"
        if "DEEPSEEK_API_KEY" in updates:
            key = updates["DEEPSEEK_API_KEY"]
            if key and key != "sk-your-key-here":
                from openai import OpenAI
                ai_tools.cloud_client = OpenAI(
                    api_key=key,
                    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                )
            else:
                ai_tools.cloud_client = None

    # 运行时更新 Motor-CAD 模块变量
    if MOTOR_OK:
        if "MOTORCAD_USE_MODE" in updates:
            motor_tools.MOTORCAD_USE_MODE = updates["MOTORCAD_USE_MODE"].lower()
        if "MOTORCAD_EXE_PATH" in updates:
            motor_tools.MOTORCAD_EXE_PATH = updates["MOTORCAD_EXE_PATH"]
            motor_tools._motorcad_available = os.path.exists(updates["MOTORCAD_EXE_PATH"])
        if "MOTORCAD_PROGID" in updates:
            motor_tools.MOTORCAD_PROGID = updates["MOTORCAD_PROGID"]

    return jsonify({"status": "ok", "message": "设置已保存并即时生效"})


# ============================================================
# API：测试用例
# ============================================================
@app.route("/api/test-cases")
def api_test_cases():
    """返回内置测试用例"""
    cases = [
        "我们要一台电动叉车驱动电机，额定功率10kW，48V电池供电，水冷，希望效率不低于93%，安装空间外径限制250mm。",
        "需要一个伺服电机，功率2.2kW，220V，转速3000rpm左右，自然风冷就行。",
        "做一款无人机电机，功率500W，电压48V，转速10000rpm以上，要求重量轻，效率90%以上。",
        "矿用隔爆电机，55kW，660V，水冷，效率要达到IE4标准（96%），转速1480rpm。",
        "家电用无刷直流电机，120W，24V，转速2000rpm，自然风冷，效率85%以上。",
    ]
    return jsonify({"cases": cases})


# ============================================================
# API：求解器状态 (v3.00.02 RMxprt 升级)
# ============================================================
@app.route("/api/solver/status")
@require_auth
def api_solver_status():
    """获取所有求解引擎的当前状态 (RMxprt + Motor-CAD + 模拟)"""
    if not MOTOR_OK:
        return jsonify({"error": "motor_tools not available"}), 500
    try:
        status = check_solver_status()
        # 附带当前用户权限信息
        if AUTH_OK:
            allowed, reason, degraded = check_motorcad_permission()
            status["user_permission"] = {
                "can_use_host_motorcad": allowed,
                "reason": reason,
                "degraded_mode": degraded,
            }
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# API：多智能体工作流（SSE 流式）
# ============================================================
# v4.02.06 (问题② Fix B): step(节点名) → checkpoint_type(DB 类型) 映射,
# 与 orchestrator.OrchestratorAgent.STEP_TO_CHECKPOINT_TYPE 保持一致。
_STEP_TO_CP_TYPE = {
    "hc_requirement": "requirement_confirm",
    "hc_base_param": "base_param_select",
    "hc_design": "design_review",
    "decision_gate": "design_final_select",
    "hc_report": "report_review",
}


def _backfill_checkpoint_id(project_id: str, step: str) -> str:
    """v4.02.06 (问题② Fix B): SSE 转发 human_input_required 时若缺 checkpoint_id,
    按 (project_id, checkpoint_type, status='pending') 从 human_checkpoints 表回查补全,
    杜绝客户端收到空 id 后报「无效的检查点 ID，无法提交决策」。
    """
    if not HUMAN_CP_OK:
        return ""
    try:
        rows = hc.list_pending_checkpoints((project_id or "").strip() or None) or []
        if not rows:
            return ""
        want = _STEP_TO_CP_TYPE.get(step, step)
        for r in rows:  # 已按 created_at DESC 排序
            if want and r.get("checkpoint_type") == want:
                return str(r.get("id") or "")
        return str(rows[0].get("id") or "")
    except Exception as e:
        print(f"[workflow][Fix B] 回查 pending 检查点失败: {e}")
        return ""


# ══════════════════════════════════════════════════════════════════════════
# v4.02.104 (需求 二): 实时日志「模型名 / 后端版本 / 客户端版本」可观测
#   客户端实时日志此前只能看到全局 LOCAL_MODEL_NAME, 看不到
#   (a) 后端程序版本 (b) 客户端版本 (c) 每个节点实际调用的模型/若降级到本地也要看得见。
#   此处构造 start 事件与 step_start 事件所需的模型快照, 决策逻辑直接复用
#   Core/llm_gateway._resolve_plan (与真实路由同一函数, 避免回显与运行态分裂)。
# ══════════════════════════════════════════════════════════════════════════
# step(节点名) → 该节点会调用的智能体 (键名与 workflow_config.agent_model_map 一致)
_STEP_AGENTS = {
    "parse_requirement": ["RequirementAgent"],
    "generate_base_params": ["DesignAgent"],
    "design_ideation": ["IdeationAgent"],
    "generate_designs": ["DesignAgent"],
    "review_solutions": ["ReviewAgent", "DevilsAdvocate", "RealityChecker",
                         "ReverseReviewer", "SandboxSimulator"],
    "convergence_review": ["ReviewAgent", "DevilsAdvocate", "RealityChecker",
                           "ReverseReviewer", "SandboxSimulator"],
    "generate_report": ["ReportAgent"],
}
# 需要在 start 事件里整体回显的智能体集合 (去重、稳定顺序)
_AGENT_ORDER = ["RequirementAgent", "DesignAgent", "IdeationAgent", "ReviewAgent",
                "DevilsAdvocate", "RealityChecker", "ReverseReviewer",
                "SandboxSimulator", "ReportAgent"]


def _agent_model_snapshot(agents=None) -> dict:
    """返回 {agent: {backend, model, source, is_cloud}} 的生效模型快照。

    与 Core/llm_gateway.resolve_llm_for_agent 共用 _resolve_plan 决策层 ⇒ 回显即运行态。
    异常/模块缺失时返回空 dict (实时日志退化为只显示全局模型名, 不影响主流程)。
    """
    try:
        from Core.llm_gateway import _resolve_plan  # type: ignore
    except Exception:
        try:
            from llm_gateway import _resolve_plan  # type: ignore
        except Exception:
            return {}
    out = {}
    for _a in (agents or _AGENT_ORDER):
        try:
            _p = _resolve_plan(_a) or {}
            _m = _p.get("effective_model") or ""
            out[_a] = {
                "model": _m,
                "backend": _p.get("backend") or "",
                "source": _p.get("source") or "",
                "is_cloud": bool(_p.get("backend") == "cloud"),
            }
        except Exception:
            continue
    return out


def _step_model_info(step: str) -> dict:
    """返回某节点的模型信息 (供 step_start 事件回显): {agents: {Agent: model}, summary: str}"""
    _agents = _STEP_AGENTS.get(step) or []
    if not _agents:
        return {}
    _snap = _agent_model_snapshot(_agents)
    if not _snap:
        return {}
    _models = {a: (v.get("model") or "") for a, v in _snap.items()}
    _uniq = []
    for _m in _models.values():
        if _m and _m not in _uniq:
            _uniq.append(_m)
    return {
        "agents": _models,
        "agents_detail": _snap,
        "summary": " / ".join(_uniq),
        "backend": (_snap.get(_agents[0], {}) or {}).get("backend", ""),
    }


@app.route("/api/workflow/run", methods=["POST"])
def api_workflow_run():
    """启动多智能体工作流，返回 SSE 流 (v3.00.09: 快速/精细求解分离)"""
    if not AGENT_OK or not _orchestrator:
        return jsonify({"error": f"Agent system not available: {AGENT_ERR}"}), 500

    data = request.get_json()
    raw_text = data.get("raw_text", "").strip()
    # v3.00.09: 方案数量拆分为快速解算(RMxprt)和精细计算(Motor-CAD可选)
    quick_solve_count = data.get("quick_solve_count", 5)
    fine_solve_count = data.get("fine_solve_count", 0)
    use_cloud_review = data.get("use_cloud_review", False)
    use_cloud_report = data.get("use_cloud_report", False)
    use_local_motorcad = data.get("use_local_motorcad", False)
    case_motor_id = data.get("case_motor_id", 0)  # v4.04: 案例电机ID
    # v4.02.104 (需求 二): 客户端版本 (实时日志回显"客户端版本/后端版本/模型名"用)
    client_version_reported = str(data.get("client_version") or "").strip()[:32]

    # v1.04.14: 客户端选择本机Motor-CAD时, 后端使用模拟模式快速生成方案
    if use_local_motorcad:
        os.environ["MOTORCAD_USE_MODE_OVERRIDE"] = "simulation"

    # v3.00.02: RMxprt 两级求解 — 设置权限标志
    if AUTH_OK and _orchestrator:
        _orchestrator.use_local_motorcad = use_local_motorcad
        allowed, reason, degraded = check_motorcad_permission()
        _orchestrator.can_use_motorcad = allowed
        if not allowed and not use_local_motorcad:
            print(f"[workflow] Motor-CAD 权限: {reason}, 精细验证将降级")

    if not raw_text and not case_motor_id:
        return jsonify({"error": "Please provide raw_text or case_motor_id"}), 400

    # v1.03.02: 启动进度广播
    client_id = request.headers.get("User-Agent", "unknown")[:60]
    if PROGRESS_HUB_OK:
        progress_hub.reset_cancel()
        progress_hub.start("workflow", client=client_id)

    # v1.03.03: 创建全局任务记录
    _wf_task_id = None
    if TASK_MANAGER_OK:
        _wf_task_id = create_task(
            TYPE_WORKFLOW, source=Config.ENV,
            specs={"raw_text": raw_text[:200],
                   "quick_solve_count": quick_solve_count,
                   "fine_solve_count": fine_solve_count}
        )

    def generate():
        try:
            if TASK_MANAGER_OK and _wf_task_id:
                msg = f"AI工作流启动: 快速{quick_solve_count}个 + 精细{fine_solve_count}个"
                update_task(_wf_task_id, status=STATUS_RUNNING, current_step="工作流启动",
                           log_entry=msg)
            # v4.02.104 (需求 二): start 事件回显「后端版本 / 客户端版本 / 各智能体生效模型」
            #   模型快照复用 _agent_model_snapshot() (= _resolve_plan 决策层, 回显即运行态)
            _start_model_snapshot = _agent_model_snapshot()
            yield f"data: {json.dumps({'type': 'start', 'message': 'Workflow started (辩证制衡双轨)', 'model': _resolve_local_model_name(), 'backend_version': SYSTEM_VERSION, 'client_version': client_version_reported, 'model_info': _start_model_snapshot, 'steps': ['load_case_motor', 'parse_requirement', 'hc_requirement', 'generate_base_params', 'hc_base_param', 'generate_designs', 'hc_design', 'review_solutions', 'convergence_review', 'decision_gate', 'generate_report', 'hc_report'], 'quick_solve_count': quick_solve_count, 'fine_solve_count': fine_solve_count, 'case_motor_id': case_motor_id}, ensure_ascii=False, default=str)}\n\n"

            _timing = {}  # v4.00.07: 由 orchestrator _timing 事件填充
            # v4.02.06 (问题② Fix B): 记录本次工作流 project_id,
            # 供 human_input_required 缺 checkpoint_id 时回查 human_checkpoints 表补全。
            _wf_project_id = ""

            _stream = _run_streaming_with_heartbeat(
                _orchestrator.run_streaming(
                    raw_text, solve_count=quick_solve_count,
                    fine_solve_count=fine_solve_count,
                    use_cloud_review=use_cloud_review,
                    use_cloud_report=use_cloud_report,
                    case_motor_id=case_motor_id))
            for update in _stream:
                event_type = update.get("type", "")
                # v4.02.20 (#1083 根因③): 心跳保活 + 生产者异常透传
                if event_type == "__heartbeat__":
                    try:
                        yield f"data: {json.dumps({'type': 'heartbeat', 'ts': _sse_time.time()}, ensure_ascii=False, default=str)}\n\n"
                    except Exception:
                        pass
                    continue
                if event_type == "__producer_error__":
                    yield f"data: {json.dumps({'type': 'fatal', 'error': str(update.get('error', 'unknown'))}, ensure_ascii=False, default=str)}\n\n"
                    return

                # ---- queued: 排队等待 (v4.00.06) ----
                if event_type == "queued":
                    yield f"data: {json.dumps({'type': 'progress', 'step': 'queue', 'message': update.get('message', '排队等待中...'), 'level': 'info'}, ensure_ascii=False, default=str)}\n\n"
                    if TASK_MANAGER_OK and _wf_task_id:
                        update_task(_wf_task_id,
                                   current_step="排队等待中",
                                   log_entry=update.get('message', '排队等待中'))

                # ---- step_start: 节点开始 ----
                elif event_type == "step_start":
                    # v4.02.104 (需求 二): 回显本节点将调用的模型名 (实时日志可见)
                    _smi = _step_model_info(update.get('step', ''))
                    yield f"data: {json.dumps({'type': 'step_start', 'step': update.get('step', ''), 'message': update.get('message', ''), 'model_info': _smi, 'model': (update.get('model') or _smi.get('summary', ''))}, ensure_ascii=False, default=str)}\n\n"
                    if TASK_MANAGER_OK and _wf_task_id:
                        # v3.00.11: 根据步骤更新进度百分比
                        step_name = update.get('step', '')
                        step_progress = {
                            'load_case_motor': 0.02,
                            'parse_requirement': 0.05,
                            'hc_requirement': 0.10,
                            'generate_base_params': 0.15,
                            'hc_base_param': 0.20,
                            'generate_designs': 0.30,
                            'hc_design': 0.50,
                            'review_solutions': 0.65,
                            'convergence_review': 0.75,
                            'decision_gate': 0.85,
                            'generate_report': 0.90,
                            'hc_report': 0.95,
                        }
                        prog = step_progress.get(step_name)
                        update_task(_wf_task_id,
                                   current_step=update.get('message', update.get('step', '')),
                                   progress=prog,
                                   log_entry=f"步骤: {update.get('step', '')}")

                # ---- progress: 子步骤进度 ----
                elif event_type == "progress":
                    yield f"data: {json.dumps({'type': 'progress', 'step': update.get('step', ''), 'message': update.get('message', ''), 'level': update.get('level', 'info'), 'timestamp': update.get('timestamp', '')}, ensure_ascii=False, default=str)}\n\n"
                    if TASK_MANAGER_OK and _wf_task_id:
                        update_task(_wf_task_id, log_entry=update.get('message', ''))

                # ---- step_done: 节点完成 ----
                elif event_type == "step_done":
                    step = update.get("step", "")

                    if step == "load_case_motor":
                        case_ref = update.get("case_motor_ref", {})
                        specs = update.get("specs", {})
                        yield f"data: {json.dumps({'type': 'step_done', 'step': 'load_case_motor', 'status': update.get('status', ''), 'case_motor_ref': case_ref, 'pre_specs': specs}, ensure_ascii=False, default=str)}\n\n"

                    elif step == "parse_requirement":
                        # v4.02.06 (问题② Fix B): 捕获 project_id 供后续 checkpoint_id 回查
                        if update.get("project_id"):
                            _wf_project_id = str(update.get("project_id") or "")
                        if "specs" in update:
                            yield f"data: {json.dumps({'type': 'step_done', 'step': 'parse_requirement', 'status': update.get('status', ''), 'specs': update['specs'], 'confidence': update.get('confidence', 0), 'project_id': update.get('project_id', ''), 'pipeline_steps': update.get('pipeline_steps', []), '_2_1_data': update.get('_2_1_data', {}), 'param_sources': update.get('param_sources', {})}, ensure_ascii=False, default=str)}\n\n"
                        elif "errors" in update:
                            yield f"data: {json.dumps({'type': 'step_error', 'step': 'parse_requirement', 'error': update['errors'][0] if update['errors'] else 'unknown'}, ensure_ascii=False, default=str)}\n\n"

                    elif step == "generate_base_params":
                        if "base_params" in update:
                            yield f"data: {json.dumps({'type': 'step_done', 'step': 'generate_base_params', 'status': update.get('status', ''), 'base_params': update.get('base_params', {}), 'base_param_ranges': update.get('base_param_ranges', {}), 'ref_motors': update.get('ref_motors', []), 'scenario_summary': update.get('scenario_summary', {})}, ensure_ascii=False, default=str)}\n\n"
                        elif "errors" in update:
                            yield f"data: {json.dumps({'type': 'step_error', 'step': 'generate_base_params', 'error': update['errors'][0] if update['errors'] else 'unknown'}, ensure_ascii=False, default=str)}\n\n"

                    elif step == "design_ideation":
                        # v4.01.054: 方案构思节点完成 — 转发构思叙述/结构化约束/输入快照
                        if "ideation_result" in update or "design_hints" in update:
                            yield f"data: {json.dumps({'type': 'step_done', 'step': 'design_ideation', 'status': update.get('status', ''), 'ideation_result': update.get('ideation_result', ''), 'design_hints': update.get('design_hints', {}), 'ideation_input': update.get('ideation_input', {})}, ensure_ascii=False, default=str)}\n\n"
                        elif "errors" in update:
                            yield f"data: {json.dumps({'type': 'step_error', 'step': 'design_ideation', 'error': update['errors'][0] if update['errors'] else 'unknown'}, ensure_ascii=False, default=str)}\n\n"

                    elif step == "generate_designs":
                        if "results" in update:
                            results = update["results"]
                            yield f"data: {json.dumps({'type': 'step_done', 'step': 'generate_designs', 'status': update.get('status', ''), 'results_count': len(results), 'results': results[:10], 'top_results': results[:10], 'matched_templates': update.get('matched_templates', []), 'param_matrix': update.get('param_matrix', []), 'review_feedback_result': update.get('review_feedback_result', {}), 'scenario_reidentified': update.get('scenario_reidentified', False), 'scenario_info': update.get('scenario_info', {}), 'param_matrix_meta': update.get('param_matrix_meta', {}), 'diversity_metrics': update.get('diversity_metrics', {}), 'solve_summary': update.get('solve_summary', {}), 'base_seeds': update.get('base_seeds', {}), 'attempt_index': update.get('attempt_index', 1), 'attempt_reason': update.get('attempt_reason', ''), 'pipeline_steps': update.get('pipeline_steps', []), 'geometry_images': update.get('geometry_images', []), 'l0_l1_diff': update.get('l0_l1_diff', {}), 'l0_calibration': update.get('l0_calibration', {})}, ensure_ascii=False, default=str)}\n\n"
                        elif "errors" in update:
                            yield f"data: {json.dumps({'type': 'step_error', 'step': 'generate_designs', 'error': update['errors'][0] if update['errors'] else 'unknown'}, ensure_ascii=False, default=str)}\n\n"

                    elif step == "review_solutions":
                        if "review" in update:
                            yield f"data: {json.dumps({'type': 'step_done', 'step': 'review_solutions', 'status': update.get('status', ''), 'review': update['review'], 'recommendation': update.get('recommendation', ''), 'top_solutions': update.get('top_solutions', [])}, ensure_ascii=False, default=str)}\n\n"
                        elif "errors" in update:
                            yield f"data: {json.dumps({'type': 'step_error', 'step': 'review_solutions', 'error': update['errors'][0] if update['errors'] else 'unknown'}, ensure_ascii=False, default=str)}\n\n"

                    elif step == "convergence_review":
                        if "con_report" in update:
                            yield f"data: {json.dumps({'type': 'step_done', 'step': 'convergence_review', 'status': update.get('status', ''), 'con_report': update['con_report'], 'convergence_findings': update.get('convergence_findings', {})}, ensure_ascii=False, default=str)}\n\n"
                        elif "errors" in update:
                            yield f"data: {json.dumps({'type': 'step_error', 'step': 'convergence_review', 'error': update['errors'][0] if update['errors'] else 'unknown'}, ensure_ascii=False, default=str)}\n\n"

                    elif step == "decision_gate":
                        yield f"data: {json.dumps({'type': 'step_done', 'step': 'decision_gate', 'status': update.get('status', ''), 'pro_report': update.get('pro_report', ''), 'con_report': update.get('con_report', ''), 'ready_for_confirm': update.get('ready_for_confirm', False)}, ensure_ascii=False, default=str)}\n\n"

                    elif step == "generate_report":
                        if "report" in update:
                            yield f"data: {json.dumps({'type': 'step_done', 'step': 'generate_report', 'status': update.get('status', ''), 'report': update['report'], 'report_filepath': update.get('report_filepath', ''), 'report_filename': update.get('report_filename', ''), 'report_docx_filepath': update.get('report_docx_filepath', '')}, ensure_ascii=False, default=str)}\n\n"
                        elif "errors" in update:
                            yield f"data: {json.dumps({'type': 'step_error', 'step': 'generate_report', 'error': update['errors'][0] if update['errors'] else 'unknown'}, ensure_ascii=False, default=str)}\n\n"

                    else:
                        # P5 (#745): 通用兜底转发 — 覆盖 human_approval / hc_requirement /
                        # hc_base_param / hc_design / hc_report 等未被显式处理的 step_done,
                        # 避免事件被静默丢弃 (客户端依赖这些 step_done 清理检查点弹窗 /
                        # 读取 human_approval 的 decision)。原样转发 orchestrator 注入的全部字段
                        # (含 status / decision 等), 仅强制 type/step 契约字段。
                        fwd = dict(update)
                        fwd["type"] = "step_done"
                        fwd["step"] = step
                        if "status" not in fwd:
                            fwd["status"] = ""
                        yield f"data: {json.dumps(fwd, ensure_ascii=False, default=str)}\n\n"

                # ---- v3.00.09: 需要人工输入 (新增 redo/end) ----
                elif event_type == "human_input_required":
                    # v4.02.06 (问题② Fix B): 转发前强制校验 checkpoint_id;
                    # 缺失则按 (project_id, checkpoint_type, status='pending') 回查补全,
                    # 杜绝客户端收到空 id 后报「无效的检查点 ID，无法提交决策」。
                    _hir_step = update.get("step", "")
                    _hir_cid = str(update.get("checkpoint_id") or "").strip()
                    if not _hir_cid:
                        _hir_ctx = update.get("context", {}) or {}
                        _pid = ""
                        if isinstance(_hir_ctx, dict):
                            _pid = str(_hir_ctx.get("project_id") or "")
                        _hir_cid = _backfill_checkpoint_id(_pid or _wf_project_id, _hir_step)
                        _msg = (f"[Fix B] human_input_required 缺 checkpoint_id, 已回查补全: {_hir_cid}"
                                if _hir_cid else
                                "[Fix B] human_input_required 缺 checkpoint_id 且回查失败")
                        print(f"[workflow] {_msg}")
                        yield f"data: {json.dumps({'type': 'progress', 'step': _hir_step, 'message': _msg, 'level': 'warning' if _hir_cid else 'error'}, ensure_ascii=False, default=str)}\n\n"
                    yield f"data: {json.dumps({'type': 'human_input_required', 'step': _hir_step, 'checkpoint_id': _hir_cid, 'title': update.get('title', '人工确认'), 'description': update.get('description', ''), 'context': update.get('context', {}), 'timeout_seconds': update.get('timeout_seconds', 86400), 'default_action': update.get('default_action', 'auto_approve'), 'options': update.get('options', list(hc.VALID_DECISIONS))}, ensure_ascii=False, default=str)}\n\n"

                # ---- fatal: 致命错误 ----
                elif event_type == "fatal":
                    yield f"data: {json.dumps({'type': 'fatal', 'error': update.get('error', 'unknown')}, ensure_ascii=False, default=str)}\n\n"

                # ---- v4.00.07: timing summary (由 orchestrator 生成, 避免并发读取错误 tracker) ----
                elif event_type == "_timing":
                    _timing = update.get("timing", {})

            # v4.00.03: 附带流程时间统计
            # v4.00.07: 优先使用 orchestrator 传入的 timing (并发隔离), fallback 到直接调用
            if not _timing:
                try:
                    _timing = _orchestrator.generate_timing_summary()
                except Exception:
                    pass

            yield f"data: {json.dumps({'type': 'done', 'message': 'Workflow completed (双轨辩证完成)', 'timing': _timing, 'backend_version': SYSTEM_VERSION, 'client_version': client_version_reported}, ensure_ascii=False, default=str)}\n\n"

            if TASK_MANAGER_OK and _wf_task_id:
                _wf_cancelled = PROGRESS_HUB_OK and progress_hub and progress_hub.is_cancelled()
                update_task(_wf_task_id,
                           status=STATUS_CANCELLED if _wf_cancelled else STATUS_COMPLETED,
                           progress=1.0 if not _wf_cancelled else None,
                           current_step="工作流完成" if not _wf_cancelled else "已终止")

        except Exception as e:
            yield f"data: {json.dumps({'type': 'fatal', 'error': str(e)}, ensure_ascii=False, default=str)}\n\n"
            if TASK_MANAGER_OK and _wf_task_id:
                update_task(_wf_task_id, status=STATUS_FAILED,
                           current_step=f"错误: {str(e)[:100]}")
        finally:
            # v1.04.14: 清理运行时模式覆盖
            os.environ.pop("MOTORCAD_USE_MODE_OVERRIDE", None)

    # ---- v4.03 (F4): 工作流与 SSE 解耦 ----
    # 原实现: Response(stream_with_context(_publish_stream(generate())))
    #   → 工作流驱动于响应生成器内部, 客户端断流即终止工作流 (RC2 的结构性成因:
    #     检查点等待线程随之消失 → pending 检查点成为无人放行的孤儿)。
    # 现实现: 在**独立作业线程**中运行 generate(), 事件经 job.emit 落入带 event_id
    #   的缓冲; 本响应仅作为订阅者读取 → 断流只终止订阅, 工作流继续跑完。
    if not JOB_MGR_OK or _job_manager is None:
        # 兜底: 作业管理器不可用时退回旧的内联执行, 保证功能不中断
        print(f"[workflow][F4] 作业管理器不可用({JOB_MGR_ERR}), 回退内联执行")
        return Response(
            stream_with_context(_publish_stream(generate())),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _workflow_runner(job):
        """在作业线程内执行工作流, 把事件写入作业缓冲 (取代原先在 SSE 生成器内驱动)。"""
        _fatal = ""
        # v1.04.15: 注册本作业 + 绑定到当前线程 ⇒ is_cancelled() 只反映**本作业**的
        #   取消请求(按 job_id 隔离), 不再被同进程其它作业的 cancel 连带误杀。
        if PROGRESS_HUB_OK and progress_hub:
            try:
                progress_hub.register_job(job.job_id, kind="workflow",
                                          task_id=_wf_task_id, client=client_id)
                progress_hub.bind_job(job.job_id)
            except Exception:
                pass
        try:
            for _ev in _iter_sse_events(generate()):
                if _ev.get("type") == "fatal" and not _fatal:
                    _fatal = str(_ev.get("error", "")) or "fatal"
                job.emit(_ev)
        finally:
            if PROGRESS_HUB_OK and progress_hub:
                try:
                    progress_hub.finish()
                except Exception:
                    pass
                try:
                    progress_hub.unbind_job()
                    progress_hub.unregister_job(job.job_id)
                except Exception:
                    pass
        if _fatal:
            job.finish("error", _fatal)  # 供 /api/workflow/jobs 观测

    _job = _job_manager.start_job(
        _workflow_runner,
        meta={"raw_text": raw_text[:200], "quick_solve_count": quick_solve_count,
              "fine_solve_count": fine_solve_count, "task_id": _wf_task_id})
    # v1.04.15: 在返回响应前即把本作业登记进取消注册表(缩小「已启动但未登记」窗口,
    #   避免此刻到达的 cancel 请求把「唯一活跃作业」误判成别的作业)。
    if PROGRESS_HUB_OK and progress_hub:
        try:
            progress_hub.register_job(_job.job_id, kind="workflow",
                                      task_id=_wf_task_id, client=client_id)
        except Exception:
            pass
    print(f"[workflow][F4] 作业已启动: {_job.job_id} (task={_wf_task_id}, "
          f"quick={quick_solve_count}, fine={fine_solve_count})")
    return Response(
        stream_with_context(_sse_from_job(_job, 0)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "X-Workflow-Job-Id": _job.job_id},
    )


# ============================================================
# API：工作流状态查询 & 断点续传 (v2.05.04)
# ============================================================
@app.route("/api/workflow/reset", methods=["POST"])
def api_workflow_reset():
    """v4.00.05: 手动重置工作流并发锁 (管理端用)
    v4.00.07: 适配信号量并发模型 — 重置 active_count 和 running_since"""
    if not AGENT_OK or not _orchestrator:
        return jsonify({"error": "Agent system not available"}), 500
    with _orchestrator._workflow_lock:
        was_active = _orchestrator._active_count
        queue_count = _orchestrator._queue_count
        _orchestrator._active_count = 0
        _orchestrator._running_since.clear()
        _orchestrator._queue_count = 0
    _orchestrator._workflow_slot_event.set()
    print(f"[workflow] 并发锁已手动重置 (was_active={was_active}, queue_count={queue_count})")
    return jsonify({"status": "ok", "was_active": was_active, "queue_count": queue_count})

@app.route("/api/workflow/status", methods=["GET"])
def api_workflow_status():
    """查询工作流状态 (客户端用于检测后端是否存活、工作流是否还在运行)
    v4.00.07: 适配信号量并发模型"""
    active = []
    if TASK_MANAGER_OK:
        try:
            active = get_active_tasks()
        except Exception:
            pass
    wf_active = getattr(_orchestrator, "_active_count", 0) if _orchestrator else 0
    wf_max = getattr(_orchestrator, "_max_concurrent", 1) if _orchestrator else 1
    wf_queue = getattr(_orchestrator, "_queue_count", 0) if _orchestrator else 0
    return jsonify({
        "server_alive": True,
        "active_tasks": len(active),
        "workflow_running": wf_active > 0,
        "workflow_active_count": wf_active,
        "workflow_max_concurrent": wf_max,
        "queue_count": wf_queue,
        "tasks": [
            {
                "id": t.get("id", ""),
                "type": t.get("type", ""),
                "status": t.get("status", ""),
                "current_step": t.get("current_step", ""),
                "progress": t.get("progress", 0),
                "updated_at": t.get("updated_at", ""),
            }
            for t in active
        ]
    })


# ============================================================
# API：工作流事件续传 (v4.03 F4: 断线重连 / 与 SSE 解耦)
# ============================================================
@app.route("/api/workflow/events", methods=["GET"])
def api_workflow_events():
    """续传工作流事件流 (GET SSE) — 客户端断线重连入口。

    客户端 `ai_client._handle_sse_disconnect` 早已按此契约重连
    (`GET /api/workflow/events?last_event_id=N`)，但后端此前**从未提供该端点**，
    导致重连必然 404 → 4 次退避后放弃 → 退化为 5 分钟检查点轮询兜底。
    v4.03 (F4) 补齐：工作流已与 SSE 解耦（作业线程内继续执行），本端点从
    last_event_id 之后**续传**，不重复投递，并继续跟随后续新事件。

    Query: last_event_id (int, 客户端已收到的最大 event_id, 默认 0)
           job_id         (可选, 缺省取当前作业)
    """
    if not JOB_MGR_OK or _job_manager is None:
        return jsonify({"error": f"Workflow job manager not available: {JOB_MGR_ERR}"}), 503
    try:
        last_event_id = int(request.args.get("last_event_id", 0) or 0)
    except (TypeError, ValueError):
        last_event_id = 0
    job_id = (request.args.get("job_id", "") or "").strip()
    job = _job_manager.get(job_id) if job_id else _job_manager.current()
    if job is None:
        return jsonify({"error": "no active workflow job"}), 404
    print(f"[workflow][F4] 续传订阅: job={job.job_id} from_event_id={last_event_id} "
          f"(status={job.status}, buffered={len(job.events)})")
    return Response(
        stream_with_context(_sse_from_job(job, last_event_id)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "X-Workflow-Job-Id": job.job_id},
    )


@app.route("/api/workflow/events_snapshot", methods=["GET"])
def api_workflow_events_snapshot():
    """v4.02.92 (P0-2): 事件快照 (非 SSE JSON) — 客户端轮询兜底定期回拉断流期间丢失的日志事件。

    与 /api/workflow/events (SSE 续传) 的区别: 本端点一次性返回 JSON 即断开,
    适合轮询定时器低频调用, 不占用常驻连接。
    Query: last_event_id (默认 0) / job_id (可选, 缺省取当前作业) / limit (默认 200, 上限 500)
    """
    if not JOB_MGR_OK or _job_manager is None:
        return jsonify({"error": f"Workflow job manager not available: {JOB_MGR_ERR}"}), 503
    try:
        last_event_id = int(request.args.get("last_event_id", 0) or 0)
    except (TypeError, ValueError):
        last_event_id = 0
    try:
        limit = max(1, min(500, int(request.args.get("limit", 200) or 200)))
    except (TypeError, ValueError):
        limit = 200
    job_id = (request.args.get("job_id", "") or "").strip()
    job = _job_manager.get(job_id) if job_id else _job_manager.current()
    if job is None:
        return jsonify({"success": False, "error": "no active workflow job"}), 404
    evs = [dict(e) for e in job.events if (e.get("event_id") or 0) > last_event_id]
    evs.sort(key=lambda e: e.get("event_id") or 0)
    if len(evs) > limit:
        evs = evs[-limit:]
    new_last = int(evs[-1].get("event_id") or last_event_id) if evs else last_event_id
    return jsonify({"success": True, "job_id": job.job_id, "status": job.status,
                    "last_event_id": new_last, "count": len(evs), "events": evs})


@app.route("/api/workflow/jobs", methods=["GET"])
def api_workflow_jobs():
    """观测: 列出工作流作业 (v4.03 F4) — 用于确认「客户端断流后作业仍在继续执行」。"""
    if not JOB_MGR_OK or _job_manager is None:
        return jsonify({"error": f"Workflow job manager not available: {JOB_MGR_ERR}"}), 503
    jobs = _job_manager.list_jobs()
    return jsonify({
        "success": True,
        "count": len(jobs),
        "running": sum(1 for j in jobs if j.get("status") == "running"),
        "jobs": jobs,
    })


@app.route("/api/human/orphans", methods=["GET"])
def api_human_orphans():
    """观测: 列出「已超时仍 pending」的孤儿检查点 (v4.03 F3)。

    Query: grace_seconds (默认 0 = 与工作流侧超时判定同口径)
    """
    if not HUMAN_CP_OK:
        return jsonify({"error": f"Human checkpoint not available: {HUMAN_CP_ERR}"}), 500
    try:
        grace = int(request.args.get("grace_seconds", 0) or 0)
    except (TypeError, ValueError):
        grace = 0
    try:
        rows = hc.list_orphan_checkpoints(grace)
        return jsonify({
            "success": True, "count": len(rows), "orphans": rows,
            "sweeper_interval": getattr(hc, "SWEEPER_INTERVAL", None),
            "sweeper_enabled": getattr(hc, "SWEEPER_ENABLED", None),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/workflow/interrupted", methods=["GET"])
def api_workflow_interrupted():
    """查询被中断的工作流 (后端重启后)"""
    interrupted = []
    if TASK_MANAGER_OK:
        try:
            interrupted = get_tasks_by_status(STATUS_INTERRUPTED, limit=20)
        except Exception:
            pass
    return jsonify({
        "count": len(interrupted),
        "tasks": [
            {
                "id": t.get("id", ""),
                "type": t.get("type", ""),
                "current_step": t.get("current_step", ""),
                "specs": t.get("specs", {}),
                "created_at": t.get("created_at", ""),
                "updated_at": t.get("updated_at", ""),
            }
            for t in interrupted
        ]
    })


@app.route("/api/workflow/seed_snapshot", methods=["GET"])
def api_workflow_seed_snapshot():
    """v4.06 (base_seeds 持久化回补): 返回最近一次 generate_designs 生成的种子快照。
    客户端在 SSE step_done 与本地 workflow_state 均缺失 base_seeds 时调用本接口回补,
    避免中间交付物第⑤段误报「未提取到种子 / 后端未返回 base_seeds」(实时日志已生成 5 组)。"""
    if not _orchestrator:
        return jsonify({"error": "Agent system not available"}), 500
    snap = getattr(_orchestrator, "_last_base_seeds", None)
    if not snap:
        return jsonify({"success": True, "available": False, "base_seeds": {}})
    return jsonify({"success": True, "available": True, "base_seeds": snap})


# ============================================================
# API：人工确认 / 成本报告 / 运营分析 (v2.05.03)
# ============================================================
@app.route("/api/workflow/human_input", methods=["POST"])
def api_workflow_human_input():
    """提交人工决策,解除工作流阻塞"""
    if not HUMAN_CP_OK:
        return jsonify({"error": f"Human checkpoint not available: {HUMAN_CP_ERR}"}), 500
    data = request.get_json()
    checkpoint_id = data.get("checkpoint_id", "").strip()
    decision = data.get("decision", "").strip()
    comment = data.get("comment", "")
    if not checkpoint_id or not decision:
        return jsonify({"error": "checkpoint_id and decision required"}), 400
    ok = hc.submit_decision(checkpoint_id, decision, comment)
    if not ok:
        return jsonify({"error": "Checkpoint not found or already decided"}), 404
    # v4.01.014: 支持客户端在弹窗中直接修改参数后提交覆盖数据
    override_data = data.get("override_data")
    if override_data and isinstance(override_data, dict):
        try:
            cp = hc.get_checkpoint(checkpoint_id)
            if cp:
                ctx = cp.get("context") or {}
                ctx.setdefault("override_data", {})
                # 合并用户覆盖数据到检查点上下文
                ctx["override_data"].update(override_data)
                # 回写内存与数据库中的 context
                hc.update_checkpoint_context(checkpoint_id, ctx)
        except Exception as e:
            logger.warning(f"[human_input] override_data 保存失败: {e}")
    return jsonify({"success": True, "checkpoint_id": checkpoint_id, "decision": decision})


@app.route("/api/human/checkpoints", methods=["GET"])
def api_human_checkpoints():
    """获取待处理的人工检查点"""
    if not HUMAN_CP_OK:
        return jsonify({"error": f"Human checkpoint not available: {HUMAN_CP_ERR}"}), 500
    project_id = request.args.get("project_id", "")
    try:
        pending = hc.list_pending_checkpoints(project_id or None)
        return jsonify({"success": True, "checkpoints": pending})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/human/checkpoint/<checkpoint_id>", methods=["GET"])
def api_human_checkpoint_detail(checkpoint_id: str):
    """获取检查点详情 (含决策上下文)"""
    if not HUMAN_CP_OK:
        return jsonify({"error": f"Human checkpoint not available: {HUMAN_CP_ERR}"}), 500
    cp = hc.get_checkpoint(checkpoint_id)
    if not cp:
        return jsonify({"error": "Checkpoint not found"}), 404
    return jsonify({"success": True, "checkpoint": cp,
                    "context": hc.get_decision_context(checkpoint_id)})


@app.route("/api/human/checkpoint/<checkpoint_id>/timeout", methods=["PUT", "POST"])
def api_human_checkpoint_timeout(checkpoint_id: str):
    """v4.01.016: 更新检查点超时时间 (用户弹窗手动延长)"""
    if not HUMAN_CP_OK:
        return jsonify({"error": f"Human checkpoint not available: {HUMAN_CP_ERR}"}), 500
    data = request.get_json(silent=True) or {}
    timeout_seconds = data.get("timeout_seconds", 0)
    try:
        timeout_seconds = max(10, int(timeout_seconds))
    except (TypeError, ValueError):
        return jsonify({"error": "timeout_seconds must be integer >= 10"}), 400
    ok, reason = hc.update_checkpoint_timeout(
        checkpoint_id, timeout_seconds,
        as_preference=bool(data.get("as_preference")))
    if not ok:
        msg = {"not_found": "Checkpoint not found", "already_decided": "Checkpoint already decided"}.get(
            reason, "Checkpoint not found or already decided")
        return jsonify({"success": False, "error": msg, "reason": reason}), 404
    return jsonify({"success": True, "checkpoint_id": checkpoint_id, "timeout_seconds": timeout_seconds})


@app.route("/api/cost/report/<project_id>", methods=["GET"])
def api_cost_report(project_id: str):
    """获取项目成本报告"""
    if not COST_OK:
        return jsonify({"error": f"Cost tracker not available: {COST_ERR}"}), 500
    try:
        report = get_project_cost_report(project_id)
        report["markdown"] = format_report_markdown(report)
        return jsonify({"success": True, "report": report})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/cost/analyze/<project_id>", methods=["GET"])
def api_cost_analyze(project_id: str):
    """获取项目运营分析建议"""
    if not COST_OK or not ANALYST_OK:
        return jsonify({"error": f"Analyst not available: {COST_ERR or ANALYST_ERR}"}), 500
    try:
        context = request.args.get("context", "{}")
        try:
            context = json.loads(context)
        except Exception:
            context = {}
        result = analyze_cost_report(project_id, context)
        return jsonify({"success": True, "analysis": result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ============================================================
# API：运营质量指标 (v4.01.029) — 人工修正率 / 返工率
# ============================================================
@app.route("/api/ops/quality-metrics", methods=["GET"])
def api_ops_quality_metrics():
    """运营质量指标: 人工修正率 = 人工修正项/总参数, 返工率 = redo/总决策

    Query: project_id (可选, 缺省仅返回全局), days (默认30)
    """
    if not QM_OK:
        return jsonify({"error": f"Quality metrics not available: {QM_ERR}"}), 500
    try:
        project_id = (request.args.get("project_id") or "").strip()
        try:
            days = int(request.args.get("days", 30))
        except Exception:
            days = 30
        payload = {
            "success": True,
            "global": qm_global_metrics(days),
        }
        if project_id:
            payload["project"] = qm_project_metrics(project_id)
        return jsonify(payload)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/ops/promote-questions", methods=["POST"])
def api_ops_promote_questions():
    """把高频人工修正参数提升为强制追问规则 (P2 闭环)

    Body: {"days":90, "apply":false}
    """
    if not QM_OK:
        return jsonify({"error": f"Quality metrics not available: {QM_ERR}"}), 500
    try:
        data = request.get_json(silent=True) or {}
        days = int(data.get("days", 90))
        apply_flag = bool(data.get("apply", False))
        result = qm_promote_questions(days=days, apply=apply_flag)
        return jsonify({"success": True, "result": result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ============================================================
# API：AI 团队工作流结果 (v2.04.01)
# ============================================================
@app.route("/api/ai_team/results", methods=["POST"])
def api_ai_team_save_result():
    """客户端上传 AI 团队工作流完整结果"""
    if not DB_OK:
        return jsonify({"error": "Database not available"}), 500
    data = request.get_json()
    if not data or not data.get("raw_text"):
        return jsonify({"error": "raw_text required"}), 400

    try:
        result_id = save_ai_team_result(data)
        return jsonify({"success": True, "id": result_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai_team/results", methods=["GET"])
def api_ai_team_results():
    """查询 AI 团队工作流结果列表 (管理端方案库)"""
    if not DB_OK:
        return jsonify({"error": "Database not available"}), 500
    client_id = request.args.get("client_id", "")
    try:
        raw_limit = request.args.get("limit", 50, type=int)
    except (TypeError, ValueError):
        raw_limit = 50
    # v4.02.10: 默认 50 (原 200), 硬上限 200, 防止超大 limit 触发一次性 jsonify OOM
    limit = max(1, min(raw_limit if raw_limit else 50, 200))
    try:
        offset = request.args.get("offset", 0, type=int) or 0
    except (TypeError, ValueError):
        offset = 0
    try:
        rows = get_ai_team_results(client_id=client_id or None, limit=limit, offset=offset)
        # 列表中不返回大文本字段，减少传输
        for r in rows:
            r.pop("review", None)
            r.pop("con_report", None)
            r.pop("report", None)

        # v4.02.10: 流式序列化 —— 逐行 yield, 避免一次性构建 200 行超大响应字符串
        # 导致 werkzeug serving 进程 MemoryError (见 svc_flask_dev.log 01:39:31)。
        def _generate():
            yield '{"success":true,"results":['
            first = True
            for r in rows:
                if not first:
                    yield ","
                first = False
                yield json.dumps(dict(r), default=str)
            yield '],"count":' + str(len(rows)) + '}'
        return Response(stream_with_context(_generate()), mimetype="application/json")
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai_team/results/<int:result_id>", methods=["GET"])
def api_ai_team_result_detail(result_id: int):
    """获取 AI 团队工作流结果详情"""
    if not DB_OK:
        return jsonify({"error": "Database not available"}), 500
    try:
        row = get_ai_team_result(result_id)
        if not row:
            return jsonify({"error": "not found"}), 404
        return jsonify({"success": True, "result": row})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ============================================================
# API：反馈（采纳/拒绝）
# ============================================================
@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    """记录方案采纳/拒绝反馈 (驱动数据飞轮)"""
    data = request.get_json()
    design_id = data.get("design_id")
    action = data.get("action", "")  # "adopt" or "reject"
    reason = data.get("reason", "")
    project_id = data.get("project_id", "")
    specs = data.get("specs", {})

    if not design_id or action not in ("adopt", "reject"):
        return jsonify({"error": "design_id and action (adopt/reject) required"}), 400

    # 优先走飞轮引擎 (创建技能模板 + 挖掘规则 + 沉淀失败特征)
    if FLYWHEEL_OK:
        try:
            if action == "adopt":
                result = _flywheel_adopt(design_id, specs, reason, project_id)
            else:
                result = _flywheel_reject(design_id, specs, reason, project_id)
            return jsonify(result)
        except Exception as e:
            # 飞轮失败, 降级到原始反馈记录
            pass

    if not FEEDBACK_OK:
        return jsonify({"error": "Feedback system not available"}), 500

    result = record_feedback(design_id, action, reason, project_id, specs)

    # 降级路径: 采纳时创建技能模板
    if action == "adopt" and EVOLUTION_OK:
        try:
            from db_tools import get_all_results
            all_results = get_all_results()
            solution = next((r for r in all_results if r["id"] == design_id), None)
            if solution:
                sol_data = {
                    "stator_od": solution.get("stator_od"),
                    "poles": solution.get("poles"),
                    "speed": solution.get("speed"),
                    "efficiency": solution.get("efficiency"),
                    "max_temp": solution.get("max_temp"),
                    "torque": solution.get("torque"),
                }
                template = create_skill_template(project_id, specs, sol_data, reason)
                result["skill_template"] = template.get("template_id", "")
        except Exception as e:
            result["skill_template_error"] = str(e)

    return jsonify(result)


@app.route("/api/feedback/stats")
def api_feedback_stats():
    """获取反馈统计"""
    if not FEEDBACK_OK:
        return jsonify({"error": "Feedback system not available"}), 500
    return jsonify(get_feedback_stats())


@app.route("/api/feedback/recent")
def api_feedback_recent():
    """获取最近反馈记录"""
    if not FEEDBACK_OK:
        return jsonify({"error": "Feedback system not available"}), 500
    limit = int(request.args.get("limit", 20))
    return jsonify({"records": get_recent_feedback(limit)})


# ============================================================
# API：进化仪表盘
# ============================================================
@app.route("/api/evolution/dashboard")
def api_evolution_dashboard():
    """获取进化仪表盘数据"""
    if not EVOLUTION_OK:
        return jsonify({"error": "Evolution engine not available"}), 500
    return jsonify(get_evolution_dashboard())


@app.route("/api/evolution/mine-rule", methods=["POST"])
def api_evolution_mine():
    """从历史数据挖掘新规则"""
    if not EVOLUTION_OK:
        return jsonify({"error": "Evolution engine not available"}), 500
    rule = mine_design_rule()
    if rule:
        return jsonify({"rule": rule, "message": "New rule mined successfully"})
    return jsonify({"message": "Not enough data to mine a rule (need 3+ adopted designs)"})


@app.route("/api/evolution/create-skill", methods=["POST"])
def api_evolution_create_skill():
    """手动创建技能模板"""
    if not EVOLUTION_OK:
        return jsonify({"error": "Evolution engine not available"}), 500

    data = request.get_json()
    project_id = data.get("project_id", "")
    specs = data.get("specs", {})
    solution = data.get("solution", {})
    feedback = data.get("feedback", "")

    template = create_skill_template(project_id, specs, solution, feedback)
    return jsonify({"template": template, "message": "Skill template created"})


# ============================================================
# API：数据飞轮
# ============================================================
@app.route("/api/flywheel/status")
def api_flywheel_status():
    """飞轮健康度: 采纳率/技能数/规则数/失败特征/AI接管率"""
    if not FLYWHEEL_OK:
        return jsonify({"error": "Flywheel engine not available"}), 500
    return jsonify(get_flywheel_status())


# ============================================================
# API：蒙特卡洛沙盘推演
# ============================================================
@app.route("/api/sandbox/simulate", methods=["POST"])
def api_sandbox_simulate():
    """对方案做蒙特卡洛推演, 返回失败率/分位/最惨情形"""
    try:
        from monte_carlo import run_simulation, run_batch
    except Exception as e:
        return jsonify({"error": f"monte_carlo module not available: {e}"}), 500

    data = request.get_json()
    solutions = data.get("solutions", [])
    specs = data.get("specs", {})
    n_trials = int(data.get("n_trials", 1000))

    if not solutions:
        return jsonify({"error": "solutions required"}), 400

    if len(solutions) == 1:
        result = run_simulation(solutions[0], specs, n_trials=n_trials)
    else:
        result = run_batch(solutions, specs, n_trials=n_trials)
    return jsonify({"result": result})


# ============================================================
# API：辩证制衡独立触发 (对已有结果跑收敛阵营)
# ============================================================
@app.route("/api/convergence/run", methods=["POST"])
def api_convergence_run():
    """对已有结果独立触发收敛阵营 (4个收敛agent)"""
    if not AGENT_OK or not _orchestrator:
        return jsonify({"error": "Agent system not available"}), 500

    data = request.get_json()
    results = data.get("results", [])
    specs = data.get("specs", {})

    if not results:
        return jsonify({"error": "results required"}), 400

    try:
        rev = _orchestrator.reverse_reviewer.safe_run(results=results, specs=specs)
        real = _orchestrator.reality_checker.safe_run(results=results, specs=specs)
        sand = _orchestrator.sandbox_simulator.safe_run(results=results, specs=specs)
        dev = _orchestrator.devils_advocate.safe_run(
            results=results, specs=specs,
            convergence_findings={
                "reverse": rev.get("con_report", "")[:200],
                "reality": real.get("con_report", "")[:200],
                "sandbox": sand.get("con_report", "")[:200],
            })
        con_report = _orchestrator._assemble_con_report({
            "reverse": rev.get("con_report", ""),
            "reality": real.get("con_report", ""),
            "sandbox": sand.get("con_report", ""),
            "devils_advocate": dev.get("con_report", ""),
        })
        return jsonify({
            "con_report": con_report,
            "findings": {
                "reverse": rev.get("con_report", ""),
                "reality": real.get("con_report", ""),
                "sandbox": sand.get("con_report", ""),
                "devils_advocate": dev.get("con_report", ""),
            },
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# API：审计日志
# ============================================================
@app.route("/api/audit/logs")
def api_audit_logs():
    """获取审计日志（支持过滤）"""
    if not AUDIT_OK:
        return jsonify({"error": "Audit system not available"}), 500
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    agent = request.args.get("agent", "")
    action = request.args.get("action", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    return jsonify({"logs": audit_logger.get_recent_logs(limit, offset, agent, action, start_date, end_date)})


@app.route("/api/audit/stats")
def api_audit_stats():
    """获取审计统计"""
    if not AUDIT_OK:
        return jsonify({"error": "Audit system not available"}), 500
    return jsonify(audit_logger.get_stats())


@app.route("/api/audit/agent-metrics")
def api_audit_agent_metrics():
    """获取智能体性能指标"""
    if not AUDIT_OK:
        return jsonify({"error": "Audit system not available"}), 500
    return jsonify({"metrics": audit_logger.get_agent_metrics()})


# ============================================================
# API：网络访问管理（局域网/Tailscale/二维码/健康检查）
# ============================================================
import socket
import subprocess
import io as _io
import base64 as _base64

# 端口号常量 (v1.03.01: 从 Config 获取)
_NET_PORT = Config.FLASK_PORT


def _get_all_ips():
    """获取本机所有 IPv4 地址，区分局域网和 Tailscale"""
    lan_ips = []
    tailscale_ips = []
    try:
        hostname = socket.gethostname()
        # 获取所有网卡地址
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            if ip.startswith("100."):
                # Tailscale 的 CGNAT 地址段 100.64.0.0/10
                tailscale_ips.append(ip)
            else:
                lan_ips.append(ip)
    except Exception:
        pass
    # 去重
    lan_ips = list(dict.fromkeys(lan_ips))
    tailscale_ips = list(dict.fromkeys(tailscale_ips))
    return lan_ips, tailscale_ips


def _check_tailscale():
    """检测 Tailscale 安装和运行状态"""
    result = {"installed": False, "running": False, "ip": None, "hostname": None}
    # 检查 CLI 是否存在
    try:
        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        if proc.returncode == 0:
            import json as _json
            ts_data = _json.loads(proc.stdout)
            result["installed"] = True
            result["running"] = ts_data.get("BackendState") == "Running"
            result["ip"] = ts_data.get("Self", {}).get("TailscaleIPs", [None])[0]
            result["hostname"] = ts_data.get("Self", {}).get("HostName")
    except FileNotFoundError:
        result["installed"] = False
    except Exception:
        result["installed"] = True  # 可能装了但状态异常
    return result


def _check_port_listening(port=_NET_PORT):
    """检测指定端口是否在监听"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            result = s.connect_ex(("127.0.0.1", port))
            return result == 0
    except Exception:
        return False


# ============================================================
# v1.03.02: 全局进度广播 API (跨设备同步 + 任务终止)
# ============================================================
@app.route("/api/progress/stream")
def api_progress_stream():
    """SSE 端点: 任何客户端可订阅, 实时接收其他客户端的工作流/求解进度"""
    if not PROGRESS_HUB_OK:
        return jsonify({"error": "ProgressHub not available"}), 500

    # v4.00.03: 标记用户在线 (用于 report_review 自动延长超时)
    if HUMAN_CP_OK:
        hc.touch_user_activity()

    def generate():
        q = progress_hub.subscribe()
        try:
            # 先发送当前状态快照
            state = progress_hub.get_state()
            yield f"data: {json.dumps({'type': 'hub_snapshot', 'state': state}, ensure_ascii=False, default=str)}\n\n"

            while True:
                try:
                    event = q.get(timeout=15)
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                except queue.Empty:
                    # 心跳, 保持连接
                    yield f"data: {json.dumps({'type': 'heartbeat', 'time': datetime.now().strftime('%H:%M:%S')})}\n\n"
        except GeneratorExit:
            pass
        finally:
            progress_hub.unsubscribe(q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/progress/status")
def api_progress_status():
    """获取当前进度状态快照 (轮询用)"""
    if not PROGRESS_HUB_OK:
        return jsonify({"active": False, "error": "ProgressHub not available"})
    return jsonify(progress_hub.get_state())


@app.route("/api/progress/cancel", methods=["POST"])
def api_progress_cancel():
    """终止当前正在运行的工作流/求解任务"""
    if not PROGRESS_HUB_OK:
        return jsonify({"error": "ProgressHub not available"}), 500

    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "user_requested")
    # v1.04.15: ★取消按 job_id 隔离
    #   显式给 job_id ⇒ 只取消该作业; 给 task_id ⇒ 反查其作业;
    #   都不给 ⇒ 仅当「恰好一个活跃作业」才生效, 多作业并发时**拒绝盲取消**,
    #   避免任一客户端的一次 cancel 连带杀死同进程其它并发作业(RUN34 事故根因)。
    job_id = str(data.get("job_id") or request.args.get("job_id") or "").strip()
    task_id = str(data.get("task_id") or request.args.get("task_id") or "").strip()
    if job_id:
        affected = progress_hub.request_cancel(reason, job_id=job_id)
    elif task_id:
        affected = progress_hub.request_cancel_task(task_id, reason=reason)
    else:
        affected = progress_hub.request_cancel(reason)
    return jsonify({"status": "cancel_requested", "reason": reason,
                    "job_id": job_id, "affected": affected,
                    "note": "" if affected else "多作业并发且未指定 job_id, 已拒绝盲取消(防误杀)"})


# ============================================================
# v1.03.03: 全局任务管理 API
# ============================================================
@app.route("/api/tasks")
def api_tasks_list():
    """获取所有任务列表"""
    if not TASK_MANAGER_OK:
        return jsonify({"error": "TaskManager not available"}), 500
    status_filter = request.args.get("status")
    limit = int(request.args.get("limit", 50))
    if status_filter:
        tasks = get_tasks_by_status(status_filter, limit=limit)
    else:
        tasks = get_all_tasks(limit=limit)
    return jsonify({"tasks": tasks, "total": len(tasks)})


@app.route("/api/tasks/active")
def api_tasks_active():
    """获取活跃任务 (queued + running)"""
    if not TASK_MANAGER_OK:
        return jsonify({"error": "TaskManager not available"}), 500
    tasks = get_active_tasks()
    return jsonify({"tasks": tasks, "total": len(tasks)})


@app.route("/api/task/<task_id>")
def api_task_detail(task_id):
    """获取单个任务详情"""
    if not TASK_MANAGER_OK:
        return jsonify({"error": "TaskManager not available"}), 500
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)


@app.route("/api/task/<task_id>/cancel", methods=["POST"])
def api_task_cancel(task_id):
    """取消指定任务"""
    if not TASK_MANAGER_OK:
        return jsonify({"error": "TaskManager not available"}), 500
    # 先在数据库中标记取消
    success = _cancel_task(task_id)
    if not success:
        return jsonify({"error": "Task not found or already finished"}), 404
    # 同时通过 ProgressHub 发送取消信号 (终止正在运行的 Worker)
    if PROGRESS_HUB_OK:
        # v1.04.15: 按 task_id 反查其作业并**只**取消它(取消隔离, 不误杀并发作业)
        progress_hub.request_cancel_task(task_id, reason=f"task_cancelled:{task_id}")
    return jsonify({"status": "cancelled", "task_id": task_id})


@app.route("/api/task/<task_id>/priority", methods=["PUT"])
def api_task_priority(task_id):
    """更新任务优先级"""
    if not TASK_MANAGER_OK:
        return jsonify({"error": "TaskManager not available"}), 500
    data = request.get_json(silent=True) or {}
    priority = data.get("priority")
    if priority is None or not isinstance(priority, int):
        return jsonify({"error": "priority (int 0-10) required"}), 400
    success = _update_task_priority(task_id, priority)
    if not success:
        return jsonify({"error": "Task not found"}), 404
    return jsonify({"status": "ok", "task_id": task_id, "priority": priority})


@app.route("/api/network/status")
def api_network_status():
    """获取网络访问状态：本机IP、Tailscale、端口监听、绑定地址"""
    lan_ips, ts_ips_from_scan = _get_all_ips()
    tailscale = _check_tailscale()

    # 优先用 Tailscale CLI 的结果
    ts_ip = tailscale.get("ip") or (ts_ips_from_scan[0] if ts_ips_from_scan else None)

    env = read_env_file()
    bind_host = env.get("BIND_HOST", "127.0.0.1")

    return jsonify({
        "lan_ips": lan_ips,
        "tailscale": {
            "installed": tailscale["installed"],
            "running": tailscale["running"],
            "ip": ts_ip,
            "hostname": tailscale["hostname"],
        },
        "port": _NET_PORT,
        "port_listening": _check_port_listening(),
        "bind_host": bind_host,
        "current_url": f"http://{bind_host}:{_NET_PORT}" if bind_host != "0.0.0.0" else f"http://127.0.0.1:{_NET_PORT}",
    })


def _generate_qr_b64(url):
    """生成二维码的 base64 data URI"""
    import qrcode
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    img_b64 = _base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{img_b64}"


@app.route("/api/network/qrcode")
def api_network_qrcode():
    """生成访问二维码（局域网 + Tailscale），返回 base64 PNG"""
    try:
        import qrcode  # noqa: F401
    except ImportError:
        return jsonify({"error": "qrcode 库未安装，请运行: pip install qrcode Pillow"}), 500

    lan_ips, _ = _get_all_ips()
    tailscale = _check_tailscale()

    # 局域网二维码
    lan_ip = lan_ips[0] if lan_ips else "127.0.0.1"
    lan_url = f"http://{lan_ip}:{_NET_PORT}"
    lan_qr = _generate_qr_b64(lan_url)

    result = {
        "lan": {
            "url": lan_url,
            "qr_code": lan_qr,
            "ip": lan_ip,
            "port": _NET_PORT,
        },
        # 兼容旧字段
        "url": lan_url,
        "qr_code": lan_qr,
        "ip": lan_ip,
        "port": _NET_PORT,
    }

    # Tailscale 二维码（如果可用）
    ts_ip = tailscale.get("ip")
    if tailscale.get("running") and ts_ip:
        ts_url = f"http://{ts_ip}:{_NET_PORT}"
        result["tailscale"] = {
            "url": ts_url,
            "qr_code": _generate_qr_b64(ts_url),
            "ip": ts_ip,
            "port": _NET_PORT,
            "hostname": tailscale.get("hostname"),
        }

    return jsonify(result)


@app.route("/api/network/check-access", methods=["POST"])
def api_network_check_access():
    """外部可达性检测：从本机模拟外部访问"""
    data = request.get_json() or {}
    target = data.get("target", "lan")  # lan / tailscale / localhost

    lan_ips, _ = _get_all_ips()
    tailscale = _check_tailscale()

    if target == "tailscale":
        ip = tailscale.get("ip")
        if not ip:
            return jsonify({"reachable": False, "error": "Tailscale 未运行或未安装"})
    elif target == "lan":
        ip = lan_ips[0] if lan_ips else None
        if not ip:
            return jsonify({"reachable": False, "error": "未找到局域网 IP"})
    else:
        ip = "127.0.0.1"

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            result = s.connect_ex((ip, _NET_PORT))
            if result == 0:
                return jsonify({
                    "reachable": True,
                    "ip": ip,
                    "port": _NET_PORT,
                    "url": f"http://{ip}:{_NET_PORT}",
                    "message": f"✅ 可达：{ip}:{_NET_PORT} 端口已开放",
                })
            else:
                return jsonify({
                    "reachable": False,
                    "ip": ip,
                    "port": _NET_PORT,
                    "error": f"❌ 不可达：{ip}:{_NET_PORT} 端口未开放（可能 Flask 未绑定该 IP，或防火墙拦截）",
                })
    except Exception as e:
        return jsonify({"reachable": False, "error": f"检测失败: {str(e)}"})


@app.route("/api/network/config", methods=["POST"])
def api_network_config():
    """更新网络绑定配置（BIND_HOST）"""
    data = request.get_json() or {}
    bind_host = data.get("bind_host", "127.0.0.1")

    # 验证地址格式
    valid_hosts = ["127.0.0.1", "0.0.0.0"]
    lan_ips, ts_ips = _get_all_ips()
    valid_hosts.extend(lan_ips)
    if ts_ips:
        valid_hosts.extend(ts_ips)

    # 也允许用户输入的任意 IP（可能切换网络后 IP 变了）
    try:
        socket.inet_aton(bind_host)
    except socket.error:
        return jsonify({"status": "error", "message": f"无效的 IP 地址: {bind_host}"}), 400

    write_env_file({"BIND_HOST": bind_host})
    load_dotenv(ENV_PATH, override=False)

    mode_desc = {
        "127.0.0.1": "仅本机访问（最安全）",
        "0.0.0.0": "所有网卡（局域网+Tailscale 均可访问）",
    }.get(bind_host, f"绑定到 {bind_host}")

    return jsonify({
        "status": "ok",
        "bind_host": bind_host,
        "mode_desc": mode_desc,
        "message": f"绑定地址已更新为 {bind_host}（{mode_desc}）。需要重启 Web UI 生效。",
    })


# ============================================================
# v2.02.01: 认证 API (注册 / 登录 / 验证 / 个人资料)
# ============================================================
@app.route("/api/auth/register", methods=["POST"])
def api_auth_register():
    """用户注册"""
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password", "")
    display_name = data.get("display_name", username)

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if len(password) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400
    if not AUTH_OK:
        return jsonify({"error": "认证系统不可用"}), 500

    if get_user_by_username(username):
        return jsonify({"error": "用户名已存在"}), 409
    if email and get_user_by_email(email):
        return jsonify({"error": "邮箱已注册"}), 409

    try:
        user_id = create_user(
            username=username,
            email=email or None,
            password_hash=hash_password(password),
            display_name=display_name,
        )
        token = generate_token(user_id, username, "user", "trial")
        return jsonify({
            "status": "ok",
            "user_id": user_id,
            "username": username,
            "token": token,
            "role": "user",
            "membership_level": "trial",
            "points_balance": 100,
        })
    except Exception as e:
        return jsonify({"error": f"注册失败: {str(e)}"}), 500


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    """用户登录, 返回 JWT"""
    from flask import make_response
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if not AUTH_OK:
        return jsonify({"error": "认证系统不可用"}), 500

    # v4.00.03: 标记用户在线
    if HUMAN_CP_OK:
        hc.touch_user_activity()

    user = get_user_by_username(username)
    if not user:
        # 也尝试用 email 登录
        user = get_user_by_email(username)
    if not user or not verify_password(password, user.get("password_hash", "")):
        return jsonify({"error": "用户名或密码错误"}), 401
    if user.get("status") != "active":
        return jsonify({"error": "账号已被禁用"}), 403

    # 更新最后登录时间
    update_user(user["id"], last_login=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    token = generate_token(
        user["id"], user["username"], user.get("role", "user"),
        user.get("membership_level", "trial")
    )
    resp = make_response(jsonify({
        "status": "ok",
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user.get("email"),
            "display_name": user.get("display_name"),
            "role": user.get("role", "user"),
            "membership_level": user.get("membership_level", "trial"),
            "points_balance": user.get("points_balance", 0),
        }
    }))
    # 登录时清除 dev 登出 cookie (恢复自动登录状态)
    if Config.IS_DEV:
        resp.delete_cookie("motorai_skip_auto")
    return resp


@app.route("/api/auth/verify")
def api_auth_verify():
    """验证 token 有效性"""
    if not AUTH_OK:
        return jsonify({"valid": False})
    token = extract_token_from_request()
    if not token:
        # dev 环境自动登录 (cookie 未设置时)
        if g.user:
            return jsonify({"valid": True, "user": current_user_dict(), "auto_login": is_dev_auto_login()})
        return jsonify({"valid": False, "admin_hint": should_show_admin_hint()})
    payload = verify_token(token)
    if not payload:
        return jsonify({"valid": False, "admin_hint": should_show_admin_hint()})
    user = get_user_by_id(payload["user_id"])
    if not user or user.get("status") != "active":
        return jsonify({"valid": False, "admin_hint": should_show_admin_hint()})
    return jsonify({"valid": True, "user": current_user_dict()})


@app.route("/api/auth/profile")
def api_auth_profile():
    """获取当前用户资料"""
    if not g.user:
        return jsonify({"error": "未登录"}), 401
    return jsonify({"user": current_user_dict(), "membership": get_membership_info(g.user.get("membership_level", "trial"))})


@app.route("/api/auth/profile", methods=["PUT"])
def api_auth_update_profile():
    """更新个人资料"""
    if not g.user:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json() or {}
    fields = {}
    for k in ("display_name", "email", "phone"):
        if k in data:
            fields[k] = data[k]
    if fields:
        update_user(g.user["id"], **fields)
    return jsonify({"status": "ok", "user": current_user_dict()})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    """登出 (无状态 JWT, 客户端删除 token 即可)

    dev 环境: 设置 cookie 阻止自动登录, 直到用户手动登录或恢复自动登录
    """
    from flask import make_response
    resp = make_response(jsonify({"status": "ok"}))
    if Config.IS_DEV:
        resp.set_cookie("motorai_skip_auto", "1", max_age=86400, httponly=True)
    return resp


@app.route("/api/auth/dev-restore", methods=["POST"])
def api_auth_dev_restore():
    """dev 环境: 恢复自动登录 (清除 cookie)"""
    from flask import make_response
    if not Config.IS_DEV:
        return jsonify({"error": "仅调试版可用"}), 403
    resp = make_response(jsonify({"status": "ok"}))
    resp.delete_cookie("motorai_skip_auto")
    return resp


# ============================================================
# v2.02.01: 管理员 API (用户管理)
# ============================================================
@app.route("/api/admin/users")
def api_admin_users():
    """管理员获取用户列表"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    users = get_all_users()
    return jsonify({"users": users, "total": len(users)})


@app.route("/api/admin/user/<int:user_id>", methods=["PUT"])
def api_admin_update_user(user_id):
    """管理员修改用户 (角色/会员等级/状态)"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    data = request.get_json() or {}
    fields = {}
    for k in ("role", "membership_level", "status", "display_name", "points_balance", "expires_at", "can_use_host_motorcad"):
        if k in data:
            fields[k] = data[k]
    if not fields:
        return jsonify({"error": "没有要更新的字段"}), 400
    update_user(user_id, **fields)
    return jsonify({"status": "ok"})


@app.route("/api/admin/user/<int:user_id>/points", methods=["POST"])
def api_admin_adjust_points(user_id):
    """管理员调整用户积分"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    data = request.get_json() or {}
    amount = data.get("amount", 0)
    reason = data.get("reason", "管理员调整")
    if amount >= 0:
        new_balance = add_points(user_id, amount, reason)
    else:
        result = spend_points(user_id, -amount, reason)
        if not result["success"]:
            return jsonify({"error": result.get("error", "操作失败")}), 400
        new_balance = result["balance"]
    return jsonify({"status": "ok", "new_balance": new_balance})


@app.route("/api/admin/user/create", methods=["POST"])
def api_admin_create_user():
    """管理员创建用户"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    email = (data.get("email") or "").strip()
    display_name = data.get("display_name") or username
    role = data.get("role", "user")
    membership_level = data.get("membership_level", "trial")
    can_use_mc = data.get("can_use_host_motorcad", 0)
    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if len(username) < 3 or len(username) > 20:
        return jsonify({"error": "用户名长度需 3-20 个字符"}), 400
    if len(password) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400
    if get_user_by_username(username):
        return jsonify({"error": "用户名已存在"}), 409
    if email and get_user_by_email(email):
        return jsonify({"error": "邮箱已被使用"}), 409
    try:
        pw_hash = hash_password(password)
        user_id = create_user(
            username=username, email=email, password_hash=pw_hash,
            display_name=display_name, role=role, membership_level=membership_level,
            can_use_host_motorcad=can_use_mc,
        )
        return jsonify({"status": "ok", "user_id": user_id, "username": username})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"创建失败: {e}"}), 500


@app.route("/api/admin/user/<int:user_id>/points/history")
def api_admin_user_points_history(user_id):
    """管理员查看指定用户的积分变动记录"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    if not POINTS_OK:
        return jsonify({"error": "积分系统不可用"}), 500
    history = get_points_history(user_id, limit=100)
    return jsonify({"history": history, "total": len(history)})


@app.route("/api/admin/user/<int:user_id>/reset-password", methods=["POST"])
def api_admin_reset_password(user_id):
    """管理员重置用户密码"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    data = request.get_json() or {}
    new_password = data.get("new_password") or ""
    if len(new_password) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400
    pw_hash = hash_password(new_password)
    update_user(user_id, password_hash=pw_hash)
    return jsonify({"status": "ok"})


@app.route("/api/auth/change-password", methods=["POST"])
def api_auth_change_password():
    """用户修改自己的密码"""
    if not g.user:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json() or {}
    old_password = data.get("old_password") or ""
    new_password = data.get("new_password") or ""
    if not old_password or not new_password:
        return jsonify({"error": "请输入旧密码和新密码"}), 400
    if len(new_password) < 6:
        return jsonify({"error": "新密码至少 6 位"}), 400
    if not verify_password(old_password, g.user.get("password_hash", "")):
        return jsonify({"error": "旧密码不正确"}), 401
    pw_hash = hash_password(new_password)
    update_user(g.user["id"], password_hash=pw_hash)
    return jsonify({"status": "ok"})


# ============================================================
# v1.03.01: 客户端设备管理 API
# ============================================================
_CLIENT_SETTINGS_FILE = os.path.join(BASE_DIR, "data", "client_settings.json")


def _load_client_settings():
    """加载客户端设置 (JSON 文件)"""
    try:
        if os.path.exists(_CLIENT_SETTINGS_FILE):
            with open(_CLIENT_SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_client_settings(settings):
    """保存客户端设置"""
    os.makedirs(os.path.dirname(_CLIENT_SETTINGS_FILE), exist_ok=True)
    with open(_CLIENT_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# 默认客户端配置
DEFAULT_CLIENT_CONFIG = {
    "allowed_solve_modes": ["local", "remote", "ask"],
    "privacy_level": "strict",
    "max_concurrent_tasks": 1,
    "ai_models_allowed": ["auto", "cloud", "local"],
    "auto_update": True,
    "update_channel": "stable",
    "motorcad_required": False,
    "notes": "",
}


@app.route("/api/admin/clients")
def api_admin_clients():
    """管理员获取所有客户端列表"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    settings = _load_client_settings()
    from datetime import datetime, timedelta
    now = datetime.now()
    clients = []
    for client_id, cfg in settings.items():
        # 离线检测: last_seen 超过5分钟视为离线
        online = cfg.get("online", False)
        last_seen_str = cfg.get("last_seen", "")
        if last_seen_str and online:
            try:
                last_seen = datetime.fromisoformat(last_seen_str)
                if (now - last_seen) > timedelta(minutes=5):
                    online = False
            except (ValueError, TypeError):
                pass
        clients.append({
            "client_id": client_id,
            "hostname": cfg.get("hostname", ""),
            "last_seen": cfg.get("last_seen", ""),
            "registered_at": cfg.get("registered_at", ""),
            "tailscale_ip": cfg.get("tailscale_ip", ""),
            "version": cfg.get("version", ""),
            "config": cfg.get("config", DEFAULT_CLIENT_CONFIG),
            "online": online,
        })
    return jsonify({"clients": clients, "total": len(clients)})


@app.route("/api/admin/clients/<client_id>", methods=["PUT"])
def api_admin_update_client(client_id):
    """管理员更新客户端配置
    v4.00.003: max_concurrent_tasks 同步推送到后端 orchestrator (控制最大并发数)
    """
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    data = request.get_json() or {}
    settings = _load_client_settings()
    if client_id not in settings:
        settings[client_id] = {"config": dict(DEFAULT_CLIENT_CONFIG)}
    # 更新配置字段
    cfg = settings[client_id].setdefault("config", dict(DEFAULT_CLIENT_CONFIG))
    for key in ("allowed_solve_modes", "privacy_level", "max_concurrent_tasks",
                "ai_models_allowed", "auto_update", "update_channel",
                "motorcad_required", "notes"):
        if key in data:
            cfg[key] = data[key]
    _save_client_settings(settings)

    # v4.00.003: 同步推送 max_concurrent_tasks 到后端 orchestrator
    # 设计语义: 任何客户端的"最大并发任务"设置都会更新全局的 MOTOR_MAX_CONCURRENT
    # 因为该值代表"后端 orchestrator 的全局并发上限"
    max_conc_updated = False
    new_max = None
    old_max = None
    if "max_concurrent_tasks" in data:
        try:
            new_max = max(1, min(10, int(data["max_concurrent_tasks"])))
        except (TypeError, ValueError):
            new_max = None
        if new_max is not None:
            # 1) 写入 os.environ (立即生效)
            os.environ["MOTOR_MAX_CONCURRENT"] = str(new_max)
            # 2) 更新 orchestrator 单例 (线程安全)
            if _orchestrator is not None:
                with _orchestrator._workflow_lock:
                    old_max = _orchestrator._max_concurrent
                    _orchestrator._max_concurrent = new_max
                    # 如果之前已满, 新值更大, 唤醒等待者
                    if old_max < new_max:
                        _orchestrator._workflow_slot_event.set()
            # 3) 持久化到 .env 文件 (重启后保留)
            try:
                write_env_file({"MOTOR_MAX_CONCURRENT": str(new_max)})
            except Exception as e:
                print(f"[update_client] 写 .env 失败 (非致命): {e}")
            max_conc_updated = True
            print(f"[update_client] max_concurrent_tasks: {old_max} -> {new_max} (client={client_id})")

    return jsonify({
        "status": "ok",
        "client_id": client_id,
        "config": cfg,
        "max_concurrent_updated": max_conc_updated,
        "max_concurrent_new_value": new_max,
        "max_concurrent_old_value": old_max,
    })


@app.route("/api/admin/clients/<client_id>", methods=["GET"])
def api_admin_get_client(client_id):
    """管理员获取单个客户端配置"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    settings = _load_client_settings()
    client = settings.get(client_id, {})
    return jsonify({
        "client_id": client_id,
        "hostname": client.get("hostname", ""),
        "last_seen": client.get("last_seen", ""),
        "tailscale_ip": client.get("tailscale_ip", ""),
        "version": client.get("version", ""),
        "config": client.get("config", DEFAULT_CLIENT_CONFIG),
    })


# ---- v4.00.004: AI 团队工作流配置 API ----

@app.route("/api/admin/workflow-config", methods=["GET"])
def api_admin_get_workflow_config():
    """获取 AI 团队工作流配置 (并发/队列超时/检查点/智能体模型)"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    if not WC_OK:
        return jsonify({"error": "workflow_config 模块未加载"}), 500
    config = _wc_get()
    # 附加 Ollama 已安装模型列表 (供前端下拉选择)
    ollama_models = []
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            tags_data = json.loads(resp.read())
            ollama_models = [m.get("name", "") for m in tags_data.get("models", [])]
    except Exception:
        pass
    # B2: 附加可分配的「云端模型」目录 (供管理端「模型总路由卡片」路由总表下拉)。
    #   来源: cloud_services_config.get_all() 的 catalog + catalog_with_display。
    #   此前前端下拉只给 ollama_models ⇒ 无法为智能体指定云端模型 (GAP1)。
    available_cloud_models = []
    try:
        try:
            from Core import cloud_services_config as _csc
        except Exception:
            import cloud_services_config as _csc  # type: ignore
        _all = _csc.get_all() or {}
        _cat = _all.get("catalog") or {}
        _catd = _all.get("catalog_with_display") or {}
        _vendors = _all.get("vendors") or {}
        for _vn, _models in _cat.items():
            _disp = _catd.get(_vn) or {}
            _ven = bool((_vendors.get(_vn) or {}).get("enabled", False))
            for _m in (_models or []):
                if not _m:
                    continue
                available_cloud_models.append({
                    "model": _m,
                    "vendor": _vn,
                    "label": _disp.get(_m, _m),
                    "vendor_enabled": _ven,
                })
    except Exception:
        pass
    return jsonify({
        "config": config,
        "ollama_models": ollama_models,
        "available_cloud_models": available_cloud_models,
        "default_model": _resolve_local_model_name(),
    })


@app.route("/api/admin/workflow-config", methods=["PUT"])
def api_admin_update_workflow_config():
    """更新 AI 团队工作流配置 (即时生效, 无需重启)
    同步更新: os.environ + orchestrator._max_concurrent + 检查点覆盖 + 智能体模型映射 + JSON 持久化
    """
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    if not WC_OK:
        return jsonify({"error": "workflow_config 模块未加载"}), 500

    data = request.get_json() or {}
    current = _wc_get()

    # 合并配置 (只更新提供的字段)
    new_config = dict(current)
    if "max_concurrent_workflows" in data:
        try:
            new_config["max_concurrent_workflows"] = max(1, min(10, int(data["max_concurrent_workflows"])))
        except (TypeError, ValueError):
            pass
    if "queue_timeout_seconds" in data:
        try:
            new_config["queue_timeout_seconds"] = max(60, min(3600, int(data["queue_timeout_seconds"])))
        except (TypeError, ValueError):
            pass
    if "human_checkpoint_timeouts" in data and isinstance(data["human_checkpoint_timeouts"], dict):
        merged = dict(current.get("human_checkpoint_timeouts", {}))
        for k, v in data["human_checkpoint_timeouts"].items():
            try:
                merged[k] = max(10, int(v))  # 最小 10 秒
            except (TypeError, ValueError):
                pass
        new_config["human_checkpoint_timeouts"] = merged
    if "agent_model_map" in data and isinstance(data["agent_model_map"], dict):
        merged = dict(current.get("agent_model_map", {}))
        for k, v in data["agent_model_map"].items():
            merged[k] = str(v) if v else ""
        new_config["agent_model_map"] = merged
    # v4.01.059 (P1-#5): 决策门质量门禁硬约束可经管理端配置 (即时生效)
    if "quality_gate" in data and isinstance(data["quality_gate"], dict):
        qg_merged = dict(current.get("quality_gate", {}))
        _qg = data["quality_gate"]
        for _scalar in ("confidence_threshold", "max_redo"):
            if _scalar in _qg:
                try:
                    qg_merged[_scalar] = float(_qg[_scalar]) if _scalar == "confidence_threshold" else int(_qg[_scalar])
                except (TypeError, ValueError):
                    pass
        if isinstance(_qg.get("hard_constraints"), dict):
            _hc = dict(qg_merged.get("hard_constraints", {}))
            for _hk, _hv in _qg["hard_constraints"].items():
                try:
                    _hc[_hk] = float(_hv)
                except (TypeError, ValueError):
                    _hc[_hk] = _hv
            qg_merged["hard_constraints"] = _hc
        if isinstance(_qg.get("efficiency_min_by_type"), dict):
            _bt = dict(qg_merged.get("efficiency_min_by_type", {}))
            for _tk, _tv in _qg["efficiency_min_by_type"].items():
                try:
                    _bt[_tk] = float(_tv)
                except (TypeError, ValueError):
                    pass
            qg_merged["efficiency_min_by_type"] = _bt
        new_config["quality_gate"] = qg_merged
    if "default_model" in data and data["default_model"]:
        new_config["default_model"] = str(data["default_model"])
        # 同步运行时环境变量 (get_agent_model 等兜底读取点即时生效)
        os.environ["LOCAL_MODEL_NAME"] = str(data["default_model"])
        # 回写 .env.<ENV> 文件 (GET 路由 _resolve_local_model_name 直接读文件)
        try:
            write_env_file({"LOCAL_MODEL_NAME": str(data["default_model"])})
        except Exception as e:
            print(f"[workflow-config] 写 LOCAL_MODEL_NAME 失败 (非致命): {e}")

    # 1) 持久化到 JSON + 更新运行时状态
    _wc_save(new_config)

    # 2) 同步 orchestrator._max_concurrent
    if _orchestrator is not None:
        try:
            _orchestrator.set_max_concurrent(new_config["max_concurrent_workflows"])
        except Exception as e:
            print(f"[workflow-config] 同步 orchestrator 失败: {e}")

    # 3) 同步 .env 文件 (MOTOR_MAX_CONCURRENT 持久化)
    try:
        write_env_file({"MOTOR_MAX_CONCURRENT": str(new_config["max_concurrent_workflows"])})
    except Exception as e:
        print(f"[workflow-config] 写 .env 失败 (非致命): {e}")

    return jsonify({
        "status": "ok",
        "config": new_config,
        "applied": {
            "default_model": new_config.get("default_model"),
            "max_concurrent_workflows": new_config["max_concurrent_workflows"],
            "queue_timeout_seconds": new_config["queue_timeout_seconds"],
            "checkpoint_overrides": len(new_config["human_checkpoint_timeouts"]),
            "agent_model_overrides": sum(1 for v in new_config["agent_model_map"].values() if v),
        },
    })


# ---- v4.00.013: AI 流程设置 API (5 段流程策略 + 量化评分权重模板 + 版本回滚) ----

def _afc_guard():
    """统一鉴权 + 模块可用性检查: 通过返回 None, 否则返回 Flask 响应元组"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    if not AFC_OK:
        return jsonify({"error": "ai_flow_config 模块未加载"}), 500
    return None


def _afc_operator() -> str:
    """当前操作人 (审计与版本历史用)"""
    try:
        return (g.user or {}).get("username") or "admin"
    except Exception:
        return "admin"


def _afc_audit(action: str, parameters=None, result=None, status: str = "success"):
    """写审计日志 (失败不阻断主流程)"""
    if not AUDIT_OK:
        return
    try:
        audit_logger.log("AIFlowConfig", action, parameters, result, status)
    except Exception as e:
        print(f"[ai-flow-config] 审计写入失败 (非致命): {e}")


def _afc_ollama_models() -> list:
    """Ollama 已安装模型列表 (供管理端模型下拉)"""
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
    except Exception:
        return []


def _afc_cloud_models() -> list:
    """云端模型目录 (供管理端模型下拉的云模型候选) — H-3

    v4.02.07 H-3 E方案 A: 不再固定 12 项, 而是动态聚合
      base = ai_flow_config.CLOUD_MODEL_CATALOG (registry 真源, 6 厂商 12 模型)
      extra = 每个 vendor 卡片当前 default_model (用户可改填, 如 agnes-2.0-flash)
    去重 + 保序, 让用户改动后 AI 团队下拉框立即可见。
    """
    try:
        base = list(_afc.CLOUD_MODEL_CATALOG or [])
    except Exception:
        base = []
    try:
        from Core import cloud_services_config as _csc
        extra = _csc.all_custom_default_models()
    except Exception:
        extra = []
    seen = set()
    out: list = []
    for m in (list(base) + list(extra)):
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _afc_display_models() -> dict:
    """返回 {vendor: {真名: 显示名}}, 供 AI 团队下拉 / 云卡片模型列表使用友好名。

    v4.02.07 H-3 E方案 B: catalog 用真名 (绑定到 SDK 调用), UI 渲染用显示名
    (解耦, 利于后续添加非注册 catalog 的自定义模型)。
    """
    try:
        from Core import cloud_services_config as _csc
        return _csc.get_all().get("catalog_with_display") or {}
    except Exception:
        return {}


@app.route("/api/admin/ai-flow-config", methods=["GET"])
def api_admin_get_ai_flow_config():
    """获取 AI 流程设置全量数据: 配置 + schema + 元信息 + 权重模板 + 模型列表"""
    bad = _afc_guard()
    if bad:
        return bad
    try:
        templates = _afc.list_templates()
    except Exception as e:
        templates = []
        print(f"[ai-flow-config] 读取权重模板失败 (非致命): {e}")
    try:
        active_weights = _afc.get_active_weights()
    except Exception:
        active_weights = {}
    return jsonify({
        "config": _afc.get_config(),
        "schema": _afc.get_schema(),
        "meta": _afc.get_meta(),
        "templates": templates,
        "active_weights": active_weights,
        "ollama_models": _afc_ollama_models(),
        "cloud_models": _afc_cloud_models(),
        "display_models": _afc_display_models(),  # v4.02.07 H-3 E方案 B: per-vendor 真名→显示名映射
        "default_model": _resolve_local_model_name(),
    })


@app.route("/api/admin/ai-flow-config", methods=["PUT"])
def api_admin_update_ai_flow_config():
    """保存 AI 流程设置 (校验 + 夹取 + 版本记录 + 单向同步 workflow_config)"""
    bad = _afc_guard()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    raw = data.get("config") if isinstance(data.get("config"), dict) else data
    note = str(data.get("note") or "管理端保存")
    try:
        result = _afc.save_config(raw, operator=_afc_operator(), note=note)
    except Exception as e:
        _afc_audit("save_config", {"note": note}, {"error": str(e)}, status="failed")
        return jsonify({"error": f"保存失败: {e}"}), 400
    _afc_audit("save_config",
               {"note": note, "changes": (result.get("changes") or [])[:50]},
               {"version": result.get("version"),
                "warnings": result.get("warnings", []),
                "sync": result.get("sync", {})})
    payload = {"status": "ok", "meta": _afc.get_meta()}
    payload.update(result)
    return jsonify(payload)


@app.route("/api/admin/ai-flow-config/reset", methods=["POST"])
def api_admin_reset_ai_flow_config():
    """恢复默认配置 (仍记录版本, 可回滚)"""
    bad = _afc_guard()
    if bad:
        return bad
    try:
        result = _afc.reset_config(operator=_afc_operator())
    except Exception as e:
        _afc_audit("reset_config", None, {"error": str(e)}, status="failed")
        return jsonify({"error": f"恢复默认失败: {e}"}), 500
    _afc_audit("reset_config", None, {"version": result.get("version")})
    payload = {"status": "ok", "meta": _afc.get_meta()}
    payload.update(result)
    return jsonify(payload)


@app.route("/api/admin/ai-flow-config/versions", methods=["GET"])
def api_admin_ai_flow_versions():
    """版本历史 (最近 10 版, 倒序)"""
    bad = _afc_guard()
    if bad:
        return bad
    try:
        return jsonify({"versions": _afc.list_versions(), "meta": _afc.get_meta(),
                        "max_versions": _afc.MAX_VERSIONS})
    except Exception as e:
        return jsonify({"error": f"读取版本历史失败: {e}"}), 500


@app.route("/api/admin/ai-flow-config/rollback", methods=["POST"])
def api_admin_ai_flow_rollback():
    """回滚到指定版本 (回滚本身记为新版本)"""
    bad = _afc_guard()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    try:
        version = int(data.get("version"))
    except (TypeError, ValueError):
        return jsonify({"error": "version 必须为整数"}), 400
    try:
        result = _afc.rollback(version, operator=_afc_operator())
    except Exception as e:
        _afc_audit("rollback", {"version": version}, {"error": str(e)}, status="failed")
        return jsonify({"error": str(e)}), 400
    _afc_audit("rollback", {"version": version}, {"new_version": result.get("version")})
    payload = {"status": "ok", "meta": _afc.get_meta()}
    payload.update(result)
    return jsonify(payload)


@app.route("/api/admin/ai-flow-config/export", methods=["GET"])
def api_admin_ai_flow_export():
    """导出配置包 (配置 + 元信息 + 权重模板)"""
    bad = _afc_guard()
    if bad:
        return bad
    try:
        bundle = _afc.export_bundle()
    except Exception as e:
        return jsonify({"error": f"导出失败: {e}"}), 500
    _afc_audit("export_bundle", None, {"version": bundle.get("meta", {}).get("version")})
    return jsonify(bundle)


@app.route("/api/admin/ai-flow-config/import", methods=["POST"])
def api_admin_ai_flow_import():
    """导入配置包 (校验后保存, 并补齐缺失的自定义模板)"""
    bad = _afc_guard()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    bundle = data.get("bundle") if isinstance(data.get("bundle"), dict) else data
    try:
        result = _afc.import_bundle(bundle, operator=_afc_operator())
    except Exception as e:
        _afc_audit("import_bundle", None, {"error": str(e)}, status="failed")
        return jsonify({"error": f"导入失败: {e}"}), 400
    _afc_audit("import_bundle", None,
               {"version": result.get("version"),
                "templates_imported": result.get("templates_imported", [])})
    payload = {"status": "ok", "meta": _afc.get_meta()}
    payload.update(result)
    return jsonify(payload)


# ---- v4.02.07: 系统级模型降级链 (网关) 配置 CRUD + 生效模型可观测 ----

def _gw_import():
    """惰性导入 llm_gateway (Core 已在 sys.path)。返回模块或 None。"""
    try:
        import llm_gateway as _gw
        return _gw
    except Exception as e:  # pragma: no cover
        print(f"[app] llm_gateway 模块加载失败: {e}")
        return None


@app.route("/api/admin/gateway-config", methods=["GET"])
def api_admin_get_gateway_config():
    """获取系统级模型降级链配置 (default_fallback_chain / agent_overrides 等)。"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    gw = _gw_import()
    if gw is None:
        return jsonify({"error": "llm_gateway 模块未加载"}), 500
    try:
        return jsonify({"config": gw.load_gateway_config()})
    except Exception as e:
        return jsonify({"error": f"读取网关配置失败: {e}"}), 500


@app.route("/api/admin/gateway-config", methods=["PUT"])
def api_admin_update_gateway_config():
    """保存系统级模型降级链配置 (校验 + 即时生效, 无需重启)。"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    gw = _gw_import()
    if gw is None:
        return jsonify({"error": "llm_gateway 模块未加载"}), 500
    data = request.get_json(silent=True) or {}
    try:
        new_cfg = gw.save_gateway_config(data)
    except Exception as e:
        return jsonify({"error": f"保存失败: {e}"}), 400
    return jsonify({"status": "ok", "config": new_cfg})


@app.route("/api/admin/agent-models/effective", methods=["GET"])
def api_admin_agent_models_effective():
    """返回每个智能体当前生效模型的实时快照 (云/地切换验收可视化依据)。

    与 resolve_llm_for_agent 共用 _resolve_plan 决策逻辑, 回显即真实运行态。
    """
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    gw = _gw_import()
    if gw is None:
        return jsonify({"error": "llm_gateway 模块未加载"}), 500
    # 智能体全集: agent_model_map 键 + 标准智能体补全集
    try:
        cfg = _wc_get() if _wc_get else {}
        amap = (cfg.get("agent_model_map") or {}) if isinstance(cfg, dict) else {}
    except Exception:
        amap = {}
    STANDARD_AGENTS = [
        "RequirementAgent", "ReviewAgent", "ReportAgent", "DevilsAdvocate",
        "RealityChecker", "ReverseReviewer", "SandboxSimulator",
        "ParamSuggestAgent", "OCRRecognizeAgent", "OCRImageRecognizeAgent",
        # v4.02.99 B: 补入标准集, 保证即使 agent_model_map 键缺失也在生效快照中可见可治理
        "DesignAgent", "IdeationAgent",
    ]
    seen = set()
    agents = []
    for a in list(amap.keys()) + STANDARD_AGENTS:
        if a not in seen:
            seen.add(a)
            agents.append(a)
    result = []
    for a in agents:
        try:
            result.append(gw.describe_agent_effective(a))
        except Exception as e:
            result.append({"agent": a, "backend": "unknown",
                           "effective_model": None, "error": str(e)})
    return jsonify({
        "agents": result,
        "gateway_config": gw.load_gateway_config(),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    })


# ---- 6.2 模型监控面板 API (Plan B: 配置预算 + token 累计) ----
def _mm_import():
    """惰性导入模型监控服务 (Core 已在 sys.path)。返回模块或 None。

    v4.02.07 fix: 必须与 llm_gateway 的写路径使用同一个模块实例
    (Core.model_monitor)。此前顶层 `import model_monitor` 与
    `from Core.model_monitor import` 在 Python 中是两个独立对象、各持一份
    _request_log deque, 导致运行时埋点写进 Core.model_monitor 而 /stats 读到
    空的顶层 model_monitor -> 面板恒为 0。统一为 Core.model_monitor 后读写同实例。
    """
    try:
        from Core import model_monitor as _mm
        return _mm
    except Exception as e:  # pragma: no cover
        print(f"[app] model_monitor 模块加载失败: {e}")
        return None


@app.route("/api/admin/model-monitor/stats", methods=["GET"])
def api_admin_model_monitor_stats():
    """实时卡片 + 分组统计 (活跃请求/60s/1h/1h成功率/1h费用 + 按模型/智能体分组)。"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    mm = _mm_import()
    if mm is None:
        return jsonify({"error": "model_monitor 模块未加载"}), 500
    try:
        return jsonify(mm.get_stats())
    except Exception as e:
        return jsonify({"error": f"读取模型监控统计失败: {e}"}), 500


@app.route("/api/admin/model-monitor/trends", methods=["GET"])
def api_admin_model_monitor_trends():
    """趋势图: 按时间桶聚合请求量与平均延迟 (hours 默认 1)。"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    mm = _mm_import()
    if mm is None:
        return jsonify({"error": "model_monitor 模块未加载"}), 500
    try:
        hours = int(request.args.get("hours", 1))
    except Exception:
        hours = 1
    try:
        return jsonify(mm.get_trends(hours=hours))
    except Exception as e:
        return jsonify({"error": f"读取模型监控趋势失败: {e}"}), 500


@app.route("/api/admin/model-monitor/degradation-events", methods=["GET"])
def api_admin_model_monitor_degradation_events():
    """降级事件列表 (云端→本地), 新→旧, limit 默认 100。"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    mm = _mm_import()
    if mm is None:
        return jsonify({"error": "model_monitor 模块未加载"}), 500
    try:
        limit = int(request.args.get("limit", 100))
    except Exception:
        limit = 100
    try:
        return jsonify({"events": mm.get_degradation_events(limit=limit)})
    except Exception as e:
        return jsonify({"error": f"读取降级事件失败: {e}"}), 500


@app.route("/api/admin/model-monitor/quota", methods=["GET"])
def api_admin_model_monitor_quota():
    """配额预警 (Plan B): 配置预算 - 当月云费用累计, 剩余<20% 告警。"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    mm = _mm_import()
    if mm is None:
        return jsonify({"error": "model_monitor 模块未加载"}), 500
    try:
        return jsonify(mm.get_quota())
    except Exception as e:
        return jsonify({"error": f"读取配额失败: {e}"}), 500


@app.route("/api/admin/model-monitor/degradation-summary", methods=["GET"])
def api_admin_model_monitor_degradation_summary():
    """P2 可观测闭环: 降级链聚合摘要 (hours 默认 24)。

    把云→备选云→本地三段链路聚合成计数, 让管理员看到"降级主要发生在哪一层",
    而不必从逐条事件流水里人工汇总。
    """
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    mm = _mm_import()
    if mm is None:
        return jsonify({"error": "model_monitor 模块未加载"}), 500
    try:
        hours = int(request.args.get("hours", 24))
    except Exception:
        hours = 24
    try:
        return jsonify(mm.get_degradation_summary(hours=hours))
    except Exception as e:
        return jsonify({"error": f"读取降级摘要失败: {e}"}), 500


@app.route("/api/admin/cloud-runtime-status", methods=["GET"])
def api_admin_cloud_runtime_status():
    """P2 可观测闭环: P0 多 KEY 池 / P1 自定义厂商 / cloud_alt 的运行时开关状态。

    这些能力由环境变量控制且**默认全关**。若只配了 JSON 里的 api_keys 却没设 env,
    池不会生效且无任何报错 —— 本接口把开关真值回显, 消除这类"静默不生效"排查黑洞。

    返回: pool_enabled / cloud_alt_enabled / cloud_alt_model / cloud_alt_max_attempts
          + vendors[] (每家的 custom 标志 / 启用 / 池规模 / 端点清单) + env_hints
    """
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    try:
        try:
            from Core import cloud_key_pool as ckp
        except Exception:
            import cloud_key_pool as ckp  # type: ignore
        try:
            from Core import cloud_services_config as csc
        except Exception:
            import cloud_services_config as csc  # type: ignore
    except Exception as e:
        return jsonify({"error": f"cloud_key_pool 模块未加载: {e}"}), 500

    pool_on = bool(ckp.key_pool_enabled())
    alt_on = bool(ckp.cloud_alt_enabled())

    vendors = []
    try:
        _all = csc.get_all()
        for name, v in (_all.get("vendors") or {}).items():
            keys = csc.pool_of(name) or []
            active_keys = [k for k in keys if k.get("enabled", True)]
            vendors.append({
                "vendor": name,
                "label": v.get("label", name),
                "custom": bool(v.get("custom")) or str(name).lower().startswith("custom:"),
                "enabled": bool(v.get("enabled", False)),
                "default_model": v.get("default_model", ""),
                "base_url": (v.get("base_url_override") or "").strip(),
                "pool_size": len(active_keys),
                "pool_effective": pool_on and len(active_keys) > 0,
                "endpoints": sorted({k.get("endpoint") for k in active_keys if k.get("endpoint")}),
            })
    except Exception as e:
        return jsonify({"error": f"读取云服务配置失败: {e}"}), 500

    # cloud_alt 当前配置 (来自网关配置, 未加载时给空值而非报错)
    alt_model, alt_attempts = "", 0
    gw = _gw_import()
    if gw is not None:
        try:
            _a, _m, _n = gw._cloud_alt_settings()
            alt_model, alt_attempts = (_m or ""), int(_n or 0)
        except Exception:
            pass

    return jsonify({
        "pool_enabled": pool_on,
        "cloud_alt_enabled": alt_on,
        "cloud_alt_model": alt_model,
        "cloud_alt_max_attempts": alt_attempts,
        "vendors": vendors,
        "env_hints": {
            "CLOUD_KEY_POOL_ENABLED": "1=启用多KEY池轮换(默认0关); 需同时在厂商卡 api_keys 里配 key",
            "CLOUD_ALT_ENABLED": "1=启用云→备选云降级层(默认0关); 需同时在网关配置填 cloud_alt_model",
        },
        "generated_at": datetime.utcnow().isoformat() + "Z",
    })


# ---- 量化评分权重模板 CRUD ----

@app.route("/api/admin/scoring-templates", methods=["GET"])
def api_admin_list_scoring_templates():
    """权重模板列表 + 权重键定义 + 当前生效权重"""
    bad = _afc_guard()
    if bad:
        return bad
    try:
        return jsonify({
            "templates": _afc.list_templates(),
            "weight_keys": _afc.WEIGHT_KEYS,
            "weight_labels": _afc.WEIGHT_LABELS,
            "active_weights": _afc.get_active_weights(),
            "active_template": _afc.get_config().get("review", {}).get("active_weight_template", ""),
        })
    except Exception as e:
        return jsonify({"error": f"读取模板失败: {e}"}), 500


@app.route("/api/admin/scoring-templates", methods=["POST"])
def api_admin_create_scoring_template():
    """新建/另存为权重模板 (总和必须 100%)"""
    bad = _afc_guard()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    weights = data.get("weights") if isinstance(data.get("weights"), dict) else {}
    try:
        result = _afc.create_template(name, weights,
                                      description=str(data.get("description") or ""),
                                      set_default=bool(data.get("set_default")))
    except Exception as e:
        _afc_audit("create_template", {"name": name}, {"error": str(e)}, status="failed")
        return jsonify({"error": str(e)}), 400
    _afc_audit("create_template", {"name": name, "weights": weights}, result)
    return jsonify({"status": "ok", "template": result,
                    "templates": _afc.list_templates()})


@app.route("/api/admin/scoring-templates/<path:name>", methods=["PUT"])
def api_admin_update_scoring_template(name):
    """更新权重模板 (改权重 / 重命名 / 改描述 / 设为默认)"""
    bad = _afc_guard()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    weights = data.get("weights") if isinstance(data.get("weights"), dict) else None
    try:
        result = _afc.update_template(
            name,
            weights=weights,
            new_name=data.get("new_name"),
            description=data.get("description"),
        )
        if data.get("set_default"):
            _afc.set_default_template(result.get("name", name))
            result["is_default"] = True
    except Exception as e:
        _afc_audit("update_template", {"name": name}, {"error": str(e)}, status="failed")
        return jsonify({"error": str(e)}), 400
    _afc_audit("update_template", {"name": name, "weights": weights}, result)
    return jsonify({"status": "ok", "template": result,
                    "templates": _afc.list_templates()})


@app.route("/api/admin/scoring-templates/<path:name>", methods=["DELETE"])
def api_admin_delete_scoring_template(name):
    """删除权重模板 (内置与当前默认禁止删除)"""
    bad = _afc_guard()
    if bad:
        return bad
    try:
        result = _afc.delete_template(name)
    except Exception as e:
        _afc_audit("delete_template", {"name": name}, {"error": str(e)}, status="failed")
        return jsonify({"error": str(e)}), 400
    _afc_audit("delete_template", {"name": name}, result)
    return jsonify({"status": "ok", "template": result,
                    "templates": _afc.list_templates()})


# ───────────────────── 云服务(API)配置 CRUD (v4.02.06 H-3 二期 S1) ─────────────────────
def _csc_guard():
    """云服务配置路由统一鉴权: 通过返回 None, 否则返回响应元组。"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    if not CSC_OK:
        return jsonify({"error": "cloud_services_config 模块未加载"}), 500
    return None


@app.route("/api/admin/cloud-services", methods=["GET"])
def api_admin_list_cloud_services():
    """列出全部厂商云服务配置 (含 catalog 模型目录)"""
    bad = _csc_guard()
    if bad:
        return bad
    try:
        data = _csc.get_all()
        # v4.02.07 修复 2026-08-25: 脱敏 api_key, 仅返回末 4 位 + has_key 标志,
        # 避免明文密钥经管理端 API 泄露 (get_all 内部仍保留明文供运行时解析)。
        for _v in (data.get("vendors") or {}).values():
            _k = (_v.get("api_key") or "")
            _v["api_key_masked"] = ("*" * max(0, len(_k) - 4)) + _k[-4:] if _k else ""
            _v.pop("api_key", None)
            # P2: 多 KEY 池同样脱敏 (P0 引入 api_keys 后, 池内明文会经本接口泄露)。
            #   只回 api_keys_masked + pool_size; UI 未改动池时不回传 api_keys, 保留原值。
            _pool = _v.get("api_keys") or []
            _masked_pool = []
            _pool_size = 0
            for _e in _pool:
                if not isinstance(_e, dict):
                    continue
                _pk = (_e.get("key") or "").strip()
                if not _pk:
                    continue
                _en = bool(_e.get("enabled", True))
                if _en:
                    _pool_size += 1
                _masked_pool.append({
                    "key_masked": ("*" * max(0, len(_pk) - 4)) + _pk[-4:],
                    "endpoint": _e.get("endpoint") or "",
                    "weight": _e.get("weight") or 1,
                    "enabled": _en,
                    "label": _e.get("label") or "",
                })
            _v["api_keys_masked"] = _masked_pool
            _v["pool_size"] = _pool_size
            _v.pop("api_keys", None)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"读取云服务配置失败: {e}"}), 500


@app.route("/api/admin/cloud-services", methods=["PUT"])
def api_admin_upsert_cloud_service():
    """新增或更新一个厂商云服务配置"""
    bad = _csc_guard()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    vendor = (data.get("vendor") or "").strip()
    if not vendor:
        return jsonify({"error": "vendor 不能为空"}), 400
    try:
        result = _csc.upsert(data)
    except Exception as e:
        return jsonify({"error": f"保存失败: {e}"}), 400
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "保存失败")}), 400
    return jsonify({"status": "ok", "vendor": result.get("vendor"),
                    "config": result.get("config"), "has_key": result.get("has_key"),
                    # v4.02.85: 若后端忽略了掩码占位符, 回传提示供 UI 显示 (不再静默)
                    "warnings": result.get("warnings") or []})


@app.route("/api/admin/cloud-services/<vendor>", methods=["DELETE"])
def api_admin_delete_cloud_service(vendor):
    """重置某厂商为默认 (禁用 + 清空密钥)"""
    bad = _csc_guard()
    if bad:
        return bad
    try:
        result = _csc.delete(vendor)
    except Exception as e:
        return jsonify({"error": f"重置失败: {e}"}), 400
    return jsonify({"status": "ok", "vendor": result.get("vendor")})


@app.route("/api/admin/cloud-services/<vendor>/test", methods=["POST"])
def api_admin_test_cloud_service(vendor):
    """连通性自检: 用请求体 (未保存填写) 或已保存配置测试某厂商默认模型"""
    bad = _csc_guard()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    try:
        result = _csc.test_connection(
            vendor,
            api_key=data.get("api_key") or "",
            base_url=data.get("base_url_override") or data.get("base_url") or "",
            model=data.get("default_model") or data.get("model") or "",
            timeout=data.get("timeout") or 0,
            api=data.get("api") or "",                # v4.02.07: 协议类型 (保留兼容)
            api_key_env=data.get("api_key_env") or "",  # v4.02.07: 密钥环境变量名
            endpoint=data.get("endpoint") or "",      # v4.02.07 H-3 二期 B: 实际派发目标 (chat/completions), 优先于 api
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"测试异常: {e}"}), 500
    return jsonify(result)


@app.route("/api/client/register", methods=["POST"])
def api_client_register():
    """客户端注册/心跳 (客户端启动时调用)"""
    data = request.get_json() or {}
    client_id = data.get("client_id", "")
    if not client_id:
        return jsonify({"error": "client_id 不能为空"}), 400

    settings = _load_client_settings()
    if client_id not in settings:
        settings[client_id] = {
            "config": dict(DEFAULT_CLIENT_CONFIG),
            "registered_at": datetime.now().isoformat(),
        }

    settings[client_id].update({
        "hostname": data.get("hostname", ""),
        "tailscale_ip": data.get("tailscale_ip", ""),
        "version": data.get("version", ""),
        "last_seen": datetime.now().isoformat(),
        "online": True,
    })
    _save_client_settings(settings)

    # 返回该客户端的配置 (管理员设定的)
    return jsonify({
        "status": "ok",
        "config": settings[client_id].get("config", DEFAULT_CLIENT_CONFIG),
    })
@app.route("/api/product-lines")
def api_product_lines():
    """获取产品线列表"""
    lines = get_product_lines()
    # 解析 JSON 字段
    for line in lines:
        for k in ("flow_stages", "pricing_model"):
            if line.get(k) and isinstance(line[k], str):
                try:
                    line[k] = json.loads(line[k])
                except:
                    pass
    return jsonify({"product_lines": lines})


@app.route("/api/product-line/<int:pl_id>")
def api_product_line_detail(pl_id):
    """获取产品线详情"""
    line = get_product_line_by_id(pl_id)
    if not line:
        return jsonify({"error": "产品线不存在"}), 404
    for k in ("flow_stages", "pricing_model"):
        if line.get(k) and isinstance(line[k], str):
            try:
                line[k] = json.loads(line[k])
            except:
                pass
    return jsonify({"product_line": line})


@app.route("/api/product-lines", methods=["POST"])
def api_create_product_line():
    """创建产品线 (admin only)"""
    if not g.user or g.user.get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    data = request.get_json() or {}
    import sqlite3 as _sql
    with _sql.connect(Config.DB_PATH) as conn:
        conn.execute(
            """INSERT INTO product_lines (code, name, description, flow_stages, design_template, pricing_model, convergence_intensity)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("code"),
                data.get("name"),
                data.get("description", ""),
                json.dumps(data.get("flow_stages", []), ensure_ascii=False),
                data.get("design_template", "Templates.mot"),
                json.dumps(data.get("pricing_model", {}), ensure_ascii=False),
                data.get("convergence_intensity", "standard"),
            )
        )
        conn.commit()
    return jsonify({"status": "ok"})


# ============================================================
# v2.02.01: 项目管理 API
# ============================================================
@app.route("/api/projects")
def api_projects():
    """获取当前用户项目列表"""
    uid = current_user_id()
    if not uid:
        return jsonify({"projects": [], "total": 0})
    projects = get_projects_by_user(uid)
    for p in projects:
        if p.get("requirements") and isinstance(p["requirements"], str):
            try:
                p["requirements"] = json.loads(p["requirements"])
            except:
                pass
    return jsonify({"projects": projects, "total": len(projects)})


@app.route("/api/projects", methods=["POST"])
def api_create_project():
    """创建项目"""
    if not g.user:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "项目名称不能为空"}), 400
    pl_id = data.get("product_line_id")
    desc = data.get("description", "")
    reqs = data.get("requirements")
    reqs_str = json.dumps(reqs, ensure_ascii=False) if reqs else None

    project_id = create_project(
        user_id=g.user["id"],
        name=name,
        product_line_id=pl_id,
        description=desc,
        requirements=reqs_str,
    )
    return jsonify({"status": "ok", "project_id": project_id})


@app.route("/api/project/<project_id>")
def api_project_detail(project_id):
    """获取项目详情"""
    project = get_project_by_id(project_id)
    if not project:
        return jsonify({"error": "项目不存在"}), 404
    # 权限检查: 非管理员只能看自己的项目
    uid = current_user_id()
    if project["user_id"] != uid and (not g.user or g.user.get("role") != "admin"):
        return jsonify({"error": "无权访问"}), 403
    if project.get("requirements") and isinstance(project["requirements"], str):
        try:
            project["requirements"] = json.loads(project["requirements"])
        except:
            pass
    return jsonify({"project": project})


# ============================================================
# v2.02.01: 积分体系 API
# ============================================================
@app.route("/api/points/balance")
def api_points_balance():
    """获取积分余额"""
    if not g.user:
        return jsonify({"balance": 0})
    return jsonify({"balance": get_balance(g.user["id"])})


@app.route("/api/points/history")
def api_points_history():
    """获取积分变动记录"""
    if not g.user:
        return jsonify({"history": [], "total": 0})
    history = get_points_history(g.user["id"])
    return jsonify({"history": history, "total": len(history)})


@app.route("/api/points/checkin", methods=["POST"])
def api_points_checkin():
    """每日签到"""
    if not g.user:
        return jsonify({"error": "未登录"}), 401
    if not POINTS_OK:
        return jsonify({"error": "积分系统不可用"}), 500
    result = do_daily_checkin(g.user["id"])
    return jsonify(result)


@app.route("/api/points/membership")
def api_points_membership():
    """获取会员等级信息"""
    if not g.user:
        return jsonify({"membership": get_membership_info("trial")})
    level = g.user.get("membership_level", "trial")
    info = get_membership_info(level)
    return jsonify({
        "membership": info,
        "balance": get_balance(g.user["id"]),
        "user_role": g.user.get("role", "user"),
    })


# ============================================================
# v2.05.02: 运营数据飞轮 API
# ============================================================

@app.route("/api/operation/dashboard")
def api_operation_dashboard():
    """运营仪表盘: 效率/成本/质量/进化规则/UI交互统计"""
    try:
        from operation_flywheel import get_operation_dashboard
        data = get_operation_dashboard()
        return jsonify({"status": "ok", "data": data})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/operation/evolution-log")
def api_evolution_log():
    """进化历史日志"""
    try:
        from operation_flywheel import _get_conn
        conn = _get_conn()
        try:
            limit = request.args.get("limit", 50, type=int)
            rows = conn.execute(
                "SELECT id, timestamp, agent, trigger_type, action_taken, rule_data "
                "FROM evolution_log ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            logs = []
            for r in rows:
                entry = {
                    "id": r[0], "timestamp": r[1], "agent": r[2],
                    "trigger_type": r[3], "action_taken": r[4],
                }
                try:
                    entry["rule_data"] = json.loads(r[5]) if r[5] else None
                except (json.JSONDecodeError, TypeError):
                    entry["rule_data"] = r[5]
                logs.append(entry)
            return jsonify({"status": "ok", "logs": logs, "count": len(logs)})
        finally:
            conn.close()
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/operation/ui-feedback", methods=["POST"])
def api_ui_feedback():
    """前端上报UI交互数据 (参数修改/直接通过)"""
    try:
        from operation_flywheel import record_ui_interaction
        data = request.get_json(force=True)
        project_id = data.get("project_id", "")
        param_name = data.get("param_name", "")
        action = data.get("action", "")  # "modify" or "pass"
        value_from = data.get("value_from", "")
        value_to = data.get("value_to", "")

        if not project_id or not param_name or not action:
            return jsonify({"status": "error", "message": "缺少必要参数"}), 400

        result = record_ui_interaction(project_id, param_name, action, value_from, value_to)
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/operation/report-feedback", methods=["POST"])
def api_report_feedback():
    """前端上报报告阅读数据 (章节停留时间/滚动次数)"""
    try:
        from operation_flywheel import record_report_metric
        data = request.get_json(force=True)
        project_id = data.get("project_id", "")
        section_name = data.get("section_name", "")
        view_time_s = data.get("view_time_s", 0)
        scroll_count = data.get("scroll_count", 0)

        if not project_id or not section_name:
            return jsonify({"status": "error", "message": "缺少必要参数"}), 400

        result = record_report_metric(project_id, section_name, view_time_s, scroll_count)
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/operation/trigger-evolution", methods=["POST"])
def api_trigger_evolution():
    """手动触发飞轮进化 (管理员)"""
    try:
        from operation_flywheel import run_all_evolution
        results = run_all_evolution()
        evolved_count = sum(1 for v in results.values()
                            if v.get("evolved") or v.get("alerts"))
        return jsonify({
            "status": "ok",
            "evolved_count": evolved_count,
            "results": results,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
# 文件分发 (管理端->客户端 Motor-CAD/模板/软件)
# ============================================================

import sqlite3 as _sql_db

def _get_db():
    """Get a SQLite connection with Row factory for the current env."""
    conn = _sql_db.connect(Config.DB_PATH)
    conn.row_factory = _sql_db.Row
    return conn

_DISTRIBUTE_DIR = os.path.join(BASE_DIR, "data", "distribute")
os.makedirs(_DISTRIBUTE_DIR, exist_ok=True)


def _init_user_feedback_table():
    """初始化用户反馈表"""
    try:
        conn = _get_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT DEFAULT '',
                category TEXT DEFAULT 'general',
                content TEXT NOT NULL,
                contact TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                reply TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                replied_at TEXT,
                attachments TEXT DEFAULT ''
            );
        """)
        # 自动迁移: 旧表可能没有 attachments 列
        try:
            conn.execute("SELECT attachments FROM user_feedback LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE user_feedback ADD COLUMN attachments TEXT DEFAULT ''")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[WARNING] Init user_feedback table failed: {e}")

_init_user_feedback_table()


@app.route("/api/user/feedback", methods=["POST"])
def api_user_feedback():
    """客户端用户提交反馈/联系管理员 (支持文件附件)"""
    import os, json as _json, time as _time

    # 兼容 JSON 和 multipart 两种请求
    if request.content_type and "multipart" in request.content_type:
        # multipart: 有附件
        data = request.form
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify({"error": "feedback content required"}), 400

        client_id = data.get("client_id", "")
        category = data.get("category", "general")
        contact = data.get("contact", "")

        # 保存附件
        attachment_names = []
        uploaded_files = request.files.getlist("attachments")
        if uploaded_files:
            feedback_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                        "data", "feedback_attachments")
            os.makedirs(feedback_dir, exist_ok=True)

            for f in uploaded_files:
                if not f.filename:
                    continue
                # 安全文件名: timestamp_originalname
                safe_name = f"{int(_time.time()*1000)}_{f.filename}"
                save_path = os.path.join(feedback_dir, safe_name)
                f.save(save_path)
                attachment_names.append(f.filename)
                print(f"[INFO] Feedback attachment saved: {safe_name}")

        attachments_str = _json.dumps(attachment_names, ensure_ascii=False) if attachment_names else ""
    else:
        # JSON: 无附件
        data = request.get_json() or {}
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify({"error": "feedback content required"}), 400
        client_id = data.get("client_id", "")
        category = data.get("category", "general")
        contact = data.get("contact", "")
        attachments_str = ""

    try:
        conn = _get_db()
        cursor = conn.execute(
            """INSERT INTO user_feedback (client_id, category, content, contact, attachments)
               VALUES (?, ?, ?, ?, ?)""",
            (client_id, category, content, contact, attachments_str)
        )
        conn.commit()
        fb_id = cursor.lastrowid
        conn.close()
        att_count = len(attachment_names) if 'attachment_names' in dir() and attachment_names else 0
        print(f"[INFO] User feedback received: id={fb_id}, category={category}, attachments={att_count}")
        return jsonify({"success": True, "feedback_id": fb_id, "attachments": att_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/user/feedback/list", methods=["GET"])
def api_user_feedback_list():
    """获取用户反馈列表 (管理端)"""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM user_feedback ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
        conn.close()
        return jsonify({
            "success": True,
            "feedbacks": [dict(r) for r in rows]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/user/feedback/<int:fb_id>/reply", methods=["POST"])
def api_user_feedback_reply(fb_id):
    """管理端回复用户反馈"""
    data = request.get_json() or {}
    reply = (data.get("reply") or "").strip()
    if not reply:
        return jsonify({"error": "reply content required"}), 400

    try:
        conn = _get_db()
        conn.execute(
            "UPDATE user_feedback SET status='replied', reply=?, replied_at=datetime('now','localtime') WHERE id=?",
            (reply, fb_id)
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/user/feedback/<int:fb_id>/attachments/<path:filename>", methods=["GET"])
def api_user_feedback_attachment(fb_id, filename):
    """下载用户反馈附件文件 (管理端)"""
    import os, json as _json
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT attachments FROM user_feedback WHERE id=?", (fb_id,)
        ).fetchone()
        conn.close()

        if not row:
            return jsonify({"error": "feedback not found"}), 404

        attachments_str = row["attachments"] or ""
        if not attachments_str:
            return jsonify({"error": "no attachments"}), 404

        # 解析附件名列表
        att_names = _json.loads(attachments_str) if isinstance(attachments_str, str) else attachments_str
        if not isinstance(att_names, list):
            att_names = [att_names]

        # 查找匹配的附件文件 (存储格式: timestamp_originalname)
        feedback_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                    "data", "feedback_attachments")
        if not os.path.isdir(feedback_dir):
            return jsonify({"error": "attachments dir not found"}), 404

        # filename 参数可以是原始文件名, 也可以是存储文件名
        target_file = None
        for stored_file in os.listdir(feedback_dir):
            # 检查是否匹配 (stored_file 格式: timestamp_originalname)
            for orig_name in att_names:
                if stored_file.endswith(orig_name) or stored_file == filename:
                    target_file = os.path.join(feedback_dir, stored_file)
                    break
            if target_file:
                break

        if not target_file or not os.path.isfile(target_file):
            return jsonify({"error": f"attachment file not found: {filename}"}), 404

        # 确定下载时的文件名 (用原始名)
        download_name = filename
        for orig_name in att_names:
            if target_file.endswith(orig_name):
                download_name = orig_name
                break

        from flask import send_file
        return send_file(target_file, as_attachment=True, download_name=download_name)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _init_distribute_table():
    """初始化文件分发表"""
    try:
        conn = _get_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS distribute_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                original_name TEXT NOT NULL,
                file_type TEXT DEFAULT 'software',
                file_size INTEGER DEFAULT 0,
                sha256 TEXT DEFAULT '',
                description TEXT DEFAULT '',
                upload_time TEXT DEFAULT (datetime('now','localtime')),
                download_count INTEGER DEFAULT 0,
                target_client TEXT DEFAULT ''
            );
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[WARNING] Init distribute table failed: {e}")

_init_distribute_table()


@app.route("/api/distribute/files", methods=["GET"])
def api_distribute_list():
    """获取可分发的文件列表"""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM distribute_files ORDER BY upload_time DESC"
        ).fetchall()
        conn.close()
        files = []
        for r in rows:
            files.append({
                "id": r["id"],
                "filename": r["filename"],
                "original_name": r["original_name"],
                "file_type": r["file_type"],
                "file_size": r["file_size"],
                "description": r["description"],
                "upload_time": r["upload_time"],
                "download_count": r["download_count"],
                "target_client": r["target_client"],
            })
        return jsonify({"success": True, "files": files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/distribute/files", methods=["POST"])
def api_distribute_upload():
    """上传文件到分发目录 (管理端使用)"""
    from werkzeug.utils import secure_filename

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    original_name = secure_filename(file.filename)
    file_type = request.form.get("file_type", "software")
    description = request.form.get("description", "")
    target_client = request.form.get("target_client", "")

    # 生成安全文件名
    import uuid as _uuid
    stored_name = f"{_uuid.uuid4().hex[:8]}_{original_name}"
    save_path = os.path.join(_DISTRIBUTE_DIR, stored_name)

    file.save(save_path)
    file_size = os.path.getsize(save_path)

    # 计算 SHA256
    import hashlib
    sha256 = ""
    try:
        h = hashlib.sha256()
        with open(save_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        sha256 = h.hexdigest()
    except Exception:
        pass

    try:
        conn = _get_db()
        cursor = conn.execute(
            """INSERT INTO distribute_files
               (filename, original_name, file_type, file_size, sha256, description, target_client)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (stored_name, original_name, file_type, file_size, sha256, description, target_client)
        )
        conn.commit()
        file_id = cursor.lastrowid
        conn.close()
        print(f"[INFO] Distribute file uploaded: {original_name} ({file_size} bytes)")
        return jsonify({
            "success": True,
            "file_id": file_id,
            "filename": original_name,
            "size": file_size,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/distribute/files/<int:file_id>", methods=["GET"])
def api_distribute_download(file_id):
    """下载分发文件 (客户端使用)"""
    from flask import send_from_directory

    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT * FROM distribute_files WHERE id=?", (file_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "File not found"}), 404

        # 增加下载计数
        conn.execute(
            "UPDATE distribute_files SET download_count = download_count + 1 WHERE id=?",
            (file_id,)
        )
        conn.commit()
        conn.close()

        return send_from_directory(
            _DISTRIBUTE_DIR, row["filename"],
            as_attachment=True, download_name=row["original_name"]
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/distribute/files/<int:file_id>", methods=["DELETE"])
def api_distribute_delete(file_id):
    """删除分发文件 (管理端使用)"""
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT * FROM distribute_files WHERE id=?", (file_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "File not found"}), 404

        # 删除物理文件
        file_path = os.path.join(_DISTRIBUTE_DIR, row["filename"])
        if os.path.exists(file_path):
            os.remove(file_path)

        conn.execute("DELETE FROM distribute_files WHERE id=?", (file_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# 双向文件传输系统 (独立于版本更新和单向分发)
# 支持主机端↔客户端双向发送/接收任意文件
# ============================================================

_TRANSFER_DIR = os.path.join(BASE_DIR, "data", "file_transfers")
os.makedirs(_TRANSFER_DIR, exist_ok=True)


def _init_transfer_table():
    """初始化双向文件传输表"""
    try:
        conn = _get_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS file_transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT UNIQUE NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                file_type TEXT DEFAULT '',
                sha256 TEXT DEFAULT '',
                sender_id TEXT NOT NULL,
                sender_name TEXT DEFAULT '',
                recipient_id TEXT NOT NULL DEFAULT 'all',
                recipient_name TEXT DEFAULT '',
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                delivered_at TEXT DEFAULT '',
                acknowledged_at TEXT DEFAULT '',
                download_count INTEGER DEFAULT 0
            );
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[WARNING] Init file_transfers table failed: {e}")

_init_transfer_table()


def _compute_sha256(file_path):
    """计算文件 SHA256"""
    import hashlib
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@app.route("/api/transfer/upload", methods=["POST"])
def api_transfer_upload():
    """上传文件 (管理端/客户端均可调用)

    Multipart form data:
        file: 文件
        sender_id: 发送方 ID (client_id 或 "manager")
        sender_name: 发送方名称 (hostname)
        recipient_id: 接收方 ID (client_id, "all", 或 "manager")
        recipient_name: 接收方名称
        description: 描述 (可选)
        file_type: 文件类型 (可选, 如 document/model/report/other)

    Returns:
        { success, file_id, original_name, size, sha256 }
    """
    from werkzeug.utils import secure_filename
    import uuid as _uuid

    if "file" not in request.files:
        return jsonify({"error": "未提供文件"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "文件名为空"}), 400

    sender_id = request.form.get("sender_id", "unknown")
    sender_name = request.form.get("sender_name", "")
    recipient_id = request.form.get("recipient_id", "all")
    recipient_name = request.form.get("recipient_name", "")
    description = request.form.get("description", "")
    file_type = request.form.get("file_type", "other")

    original_name = secure_filename(file.filename)
    file_id = _uuid.uuid4().hex[:12]
    stored_name = f"{file_id}_{original_name}"
    save_path = os.path.join(_TRANSFER_DIR, stored_name)

    # 流式保存 (支持大文件)
    file.save(save_path)
    file_size = os.path.getsize(save_path)

    # SHA256 校验
    sha256 = ""
    try:
        sha256 = _compute_sha256(save_path)
    except Exception:
        pass

    try:
        conn = _get_db()
        conn.execute(
            """INSERT INTO file_transfers
               (file_id, original_name, stored_name, file_size, file_type, sha256,
                sender_id, sender_name, recipient_id, recipient_name, description, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (file_id, original_name, stored_name, file_size, file_type, sha256,
             sender_id, sender_name, recipient_id, recipient_name, description)
        )
        conn.commit()
        conn.close()
        print(f"[INFO] File transfer uploaded: {original_name} ({file_size} bytes) "
              f"from {sender_id} to {recipient_id}")
        return jsonify({
            "success": True,
            "file_id": file_id,
            "original_name": original_name,
            "size": file_size,
            "sha256": sha256,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/transfer/files", methods=["GET"])
def api_transfer_list():
    """查询文件传输列表

    Query params:
        sender_id: 按发送方过滤
        recipient_id: 按接收方过滤 (会同时匹配 "all" 的文件)
        role: "manager" 时返回所有文件; "client" 时需提供 client_id
        client_id: 客户端 ID (role=client 时必填)

    Returns:
        { success, files: [...] }
    """
    sender_id = request.args.get("sender_id", "")
    recipient_id = request.args.get("recipient_id", "")
    role = request.args.get("role", "")
    client_id = request.args.get("client_id", "")

    try:
        conn = _get_db()
        query = "SELECT * FROM file_transfers WHERE 1=1"
        params = []

        if role == "manager":
            # 管理端: 可以看所有, 或按 recipient_id="manager" 过滤收件箱
            if recipient_id == "manager":
                query += " AND recipient_id = ?"
                params.append("manager")
            elif sender_id:
                query += " AND sender_id = ?"
                params.append(sender_id)
        else:
            # 客户端:
            #   sender_id 有值 → 查看自己发出的文件 (发件箱)
            #   仅 client_id  → 查看发给自己的文件 (收件箱, 含广播 "all")
            if sender_id:
                query += " AND sender_id = ?"
                params.append(sender_id)
            elif client_id:
                query += " AND (recipient_id = ? OR recipient_id = 'all')"
                params.append(client_id)

        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
        conn.close()

        files = []
        for r in rows:
            files.append({
                "file_id": r["file_id"],
                "original_name": r["original_name"],
                "file_size": r["file_size"],
                "file_type": r["file_type"],
                "sender_id": r["sender_id"],
                "sender_name": r["sender_name"],
                "recipient_id": r["recipient_id"],
                "recipient_name": r["recipient_name"],
                "description": r["description"],
                "status": r["status"],
                "created_at": r["created_at"],
                "delivered_at": r["delivered_at"],
                "acknowledged_at": r["acknowledged_at"],
                "download_count": r["download_count"],
                "sha256": r["sha256"][:16] + "..." if r["sha256"] else "",
            })
        return jsonify({"success": True, "files": files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/transfer/files/<file_id>", methods=["GET"])
def api_transfer_file_info(file_id):
    """获取单个文件元数据"""
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT * FROM file_transfers WHERE file_id=?", (file_id,)
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "文件不存在"}), 404
        return jsonify({
            "success": True,
            "file_id": row["file_id"],
            "original_name": row["original_name"],
            "file_size": row["file_size"],
            "file_type": row["file_type"],
            "sha256": row["sha256"],
            "sender_id": row["sender_id"],
            "sender_name": row["sender_name"],
            "recipient_id": row["recipient_id"],
            "recipient_name": row["recipient_name"],
            "description": row["description"],
            "status": row["status"],
            "created_at": row["created_at"],
            "download_count": row["download_count"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/transfer/download/<file_id>", methods=["GET"])
def api_transfer_download(file_id):
    """下载文件 (流式, 支持大文件)"""
    from flask import send_from_directory

    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT * FROM file_transfers WHERE file_id=?", (file_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "文件不存在"}), 404

        # 更新下载计数和状态
        conn.execute(
            """UPDATE file_transfers
               SET download_count = download_count + 1,
                   status = CASE WHEN status = 'pending' THEN 'delivered' ELSE status END,
                   delivered_at = CASE WHEN delivered_at = '' THEN datetime('now','localtime') ELSE delivered_at END
               WHERE file_id=?""",
            (file_id,)
        )
        conn.commit()
        conn.close()

        return send_from_directory(
            _TRANSFER_DIR, row["stored_name"],
            as_attachment=True, download_name=row["original_name"]
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/transfer/files/<file_id>", methods=["DELETE"])
def api_transfer_delete(file_id):
    """删除文件 (发送方或管理端可删除)"""
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT * FROM file_transfers WHERE file_id=?", (file_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "文件不存在"}), 404

        # 删除物理文件
        file_path = os.path.join(_TRANSFER_DIR, row["stored_name"])
        if os.path.exists(file_path):
            os.remove(file_path)

        conn.execute("DELETE FROM file_transfers WHERE file_id=?", (file_id,))
        conn.commit()
        conn.close()
        print(f"[INFO] File transfer deleted: {row['original_name']} (file_id={file_id})")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/transfer/ack/<file_id>", methods=["POST"])
def api_transfer_ack(file_id):
    """确认接收文件"""
    data = request.get_json(silent=True) or {}
    recipient_id = data.get("recipient_id", "")

    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT * FROM file_transfers WHERE file_id=?", (file_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "文件不存在"}), 404

        conn.execute(
            """UPDATE file_transfers
               SET status = 'acknowledged',
                   acknowledged_at = datetime('now','localtime')
               WHERE file_id=?""",
            (file_id,)
        )
        conn.commit()
        conn.close()
        print(f"[INFO] File transfer acknowledged: {row['original_name']} by {recipient_id}")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# v3.00.06: 客户端自动更新 API
# ============================================================

_UPDATE_REPO_DIR = os.path.join(BASE_DIR, "update_repo")
os.makedirs(_UPDATE_REPO_DIR, exist_ok=True)


@app.route("/api/version/latest", methods=["GET"])
def api_version_latest():
    """返回最新客户端版本信息

    响应格式 (扁平):
        {
            "version": "3.00.05",
            "type": "client",
            "download_url": "/packages/MotorAI_Client_update_v3.00.05.zip",
            "size": 50720603,
            "sha256": "",
            "changelog": "..."
        }
    """
    try:
        info_path = os.path.join(_UPDATE_REPO_DIR, "version_info.json")
        if not os.path.exists(info_path):
            return jsonify({"error": "version_info.json not found"}), 404

        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)

        # 计算实际文件的 SHA256 (如果 zip 存在)
        zip_name = os.path.basename(info.get("download_url", ""))
        zip_path = os.path.join(_UPDATE_REPO_DIR, zip_name)
        if os.path.exists(zip_path):
            actual_size = os.path.getsize(zip_path)
            info["size"] = actual_size  # 用实际文件大小覆盖
            if not info.get("sha256"):
                import hashlib
                h = hashlib.sha256()
                with open(zip_path, "rb") as zf:
                    while True:
                        chunk = zf.read(65536)
                        if not chunk:
                            break
                        h.update(chunk)
                info["sha256"] = h.hexdigest()
        else:
            print(f"[WARN] Update zip not found: {zip_path}")

        return jsonify(info)
    except Exception as e:
        print(f"[ERROR] api_version_latest: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/rmxprt_image/<path:filename>", methods=["GET"])
def serve_rmxprt_image(filename):
    """v4.02.69: 下发 RMxprt 2D 几何截图 (TOP10 卡片「4. top10 方案图示」)。

    背景: worker 抓屏落盘于 后端主机 Scripts/rmxprt_images/, orchestrator 随
    generate_designs SSE 下发 geometry_images (含 paths 绝对路径 / files / urls)。
    客户端与后端同机时直接读盘; 不同机 (远程 Tailscale/LAN 连 5000 口) 时绝对
    路径不可达, 客户端改按 urls 经本路由拉取, 缓存到本地后渲染。

    安全: 仅允许 [A-Za-z0-9._-] 组成的至多两段路径 (v4.02.76 起支持 run_*/ 子目录),
          显式拒绝 "." / "..", 杜绝路径遍历。
    v4.02.76: 图片改由 worker 落盘到 run_<stamp>_<pid>/ 子目录 (改进方案 v1 方案 C),
          故本路由放开一层子目录; 同时对只带 basename 的历史/兜底请求做子目录内查找。
    """
    from flask import send_from_directory

    _img_dir = os.path.join(BASE_DIR, "Scripts", "rmxprt_images")
    _allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    _rel = (filename or "").replace("\\", "/").strip("/")
    _parts = [p for p in _rel.split("/") if p]
    if not _parts or len(_parts) > 2:
        return jsonify({"error": "invalid filename"}), 400
    for _p in _parts:
        if _p in (".", "..") or any(_ch not in _allowed for _ch in _p):
            return jsonify({"error": "invalid filename"}), 400

    _safe = "/".join(_parts)
    if not os.path.exists(os.path.join(_img_dir, *_parts)):
        # 兜底: 请求只带 basename (旧 payload / files 键) 时, 在 run_*/ 子目录里按名查找
        _base = _parts[-1]
        _hit = ""
        try:
            for _name in sorted(os.listdir(_img_dir)):
                if os.path.isfile(os.path.join(_img_dir, _name, _base)):
                    _hit = f"{_name}/{_base}"
                    break
        except Exception:
            _hit = ""
        if not _hit:
            return jsonify({"error": f"image not found: {_safe}"}), 404
        _safe = _hit
    return send_from_directory(_img_dir, _safe, as_attachment=False)


@app.route("/packages/<path:filename>", methods=["GET"])
def serve_update_package(filename):
    """提供更新包下载

    从 update_repo 目录发送 zip 文件。
    设置正确的 Content-Length 和 Content-Type。
    """
    from flask import send_from_directory

    # 安全: 防止路径遍历
    filename = os.path.basename(filename)
    file_path = os.path.join(_UPDATE_REPO_DIR, filename)

    if not os.path.exists(file_path):
        print(f"[WARN] Package not found: {file_path}")
        return jsonify({"error": f"Package not found: {filename}"}), 404

    print(f"[INFO] Serving update package: {filename} ({os.path.getsize(file_path)} bytes)")
    return send_from_directory(
        _UPDATE_REPO_DIR, filename,
        as_attachment=True,
        download_name=filename,
        mimetype="application/zip",
    )


# ============================================================
# v2.05.03: 客户端异常自动上报
# ============================================================

def _init_client_errors_table():
    """初始化客户端错误报告表"""
    try:
        conn = _get_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS client_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT DEFAULT '',
                client_version TEXT DEFAULT '',
                username TEXT DEFAULT '',
                error_type TEXT DEFAULT 'unhandled_exception',
                error_message TEXT NOT NULL,
                traceback TEXT DEFAULT '',
                os_info TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                status TEXT DEFAULT 'new'
            );
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[WARNING] Init client_errors table failed: {e}")

_init_client_errors_table()


@app.route("/api/client/error-report", methods=["POST"])
def api_client_error_report():
    """接收客户端异常/故障自动上报"""
    data = request.get_json() or {}
    error_message = (data.get("error_message") or "").strip()
    if not error_message:
        return jsonify({"error": "error_message required"}), 400

    try:
        conn = _get_db()
        cursor = conn.execute(
            """INSERT INTO client_errors
               (client_id, client_version, username, error_type, error_message, traceback, os_info)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (data.get("client_id", ""),
             data.get("client_version", ""),
             data.get("username", ""),
             data.get("error_type", "unhandled_exception"),
             error_message,
             data.get("traceback", ""),
             data.get("os_info", ""))
        )
        conn.commit()
        err_id = cursor.lastrowid
        conn.close()
        print(f"[ERROR-REPORT] Client error received: id={err_id}, client={data.get('client_id','')}, "
              f"type={data.get('error_type','')}, msg={error_message[:100]}")
        return jsonify({"success": True, "error_id": err_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/client/errors", methods=["GET"])
def api_client_errors_list():
    """获取客户端错误报告列表 (管理端)"""
    try:
        conn = _get_db()
        # 支持状态筛选
        status = request.args.get("status", "")
        limit = request.args.get("limit", 200, type=int)

        if status:
            rows = conn.execute(
                "SELECT * FROM client_errors WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM client_errors ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        conn.close()
        return jsonify({
            "success": True,
            "errors": [dict(r) for r in rows]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/client/errors/<int:err_id>", methods=["PUT"])
def api_client_error_update(err_id):
    """更新错误报告状态 (管理端: 标记已处理/忽略)"""
    data = request.get_json() or {}
    new_status = data.get("status", "")
    if new_status not in ("new", "processing", "resolved", "ignored"):
        return jsonify({"error": "invalid status"}), 400

    try:
        conn = _get_db()
        conn.execute(
            "UPDATE client_errors SET status=? WHERE id=?",
            (new_status, err_id)
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/client/errors/stats", methods=["GET"])
def api_client_errors_stats():
    """获取错误统计 (管理端仪表盘)"""
    try:
        conn = _get_db()
        total = conn.execute("SELECT COUNT(*) FROM client_errors").fetchone()[0]
        new_count = conn.execute("SELECT COUNT(*) FROM client_errors WHERE status='new'").fetchone()[0]
        resolved = conn.execute("SELECT COUNT(*) FROM client_errors WHERE status='resolved'").fetchone()[0]
        # 按客户端统计
        by_client = conn.execute(
            "SELECT client_id, COUNT(*) as cnt FROM client_errors GROUP BY client_id ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        # 最近24小时
        recent = conn.execute(
            "SELECT COUNT(*) FROM client_errors WHERE created_at > datetime('now','localtime','-1 day')"
        ).fetchone()[0]
        conn.close()
        return jsonify({
            "success": True,
            "total": total,
            "new": new_count,
            "resolved": resolved,
            "recent_24h": recent,
            "by_client": [dict(r) for r in by_client]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ============================================================
# 案例电机数据中心 API
# ============================================================
try:
    sys.path.insert(0, CORE_DIR)
    from case_motor_db import (
        init_case_motor_tables, create_case_motor, update_case_motor, get_case_motor,
        list_case_motors, delete_case_motor, get_statistics as cm_get_stats,
        upsert_param_bloodline, get_param_bloodline,
        save_verification, get_verification_history,
        save_draft, load_draft, list_drafts,
        calculate_completeness, MOTOR_PARAM_FIELDS, SERIES_TEMPLATES,
        find_case_motor_by_fingerprint, derive_motor_type, _fp_equal,
        list_user_templates, get_user_template, create_user_template,
        update_user_template, delete_user_template,
        find_similar_case_motors, compare_case_motor_batch,
    )
    sys.path.insert(0, AGENTS_DIR)
    from case_motor_agent import CaseVerifyAgent, ParamSuggestAgent, OCRRecognizeAgent, ParamCompareAgent
    _cm_verify_agent = CaseVerifyAgent()
    _cm_suggest_agent = ParamSuggestAgent()
    _cm_ocr_agent = OCRRecognizeAgent()
    _cm_compare_agent = ParamCompareAgent()
    CM_DB_OK = True
    print("[app] 案例电机模块加载成功")
    # v4.01.007: 后台预热 Ollama 模型 (T600 上模型加载需要 ~85s)
    try:
        _cm_ocr_agent._prewarm_model()
        print("[app] Ollama 模型后台预热已启动")
    except Exception as _e:
        print(f"[app] Ollama 模型预热启动失败 (非致命): {_e}")
except Exception as e:
    CM_DB_OK = False
    print(f"[app] 案例电机模块加载失败: {e}")


@app.route("/api/case-motor/init", methods=["POST"])
def cm_init_tables():
    """初始化案例电机数据库表"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        init_case_motor_tables()
        return jsonify({"success": True, "message": "案例电机表初始化完成"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/list", methods=["GET"])
def cm_list():
    """查询案例电机列表"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        status = request.args.get("status")
        series_id = request.args.get("series_id")
        motor_type = request.args.get("motor_type")
        motor_model = request.args.get("motor_model")
        keyword = request.args.get("keyword")
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))
        motors = list_case_motors(status=status, series_id=series_id,
                                  motor_type=motor_type, motor_model=motor_model, keyword=keyword,
                                  limit=limit, offset=offset)
        stats = cm_get_stats()
        return jsonify({"success": True, "motors": motors, "stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/<int:case_id>", methods=["GET"])
def cm_detail(case_id):
    """获取案例电机详情 (含验算结果和参数血缘)"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        case = get_case_motor(case_id)
        if not case:
            return jsonify({"error": "案例不存在"}), 404
        # v4.05: 附带参数血缘记录
        bloodline = []
        try:
            bloodline = get_param_bloodline(case_id)
        except Exception:
            pass
        return jsonify({"success": True, "case": case, "param_bloodline": bloodline})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/create", methods=["POST"])
def cm_create():
    """创建案例电机"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        data = request.get_json(force=True)
        data["completeness_score"] = calculate_completeness(data)
        if g.get("user"):
            data["user_id"] = g.user.get("id")
        result = create_case_motor(data)
        return jsonify({"success": True, "case": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/<int:case_id>", methods=["PUT"])
def cm_update(case_id):
    """更新案例电机"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        data = request.get_json(force=True)
        data["completeness_score"] = calculate_completeness(data)
        update_case_motor(case_id, data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/<int:case_id>", methods=["DELETE"])
def cm_delete(case_id):
    """删除案例电机"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        ok = delete_case_motor(case_id)
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/<int:case_id>/verify", methods=["POST"])
def cm_verify(case_id):
    """提交验算"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        case = get_case_motor(case_id)
        if not case:
            return jsonify({"error": "案例不存在"}), 404

        # 提取参数用于验算
        params = {k: v for k, v in case.items() if v is not None and k in {f[0] for f in MOTOR_PARAM_FIELDS}}
        result = _cm_verify_agent.safe_run(params=params)
        verify_id = save_verification(
            case_id=case_id,
            verify_type=result.get("verify_type", "full"),
            result=result["result"],
            details=result["details"],
            summary=result["summary"],
            warning_items=result.get("warning_items", ""),
        )
        return jsonify({"success": True, "verify_id": verify_id, "result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/<int:case_id>/verify-history", methods=["GET"])
def cm_verify_history(case_id):
    """获取验算历史"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        history = get_verification_history(case_id)
        return jsonify({"success": True, "history": history})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/suggest", methods=["POST"])
def cm_suggest():
    """AI参数补全"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        data = request.get_json(force=True)
        params = data.get("params", {})
        series_id = data.get("series_id", "")
        # v4.00.012: 线程超时保护 (120s), 避免 LLM 长时间阻塞
        import concurrent.futures
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_cm_suggest_agent.safe_run, params=params, series_id=series_id)
        try:
            result = future.result(timeout=120)
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False)
            return jsonify({"error": "AI补全超时, 请稍后重试或先手动填写关键参数"}), 504
        finally:
            executor.shutdown(wait=False)
        return jsonify({"success": True, "suggestions": result.get("suggestions", [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/series-templates", methods=["GET"])
def cm_series_templates():
    """获取电机系列模板列表"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    return jsonify({"success": True, "templates": SERIES_TEMPLATES})


# ==================== v4.02: 用户模版 (系列 × 电机类型 二维) ====================

@app.route("/api/case-motor/user-templates", methods=["GET"])
def cm_user_templates_list():
    """列出用户自定义模版 (可按系列/类型过滤)"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    series = request.args.get("series")
    motor_type = request.args.get("motor_type")
    try:
        rows = list_user_templates(series=series, motor_type=motor_type)
        return jsonify({"success": True, "templates": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/user-templates", methods=["POST"])
def cm_user_templates_create():
    """创建用户自定义模版"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        data = request.get_json(force=True)
        if not data.get("name"):
            return jsonify({"error": "模版名称不能为空"}), 400
        tpl = create_user_template(data)
        return jsonify({"success": True, "template": tpl})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/user-templates/<int:tpl_id>", methods=["GET"])
def cm_user_templates_get(tpl_id):
    """获取单个用户模版"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    tpl = get_user_template(tpl_id)
    if not tpl:
        return jsonify({"error": "模版不存在"}), 404
    return jsonify({"success": True, "template": tpl})


@app.route("/api/case-motor/user-templates/<int:tpl_id>", methods=["PUT"])
def cm_user_templates_update(tpl_id):
    """更新用户模版"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        data = request.get_json(force=True)
        tpl = update_user_template(tpl_id, data)
        if not tpl:
            return jsonify({"error": "模版不存在"}), 404
        return jsonify({"success": True, "template": tpl})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/user-templates/<int:tpl_id>", methods=["DELETE"])
def cm_user_templates_delete(tpl_id):
    """删除用户模版"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    ok = delete_user_template(tpl_id)
    if not ok:
        return jsonify({"error": "模版不存在"}), 404
    return jsonify({"success": True})


@app.route("/api/case-motor/param-fields", methods=["GET"])
def cm_param_fields():
    """获取参数字段定义"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    return jsonify({"success": True, "fields": MOTOR_PARAM_FIELDS})


# ==================== v4.05: OCR 扫描 + 文本解析 ====================

import re as _re

# 电机铭牌参数正则提取规则
_OCR_PATTERNS = {
    # ═══ 1. 通用参数 (General) ═══
    "motor_type": [
        (r"电机型号[：:\s]*([A-Za-z0-9\-/]+)", ""),
        (r"型号[：:\s]*([A-Za-z0-9\-/]+)", ""),
        (r"^([A-Z]+\d*[-]?\d+[A-Z]?[-]?\d*)\s*电机", ""),
        (r"^([A-Z]+\d*-\d+[A-Z]?-\d+)", ""),
    ],
    "rated_power": [
        (r"额定功率[：:\s]*(\d+\.?\d*)", "kW"),
        (r"功率[：:\s]*(\d+\.?\d*)\s*kW", "kW"),
        (r"(\d+\.?\d*)\s*kW", "kW"),
    ],
    "rated_voltage": [
        (r"额定电压[：:\s]*(\d+)", "V"),
        (r"电压[：:\s]*(\d+)", "V"),
        (r"(\d{3,4})\s*V\b", "V"),
    ],
    "rated_current": [
        (r"额定电流[：:\s]*(\d+\.?\d*)", "A"),
        (r"电流[：:\s]*(\d+\.?\d*)\s*A", "A"),
    ],
    "rated_speed": [
        (r"额定转速[：:\s]*(\d+)", "r/min"),
        (r"转速[：:\s]*(\d+)", "r/min"),
        (r"(\d{2,5})\s*rpm", "r/min"),
        (r"(\d{2,5})\s*r/?min", "r/min"),
    ],
    "rated_freq": [
        (r"额定频率[：:\s]*(\d+)", "Hz"),
        (r"频率[：:\s]*(\d+)", "Hz"),
        (r"(\d{2})\s*Hz", "Hz"),
    ],
    "poles": [
        (r"极数[：:\s]*(\d+)", ""),
        (r"(\d+)P\b", ""),
        (r"(\d+)极", ""),
    ],
    "phase": [
        (r"相数[：:\s]*(\d+)", ""),
        (r"(\d)相", ""),
        (r"三相", "3"),
    ],
    "operating_temp": [
        (r"工作温度[：:\s]*(\d+\.?\d*)", "℃"),
        (r"运行温度[：:\s]*(\d+\.?\d*)", "℃"),
        (r"环境温度[：:\s]*(\d+\.?\d*)", "℃"),
    ],
    "load_type": [
        (r"负载类型[：:\s]*([^\n\r]+)", ""),
        (r"负载方式[：:\s]*([^\n\r]+)", ""),
    ],
    "insulation_class": [
        (r"绝缘等级[：:\s]*([A-F])", ""),
        (r"绝缘[等级：:\s]*([A-F])", ""),
        (r"[Ii]nsulation[：:\s]*([A-F])", ""),
    ],
    "protection_class": [
        (r"防护[等级：\s]*IP\s*(\d+)", "IP"),
        (r"IP\s*(\d+)", "IP"),
    ],
    "cooling_method": [
        (r"冷却[方式：\s]*IC\s*(\d+)", "IC"),
        (r"IC\s*(\d+)", "IC"),
    ],
    # ═══ 2. 定子参数 (Stator) ═══
    "stator_od": [
        (r"定子外径[：:\s]*(\d+\.?\d*)", "mm"),
        (r"定子外径[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
    ],
    "stator_id": [
        (r"定子内径[：:\s]*(\d+\.?\d*)", "mm"),
        (r"定子内径[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
    ],
    "core_length": [
        (r"定子铁心长度[：:\s]*(\d+\.?\d*)", "mm"),
        (r"定子铁芯长度[：:\s]*(\d+\.?\d*)", "mm"),
        (r"铁心长度[：:\s]*(\d+\.?\d*)", "mm"),
        (r"铁芯长度[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "air_gap": [
        (r"气隙[长度：:\s]*(\d+\.?\d*)", "mm"),
        (r"气隙[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
    ],
    "slots_stator": [
        (r"定子槽数[：:\s]*(\d+)", ""),
        (r"定子槽[数：\s]*(\d+)", ""),
    ],
    "lamination_sectors": [
        (r"叠片扇形数[：:\s]*(\d+)", ""),
        (r"扇形数[：:\s]*(\d+)", ""),
        (r"分段数[：:\s]*(\d+)", ""),
    ],
    "stator_skew": [
        (r"定子斜槽[：:\s]*(\d+\.?\d*)", "slot"),
        (r"斜槽[：:\s]*(\d+\.?\d*)", "slot"),
    ],
    "tooth_width": [
        (r"齿宽[：:\s]*(\d+\.?\d*)", "mm"),
        (r"定子齿宽[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "yoke_thickness": [
        (r"轭厚[：:\s]*(\d+\.?\d*)", "mm"),
        (r"轭部厚度[：:\s]*(\d+\.?\d*)", "mm"),
        (r"定子轭厚[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    # ═══ 3. 定子槽形参数 (Stator Slot) ═══
    "stator_slot_type": [
        (r"定子槽形类型[：:\s]*([^\n\r]+)", ""),
        (r"定子槽形[：:\s]*([^\n\r]+)", ""),
    ],
    "stator_hs0": [
        (r"定子.*?Hs0[：:\s]*(\d+\.?\d*)", "mm"),
        (r"Hs0[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
    ],
    "stator_hs1": [
        (r"定子.*?Hs1[：:\s]*(\d+\.?\d*)", "mm"),
        (r"Hs1[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
    ],
    "stator_hs2": [
        (r"定子.*?Hs2[：:\s]*(\d+\.?\d*)", "mm"),
        (r"Hs2[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
    ],
    "stator_bs0": [
        (r"定子.*?Bs0[：:\s]*(\d+\.?\d*)", "mm"),
        (r"Bs0[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
    ],
    "stator_bs1": [
        (r"定子.*?Bs1[：:\s]*(\d+\.?\d*)", "mm"),
        (r"Bs1[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
    ],
    "stator_bs2": [
        (r"定子.*?Bs2[：:\s]*(\d+\.?\d*)", "mm"),
        (r"Bs2[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
    ],
    "stator_rs": [
        (r"定子.*?Rs[：:\s]*(\d+\.?\d*)", "mm"),
        (r"槽底半径[Rs：:\s]*(\d+\.?\d*)", "mm"),
        (r"Rs[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
    ],
    "stator_slot_ins": [
        (r"槽绝缘[厚度：:\s]*(\d+\.?\d*)", "mm"),
        (r"槽绝缘[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
        (r"定子槽绝缘[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    # ═══ 4. 转子参数 (Rotor) ═══
    "rotor_od": [
        (r"转子外径[：:\s]*(\d+\.?\d*)", "mm"),
        (r"转子外径[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
    ],
    "rotor_id": [
        (r"转子内径[：:\s]*(\d+\.?\d*)", "mm"),
        (r"转子内径[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
    ],
    "rotor_core_length": [
        (r"转子铁心长度[：:\s]*(\d+\.?\d*)", "mm"),
        (r"转子铁芯长度[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "slots_rotor": [
        (r"转子槽数[：:\s]*(\d+)", ""),
        (r"转子槽[数：\s]*(\d+)", ""),
    ],
    "rotor_cast": [
        (r"铸铝转子[：:\s]*(\S+)", ""),
    ],
    "rotor_double_cage": [
        (r"双笼转子[：:\s]*(\S+)", ""),
    ],
    "rotor_deep_bar": [
        (r"深槽转子[：:\s]*(\S+)", ""),
    ],
    "rotor_wound": [
        (r"绕线转子[：:\s]*(\S+)", ""),
    ],
    # ═══ 5. 转子槽形参数 (Rotor Slot) ═══
    "rotor_slot_type": [
        (r"转子槽形类型[：:\s]*([^\n\r]+)", ""),
        (r"转子槽形[：:\s]*([^\n\r]+)", ""),
    ],
    "rotor_hs0": [
        (r"转子.*?Hs0[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "rotor_hs1": [
        (r"转子.*?Hs1[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "rotor_hs2": [
        (r"转子.*?Hs2[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "rotor_bs0": [
        (r"转子.*?Bs0[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "rotor_bs1": [
        (r"转子.*?Bs1[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "rotor_bs2": [
        (r"转子.*?Bs2[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    # ═══ 6. 绕组参数 (Winding) ═══
    "winding_type": [
        (r"绕组类型[：:\s]*([^\n\r]+)", ""),
        (r"绕组形式[：:\s]*([^\n\r]+)", ""),
        (r"绕组方式[：:\s]*([^\n\r]+)", ""),
    ],
    "winding_layer": [
        (r"绕组层数[：:\s]*(\d+)", ""),
        (r"单层", "1"),
        (r"双层", "2"),
    ],
    "parallel_branches": [
        (r"并联支路[数：\s]*(\d+)", ""),
        (r"并联支路数[：:\s]*(\d+)", ""),
        (r"并联[：:\s]*(\d+)\s*路", ""),
    ],
    "conductors_per_slot": [
        (r"每槽导体数[：:\s]*(\d+)", ""),
        (r"每槽线数[：:\s]*(\d+)", ""),
    ],
    "turns_per_coil": [
        (r"每圈匝数[：:\s]*(\d+)", ""),
        (r"每线圈匝数[：:\s]*(\d+)", ""),
        (r"匝数[：:\s]*(\d+)", ""),
    ],
    "strands": [
        (r"并绕根数[：:\s]*(\d+)", ""),
        (r"并绕[：:\s]*(\d+)\s*根", ""),
        (r"(\d+)\s*根并绕", ""),
    ],
    "wire_dia": [
        (r"线径[：:\s]*(\d+\.?\d*)", "mm"),
        (r"导线直径[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "wire_wrap": [
        (r"线径包带[：:\s]*(\d+\.?\d*)", "mm"),
        (r"包带[厚度：:\s]*(\d+\.?\d*)", "mm"),
        (r"绝缘层[厚度：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "winding_pitch": [
        (r"绕组节距[：:\s]*([0-9\-/]+)", ""),
        (r"节距[：:\s]*([0-9\-/]+)", ""),
    ],
    "coil_pitch": [
        (r"线圈节距[：:\s]*([0-9\-/]+)", ""),
    ],
    "connection_type": [
        (r"接法[：:\s]*([A-Za-z\u4e00-\u9fa5△ΔY/\-]+)", ""),
        (r"联结[：:\s]*([A-Za-z\u4e00-\u9fa5△ΔY]+)", ""),
    ],
    "rotor_winding_type": [
        (r"转子绕组类型[：:\s]*([^\n\r]+)", ""),
        (r"转子绕组方式[：:\s]*([^\n\r]+)", ""),
    ],
    "rotor_bar_material": [
        (r"转子条材料[：:\s]*([^\n\r]+)", ""),
        (r"导条材料[：:\s]*([^\n\r]+)", ""),
    ],
    "end_ring_material": [
        (r"端环材料[：:\s]*([^\n\r]+)", ""),
    ],
    "rotor_bar_width": [
        (r"转子条宽[：:\s]*(\d+\.?\d*)", "mm"),
        (r"导条宽[度：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "rotor_bar_height": [
        (r"转子条高[：:\s]*(\d+\.?\d*)", "mm"),
        (r"导条高[度：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "end_ring_width": [
        (r"端环宽[度：:\s]*(\d+\.?\d*)", "mm"),
        (r"端环宽[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "end_ring_height": [
        (r"端环高[度：:\s]*(\d+\.?\d*)", "mm"),
        (r"端环高[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    # ═══ 7. 转轴参数 (Shaft) ═══
    "shaft_dia": [
        (r"轴径[：:\s]*(\d+\.?\d*)", "mm"),
        (r"轴直径[：:\s]*(\d+\.?\d*)", "mm"),
    ],
    "shaft_length": [
        (r"轴长度[：:\s]*(\d+\.?\d*)", "mm"),
        (r"轴长[：:\s]*(\d+\.?\d*)\s*mm", "mm"),
    ],
    "friction_loss": [
        (r"摩擦损耗[：:\s]*(\d+\.?\d*)", "W"),
    ],
    "wind_loss": [
        (r"风磨损耗[：:\s]*(\d+\.?\d*)", "W"),
        (r"风摩损耗[：:\s]*(\d+\.?\d*)", "W"),
    ],
    "reference_speed": [
        (r"参考转速[：:\s]*(\d+)", "r/min"),
        (r"参考转速[：:\s]*(\d+)\s*rpm", "r/min"),
    ],
    # ═══ 8. 材料参数 (Material) ═══
    "stator_material": [
        (r"定子铁[芯芯]材料[：:\s]*([A-Za-z0-9\-_]+)", ""),
        (r"定子材料[：:\s]*([A-Za-z0-9\-_]+)", ""),
    ],
    "rotor_material": [
        (r"转子铁[芯芯]材料[：:\s]*([A-Za-z0-9\-_]+)", ""),
        (r"转子材料[：:\s]*([A-Za-z0-9\-_]+)", ""),
    ],
    "conductor_material": [
        (r"导体材料[：:\s]*([A-Za-z\u4e00-\u9fa5]+)", ""),
        (r"导线材料[：:\s]*([A-Za-z\u4e00-\u9fa5]+)", ""),
    ],
    "shaft_material": [
        (r"轴材料[：:\s]*([A-Za-z0-9\u4e00-\u9fa5_]+)", ""),
        (r"轴材[：:\s]*([A-Za-z0-9\u4e00-\u9fa5_]+)", ""),
    ],
    # ═══ 9. 性能参数 (Performance) ═══
    "efficiency": [
        (r"效率[：:\s]*(\d+\.?\d*)\s*%?", "%"),
        (r"η[=：:\s]*(\d+\.?\d*)\s*%?", "%"),
    ],
    "power_factor": [
        (r"功率因数[：:\s]*(\d+\.?\d*)", ""),
        (r"[Cc][Oo][Ss][φφΦ]?[=：:\s]*(\d+\.?\d*)", ""),
        (r"cos\s*φ\s*[=：:\s]*(\d+\.?\d*)", ""),
    ],
    "starting_torque": [
        (r"起动转矩[：:\s]*(\d+\.?\d*)", "N.m"),
        (r"堵转转矩[：:\s]*(\d+\.?\d*)", "N.m"),
    ],
    "max_torque": [
        (r"最大转矩[：:\s]*(\d+\.?\d*)", "N.m"),
        (r"峰值转矩[：:\s]*(\d+\.?\d*)", "N.m"),
    ],
    "starting_current": [
        (r"起动电流[：:\s]*(\d+\.?\d*)", "A"),
        (r"堵转电流[：:\s]*(\d+\.?\d*)", "A"),
    ],
    "temperature_rise": [
        (r"温升[：:\s]*(\d+\.?\d*)\s*K?", "K"),
    ],
    "noise_level": [
        (r"噪声[：:\s]*(\d+\.?\d*)\s*dB", "dB"),
        (r"噪音[：:\s]*(\d+\.?\d*)\s*dB", "dB"),
    ],
    "vibration_level": [
        (r"振动[等级：:\s]*([A-C])", ""),
    ],
    # ═══ 10. 电磁参数 (Electromagnetic) ═══
    "stator_tooth_flux": [
        (r"定子齿磁密[：:\s]*(\d+\.?\d*)", "T"),
        (r"齿磁密[：:\s]*(\d+\.?\d*)\s*T", "T"),
    ],
    "stator_yoke_flux": [
        (r"定子轭磁密[：:\s]*(\d+\.?\d*)", "T"),
        (r"轭磁密[：:\s]*(\d+\.?\d*)\s*T", "T"),
    ],
    "rotor_yoke_flux": [
        (r"转子轭磁密[：:\s]*(\d+\.?\d*)", "T"),
    ],
    "air_gap_flux": [
        (r"气隙磁密[：:\s]*(\d+\.?\d*)", "T"),
        (r"气隙磁通密度[：:\s]*(\d+\.?\d*)", "T"),
    ],
    "current_density": [
        (r"电流密度[：:\s]*(\d+\.?\d*)", "A/mm2"),
        (r"电密[：:\s]*(\d+\.?\d*)", "A/mm2"),
    ],
    # ═══ 11. 损耗参数 (Losses) ═══
    "copper_loss": [
        (r"铜耗[：:\s]*(\d+\.?\d*)", "W"),
        (r"铜损[：:\s]*(\d+\.?\d*)\s*W", "W"),
    ],
    "iron_loss": [
        (r"铁耗[：:\s]*(\d+\.?\d*)", "W"),
        (r"铁损[：:\s]*(\d+\.?\d*)\s*W", "W"),
    ],
    "total_loss": [
        (r"总损耗[：:\s]*(\d+\.?\d*)", "W"),
    ],
    "stray_loss": [
        (r"杂散损耗[：:\s]*(\d+\.?\d*)", "W"),
        (r"附加损耗[：:\s]*(\d+\.?\d*)", "W"),
    ],
}

# 字段中文映射 (v4.01.009: 11大类全字段覆盖)
_OCR_FIELD_LABELS = {
    # 1. 通用参数
    "motor_type": "型号", "rated_power": "额定功率", "rated_voltage": "额定电压",
    "rated_current": "额定电流", "rated_speed": "额定转速", "rated_freq": "额定频率",
    "poles": "极数", "phase": "相数",
    "operating_temp": "工作温度", "load_type": "负载类型",
    "insulation_class": "绝缘等级", "protection_class": "防护等级", "cooling_method": "冷却方式",
    # 2. 定子参数
    "stator_od": "定子外径", "stator_id": "定子内径", "core_length": "铁心长度",
    "air_gap": "气隙长度", "slots_stator": "定子槽数",
    "lamination_sectors": "叠片扇形数", "stator_skew": "定子斜槽",
    "tooth_width": "齿宽", "yoke_thickness": "轭厚",
    # 3. 定子槽形参数
    "stator_slot_type": "定子槽形类型",
    "stator_hs0": "定子槽口高Hs0", "stator_hs1": "定子槽肩高Hs1", "stator_hs2": "定子槽身高Hs2",
    "stator_bs0": "定子槽口宽Bs0", "stator_bs1": "定子槽肩宽Bs1", "stator_bs2": "定子槽身宽Bs2",
    "stator_rs": "定子槽底半径Rs", "stator_slot_ins": "定子槽绝缘",
    # 4. 转子参数
    "rotor_od": "转子外径", "rotor_id": "转子内径", "rotor_core_length": "转子铁心长度",
    "slots_rotor": "转子槽数",
    "rotor_cast": "铸铝转子", "rotor_double_cage": "双笼转子",
    "rotor_deep_bar": "深槽转子", "rotor_wound": "绕线转子",
    # 5. 转子槽形参数
    "rotor_slot_type": "转子槽形类型",
    "rotor_hs0": "转子槽口高Hs0", "rotor_hs1": "转子槽肩高Hs1", "rotor_hs2": "转子槽身高Hs2",
    "rotor_bs0": "转子槽口宽Bs0", "rotor_bs1": "转子槽肩宽Bs1", "rotor_bs2": "转子槽身宽Bs2",
    # 6. 绕组参数
    "winding_type": "绕组类型", "winding_layer": "绕组层数",
    "parallel_branches": "并联支路数", "conductors_per_slot": "每槽导体数",
    "turns_per_coil": "每圈匝数", "strands": "并绕根数",
    "wire_dia": "线径", "wire_wrap": "线径包带",
    "winding_pitch": "绕组节距", "coil_pitch": "线圈节距",
    "connection_type": "接法",
    "rotor_winding_type": "转子绕组类型",
    "rotor_bar_material": "转子条材料", "end_ring_material": "端环材料",
    "rotor_bar_width": "转子条宽", "rotor_bar_height": "转子条高",
    "end_ring_width": "端环宽", "end_ring_height": "端环高",
    # 7. 转轴参数
    "shaft_dia": "轴径", "shaft_length": "轴长度",
    "friction_loss": "摩擦损耗", "wind_loss": "风磨损耗", "reference_speed": "参考转速",
    # 8. 材料参数
    "stator_material": "定子铁芯材料", "rotor_material": "转子铁芯材料",
    "conductor_material": "导体材料", "shaft_material": "轴材料",
    # 9. 性能参数
    "efficiency": "效率", "power_factor": "功率因数",
    "starting_torque": "起动转矩", "max_torque": "最大转矩",
    "starting_current": "起动电流", "temperature_rise": "温升",
    "noise_level": "噪声水平", "vibration_level": "振动等级",
    # 10. 电磁参数
    "stator_tooth_flux": "定子齿磁密", "stator_yoke_flux": "定子轭磁密",
    "rotor_yoke_flux": "转子轭磁密", "air_gap_flux": "气隙磁密", "current_density": "电流密度",
    # 11. 损耗参数
    "copper_loss": "铜耗", "iron_loss": "铁耗", "total_loss": "总损耗",
    "stray_loss": "杂散损耗",
}


def _extract_motor_params(text):
    """从文本中提取电机参数 (v4.01.009: 11大类全字段正则提取)"""
    results = {}
    text_lower = text.lower()

    # v4.01.009: 整数类型字段
    _int_fields = {"poles", "phase", "rated_voltage", "rated_freq", "rated_speed",
                   "slots_stator", "slots_rotor", "turns_per_coil", "parallel_branches",
                   "conductors_per_slot", "strands", "coil_pitch", "stator_skew",
                   "reference_speed", "lamination_sectors", "winding_layer"}
    # v4.01.009: 浮点类型字段
    _float_fields = {"rated_power", "rated_current", "efficiency", "power_factor",
                     "operating_temp",
                     "stator_od", "stator_id", "rotor_od", "core_length", "air_gap",
                     "rotor_core_length",
                     "shaft_dia", "tooth_width", "yoke_thickness", "shaft_length",
                     "stator_hs0", "stator_hs1", "stator_hs2", "stator_bs0", "stator_bs1",
                     "stator_bs2", "stator_rs", "stator_slot_ins",
                     "rotor_id", "rotor_hs0", "rotor_hs1", "rotor_hs2",
                     "rotor_bs0", "rotor_bs1", "rotor_bs2",
                     "wire_dia", "wire_wrap", "rotor_bar_width", "rotor_bar_height",
                     "end_ring_width", "end_ring_height",
                     "starting_torque", "max_torque", "starting_current",
                     "noise_level", "temperature_rise",
                     "stator_tooth_flux", "stator_yoke_flux", "rotor_yoke_flux",
                     "air_gap_flux", "current_density",
                     "copper_loss", "iron_loss", "total_loss", "friction_loss",
                     "wind_loss", "stray_loss"}

    for field, patterns in _OCR_PATTERNS.items():
        for pattern, unit in patterns:
            match = _re.search(pattern, text, _re.IGNORECASE)
            if match:
                # v4.01.009 修复: 处理无捕获组的模式 (如 r"三相", r"单层")
                # 之前: raw_val = match.group(1) 直接 IndexError → NameError → HTTP 500
                try:
                    raw_val = match.group(1) if match.lastindex else match.group(0)
                except (IndexError, AttributeError):
                    raw_val = match.group(0)

                # 类型转换
                try:
                    if field in _int_fields:
                        val = int(float(raw_val))
                    elif field in _float_fields:
                        val = float(raw_val)
                    else:
                        val = raw_val.strip()
                except (ValueError, TypeError):
                    val = raw_val.strip() if raw_val else ""

                # v4.01.009 修复: 关键字模式 (raw_val 是 "三相"/"单层" 而 unit 是 "3"/"1")
                # 这种情况 val 不是数字, 但 unit 是 — 用 unit 作为值
                if not val or not str(val).replace('.', '').replace('-', '').isdigit():
                    if unit and str(unit).replace('.', '').replace('-', '').isdigit():
                        val = int(unit) if field in _int_fields else (float(unit) if field in _float_fields else unit)
                        unit = ""

                results[field] = {
                    "value": val,
                    "unit": unit,
                    "label": _OCR_FIELD_LABELS.get(field, field),
                    "raw_match": match.group(0),
                }
                break  # 取第一个匹配

    return results


@app.route("/api/case-motor/parse-text", methods=["POST"])
def cm_parse_text():
    """v4.05: 从文本中提取电机铭牌参数 (不需要OCR引擎)"""
    try:
        data = request.get_json()
        text = data.get("text", "")
        if not text.strip():
            return jsonify({"error": "文本不能为空"}), 400

        params = _extract_motor_params(text)
        return jsonify({
            "success": True,
            "raw_text": text,
            "params": params,
            "param_count": len(params),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/ocr", methods=["POST"])
def cm_ocr():
    """v4.05: OCR 扫描电机铭牌/参数表图片"""
    try:
        if "image" not in request.files:
            return jsonify({"error": "未提供图片文件"}), 400

        file = request.files["image"]
        if not file.filename:
            return jsonify({"error": "未选择文件"}), 400

        # 读取图片
        from PIL import Image
        import io as _io
        img_data = file.read()
        img = Image.open(_io.BytesIO(img_data))

        # 尝试 OCR
        try:
            import pytesseract
            # v4.06: 设置 Tesseract 路径 (Windows 默认安装路径)
            import os as _os
            _tess_path = r"D:\Program Files\Tesseract-OCR\tesseract.exe"
            if _os.path.exists(_tess_path):
                pytesseract.pytesseract.tesseract_cmd = _tess_path
            # 图片预处理: 转灰度 + 放大 (提升识别率)
            if img.width < 1000:
                _scale = 2
                img = img.resize((img.width * _scale, img.height * _scale), Image.LANCZOS)
            img_gray = img.convert("L")
            # 尝试中文+英文识别
            raw_text = pytesseract.image_to_string(img_gray, lang="chi_sim+eng")
        except ImportError:
            return jsonify({
                "success": False,
                "error": "OCR引擎未安装",
                "hint": "请安装 Tesseract OCR: 1) 下载安装 tesseract-ocr 2) pip install pytesseract",
                "fallback_url": "/api/case-motor/parse-text",
            }), 200
        except Exception as ocr_err:
            # Tesseract 已安装但识别失败 (可能是语言包缺失)
            return jsonify({
                "success": False,
                "error": f"OCR识别失败: {ocr_err}",
                "hint": "可能缺少中文语言包, 请安装 tesseract-ocr-chi-sim",
                "fallback_url": "/api/case-motor/parse-text",
            }), 200

        # 提取参数
        params = _extract_motor_params(raw_text)

        return jsonify({
            "success": True,
            "raw_text": raw_text.strip(),
            "params": params,
            "param_count": len(params),
            "image_size": f"{img.width}x{img.height}",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/ai-recognize", methods=["POST"])
def cm_ai_recognize():
    """v4.07: AI智能识别 — 使用LLM从文本中提取电机铭牌参数

    接收 OCR 原始文本或用户输入文本, 调用 OCRRecognizeAgent 进行 LLM 智能识别,
    与正则提取结果合并后返回。

    请求体: {"text": "...", "regex_params": {...}}
    """
    try:
        data = request.get_json()
        text = data.get("text", "")
        if not text.strip():
            return jsonify({"error": "文本不能为空"}), 400

        # v4.07: 当前使用的 OCR AI 模型名 (供客户端日志显示)
        _ocr_model = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")

        regex_params = data.get("regex_params", {})

        if not CM_DB_OK or not hasattr(_cm_ocr_agent, 'run'):
            # AI agent 不可用, 仅返回正则结果
            params = _extract_motor_params(text)
            return jsonify({
                "success": True,
                "raw_text": text,
                "params": params,
                "param_count": len(params),
                "ai_count": 0,
                "regex_count": len(params),
                "ai_available": False,
                "model": _ocr_model,
                "model_status": "disabled",
            })

        # 正则提取 (如果客户端未传)
        if not regex_params:
            regex_params = _extract_motor_params(text)

        # v4.00.007: AI 智能识别 + 自动回退 (防止 LLM 卡死导致整个接口挂死)
        # v4.00.008: 传递 elapsed/model_status 统计信息
        ai_used = False
        ai_error = None
        merged_params = None
        elapsed = 0
        model_status = "unknown"
        try:
            result = _cm_ocr_agent.run(text, regex_params)
            merged_params = result["params"]
            ai_count = result["ai_count"]
            regex_count = result["regex_count"]
            inferred_count = result.get("inferred_count", 0)
            validation = result.get("validation", [])
            warnings = result.get("warnings", [])
            ai_used = ai_count > 0
            elapsed = result.get("elapsed", 0)
            model_status = result.get("model_status", "unknown")
        except Exception as e:
            # LLM 调用超时/失败/挂死 → 自动回退到正则结果, 不让客户端完全失败
            logger.warning("[cm_ai_recognize] LLM 异常, 回退到纯正则: %s", e)
            ai_error = str(e)
            merged_params = {k: {**v, "source": "regex"} for k, v in regex_params.items()}
            ai_count = 0
            regex_count = len(regex_params)
            inferred_count = 0
            validation = []
            warnings = [f"AI识别暂时不可用, 已回退到正则提取 ({type(e).__name__})"]
            model_status = "exception"

        return jsonify({
            "success": True,
            "raw_text": text,
            "params": merged_params,
            "param_count": len(merged_params),
            "ai_count": ai_count,
            "regex_count": regex_count,
            "inferred_count": inferred_count,
            "validation": validation,
            "warnings": warnings,
            "ai_available": ai_used,
            "fallback_reason": ai_error if not ai_used else None,
            "elapsed": elapsed,                    # v4.00.008: 总耗时秒
            "model": _ocr_model,                   # v4.07: 当前OCR AI模型名 (客户端日志显示)
            "model_status": model_status,          # v4.00.008: healthy/unhealthy/cached/exception
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/ocr-vl", methods=["POST"])
def cm_ocr_vl():
    """v4.01.023: 多模态 VL 识别 — 直接识别电机铭牌/手册图片 (qwen3-vl:2b)

    接收图片文件, 调用 OCRRecognizeAgent.vl_recognize 走多模态视觉识别,
    复用知识补全 + 物理校验 + 置信度后处理闭环, 返回结构化参数。
    默认 VL 模型已升级为 qwen3-vl:2b-instruct (更轻量, T600 4GB 可跑; 等待上限 300s)。
    """
    try:
        if "image" not in request.files:
            return jsonify({"error": "未提供图片文件"}), 400

        file = request.files["image"]
        if not file.filename:
            return jsonify({"error": "未选择文件"}), 400

        from PIL import Image
        import io as _io
        img_data = file.read()
        img = Image.open(_io.BytesIO(img_data))
        fmt = (img.format or "JPEG").upper()

        if not CM_DB_OK or not hasattr(_cm_ocr_agent, 'vl_recognize'):
            return jsonify({
                "success": False,
                "error": "VL 识别不可用",
                "hint": "请确认 OCRImageRecognizeAgent 已加载且 Ollama 已安装 qwen3-vl:2b-instruct 模型",
            }), 200

        result = _cm_ocr_agent.vl_recognize(img_data, fmt)

        params = result.get("params", {})
        model_status = result.get("model_status", "unknown")
        vl_model = result.get("vl_model", "qwen3-vl:2b-instruct")

        # v4.01.023: 模型未安装时给出明确安装命令
        if model_status == "model_not_found":
            return jsonify({
                "success": False,
                "error": f"Ollama 未安装 VL 模型 {vl_model}",
                "hint": f"请在服务器执行: ollama pull {vl_model}",
                "params": params,
                "param_count": len(params),
                "vl_count": result.get("vl_count", 0),
                "inferred_count": result.get("inferred_count", 0),
                "validation": result.get("validation", []),
                "warnings": result.get("warnings", []),
                "model_status": model_status,
                "vl_model": vl_model,
                "elapsed": result.get("elapsed", 0),
            })

        # VL 模型不可用 / 不健康 → 返回可降级提示
        if model_status in ("unhealthy", "no_result"):
            reason = "Ollama 未运行或未加载 VL 模型" if model_status == "unhealthy" \
                else "VL 模型未返回有效结果"
            return jsonify({
                "success": False,
                "error": reason,
                "hint": f"请确认 Ollama 已安装 {vl_model} 模型",
                "params": params,
                "param_count": len(params),
                "vl_count": result.get("vl_count", 0),
                "inferred_count": result.get("inferred_count", 0),
                "validation": result.get("validation", []),
                "warnings": result.get("warnings", []),
                "model_status": model_status,
                "vl_model": vl_model,
                "elapsed": result.get("elapsed", 0),
            })

        return jsonify({
            "success": True,
            "raw_text": "[多模态VL视觉识别]",
            "params": params,
            "param_count": len(params),
            "vl_count": result.get("vl_count", 0),
            "inferred_count": result.get("inferred_count", 0),
            "validation": result.get("validation", []),
            "warnings": result.get("warnings", []),
            "model_status": model_status,
            "vl_model": vl_model,
            "elapsed": result.get("elapsed", 0),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/ocr-stats", methods=["GET"])
def cm_ocr_stats():
    """v4.00.008: OCR 识别统计 (供数据飞轮/运营分析师消费)"""
    try:
        if not CM_DB_OK or not hasattr(_cm_ocr_agent, 'get_stats'):
            return jsonify({"error": "OCR统计不可用"}), 500
        stats = _cm_ocr_agent.get_stats()
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/<int:case_id>/submit", methods=["POST"])
def cm_submit(case_id):
    """提交至候选库 (触发人工审核)"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        case = get_case_motor(case_id)
        if not case:
            return jsonify({"error": "案例不存在"}), 404
        # 更新状态为待审核
        update_case_motor(case_id, {"status": "pending_review"})
        # 创建人工审核检查点
        try:
            if not HUMAN_CP_OK:
                raise ImportError(f"human_checkpoint 模块未加载: {HUMAN_CP_ERR}")
            checkpoint_id = hc.create_checkpoint(
                project_id=f"CASE_{case_id}",
                checkpoint_type="case_motor_review",
                title=f"案例电机入库审核: {case.get('motor_type', '')}",
                description=f"案例编号 {case.get('case_code', '')} 提交入库审核, 请确认参数完整性和准确性后通过。",
                context={"case_id": case_id, "case_code": case.get("case_code", ""),
                         "motor_type": case.get("motor_type", "")},
                default_action="auto_approve",
            )
            return jsonify({
                "success": True,
                "status": "pending_review",
                "checkpoint_id": checkpoint_id,
            })
        except Exception as hc_err:
            # 人工检查点创建失败不影响提交流程
            return jsonify({"success": True, "status": "pending_review", "checkpoint_error": str(hc_err)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/<int:case_id>/review", methods=["POST"])
def cm_review(case_id):
    """v4.04: 管理端审核案例电机 (approve/reject)"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        data = request.get_json()
        decision = data.get("decision", "").strip()
        comment = data.get("comment", "")
        if decision not in ("approve", "reject"):
            return jsonify({"error": "decision must be 'approve' or 'reject'"}), 400

        case = get_case_motor(case_id)
        if not case:
            return jsonify({"error": "案例不存在"}), 404

        # 更新案例状态
        new_status = "approved" if decision == "approve" else "rejected"
        update_case_motor(case_id, {
            "status": new_status,
            "review_comment": comment,
            "reviewed_by": "admin",
            "reviewed_at": datetime.now().isoformat(),
        })

        # 如果有对应的检查点, 解除阻塞
        checkpoint_id = data.get("checkpoint_id", "")
        if checkpoint_id and HUMAN_CP_OK:
            try:
                hc.submit_decision(checkpoint_id, decision, comment)
            except Exception:
                pass

        return jsonify({
            "success": True,
            "case_id": case_id,
            "status": new_status,
            "decision": decision,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/<int:case_id>/bloodline", methods=["GET"])
def cm_bloodline(case_id):
    """获取参数血缘记录"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        bloodline = get_param_bloodline(case_id)
        return jsonify({"success": True, "bloodline": bloodline})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/similar", methods=["POST"])
def cm_find_similar():
    """v4.01.042 (#523 一.3): 审核单条时检测库内相近/相同电机

    请求体: {
        "candidate": {电机参数 dict, 至少含 motor_model/rated_power/poles 之一},
        "exclude_id": 可选, 排除自身,
        "top_n": 5, "threshold": 0.80, "status_filter": 可选
    }
    返回: { success, matches:[{case_id, motor_model, similarity_pct, verdict_cn,
             same_fields, diff_fields, suggest_action, ...}] }
    """
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        data = request.get_json() or {}
        candidate = data.get("candidate") or {}
        if not isinstance(candidate, dict) or not candidate:
            return jsonify({"error": "candidate 必须为非空电机参数字典"}), 400
        exclude_id = data.get("exclude_id")
        top_n = int(data.get("top_n", 5) or 5)
        threshold = float(data.get("threshold", 0.80) or 0.80)
        status_filter = data.get("status_filter") or None
        matches = find_similar_case_motors(
            candidate, exclude_id=exclude_id, top_n=top_n,
            threshold=threshold, status_filter=status_filter)
        return jsonify({
            "success": True,
            "count": len(matches),
            "threshold": threshold,
            "matches": matches,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/compare-batch", methods=["POST"])
def cm_compare_batch():
    """v4.01.042 (#523 一.2): 系列电机批量录入打包审核 — 批次内横向对比

    请求体: { "items": [电机参数 dict, ...] }
    返回: { success, count, common:[一致字段], differences:[差异字段逐台取值],
            pairwise:[两两相似度], has_duplicate, duplicate_pairs }
    """
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        data = request.get_json() or {}
        items = data.get("items") or []
        if not isinstance(items, list) or not items:
            return jsonify({"error": "items 必须为非空电机列表"}), 400
        if len(items) > 200:
            return jsonify({"error": "单次对比上限 200 台"}), 400
        result = compare_case_motor_batch(items)
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500





@app.route("/api/case-motor/<int:case_id>/param-bloodline", methods=["POST"])
def cm_update_bloodline(case_id):
    """更新单个参数血缘"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        data = request.get_json(force=True)
        param_id = upsert_param_bloodline(
            case_id=case_id,
            param_name=data["param_name"],
            param_value=str(data["param_value"]),
            param_unit=data.get("param_unit", ""),
            source_type=data.get("source_type", "user_input"),
            source_detail=data.get("source_detail", ""),
            confidence=data.get("confidence", 100),
            status=data.get("status", "confirmed"),
            modified_reason=data.get("modified_reason", ""),
            modified_note=data.get("modified_note", ""),
        )
        return jsonify({"success": True, "param_id": param_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/drafts", methods=["GET"])
def cm_drafts():
    """获取草稿列表"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        user_id = g.get("user", {}).get("id", 0) if g.get("user") else 0
        drafts = list_drafts(user_id)
        return jsonify({"success": True, "drafts": drafts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/draft", methods=["POST"])
def cm_save_draft():
    """保存草稿"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        data = request.get_json(force=True)
        user_id = g.get("user", {}).get("id", 0) if g.get("user") else 0
        draft_id = save_draft(
            case_id=data.get("case_id"),
            user_id=user_id,
            draft_data=data.get("draft_data", {}),
        )
        return jsonify({"success": True, "draft_id": draft_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/calculate", methods=["POST"])
def cm_calculate():
    """计算反推: 输入关键设计变量, 反推标准参数"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        data = request.get_json(force=True)
        # 使用 ParamSuggestAgent 做反推
        params = data.get("params", {})
        series_id = data.get("series_id", "")
        result = _cm_suggest_agent.safe_run(params=params, series_id=series_id)
        return jsonify({"success": True, "calculated": result.get("suggestions", [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/stats", methods=["GET"])
def cm_stats():
    """获取案例电机统计"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        stats = cm_get_stats()
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _diag_conflict(tag, **kw):
    """TEMP DIAGNOSTIC (#538 冲突诊断): 仅写文件日志, 不改业务逻辑. 诊断完成后整段删除."""
    try:
        import datetime
        line = "[%s] %s | " % (datetime.datetime.now().strftime("%H:%M:%S.%f"), tag)
        line += " | ".join("%s=%s" % (k, v) for k, v in kw.items())
        with open(r"D:\MotorDesign\diagnose_cm_import.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


@app.route("/api/case-motor/import", methods=["POST"])
def cm_import():
    """批量导入 (CSV/Excel)"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        import csv as csv_mod
        import io

        if "file" not in request.files:
            return jsonify({"error": "未上传文件"}), 400
        file = request.files["file"]
        if not file.filename:
            return jsonify({"error": "文件名为空"}), 400

        # 读取文件内容
        content = file.read().decode("utf-8-sig")
        reader = csv_mod.DictReader(io.StringIO(content))

        # 获取字段映射
        field_map_str = request.form.get("field_map", "{}")
        import json as json_mod
        field_map = json_mod.loads(field_map_str) if field_map_str else {}

        # 导入级系列号 (强约束: 用户必须先选一个录入系列)
        import_series_id = (request.form.get("series_id") or "").strip()
        import_series_name = (request.form.get("series_name") or "").strip()

        imported, skipped, conflicts = [], [], []
        for row in reader:
            data = {}
            row_series_id = import_series_id
            row_series_name = import_series_name
            row_motor_type = ""
            row_motor_model = ""
            for csv_col, sys_field in field_map.items():
                if csv_col not in row or not sys_field:
                    continue
                val = row[csv_col].strip()
                if not val:
                    continue
                # 特殊字段: 系列号 / 类型类别 / 具体型号 (来自 CSV 映射时优先 CSV)
                if sys_field == "series_id":
                    row_series_id = val
                    continue
                if sys_field == "series_name":
                    row_series_name = val
                    continue
                if sys_field == "motor_type":
                    row_motor_type = val
                    continue
                if sys_field == "motor_model":
                    row_motor_model = val
                    continue
                # 常规参数: 尝试数值转换
                try:
                    data[sys_field] = float(val) if "." in val else int(val)
                except ValueError:
                    data[sys_field] = val

            # v4.01.033: 型号→类型类别 自动推导 (具体型号优先于类型类别)
            if not row_motor_type and row_motor_model:
                row_motor_type = derive_motor_type(row_motor_model)

            if not data and not row_motor_type:
                continue

            data["motor_type"] = row_motor_type
            data["motor_model"] = row_motor_model
            data["series_id"] = row_series_id
            data["series_name"] = row_series_name
            data["completeness_score"] = calculate_completeness(data)
            data["source_type"] = "file_import"
            data["source_detail"] = f"批量导入: {file.filename}"

            # 冲突检测: 按 (系列号+型号) 查已录入
            fp = find_case_motor_by_fingerprint(
                row_series_id, row_motor_type,
                data.get("rated_power"), data.get("poles"), data.get("rated_speed"),
                motor_model=row_motor_model,
            )
            if fp:
                # 比较关键参数: 全部一致 -> 跳过(已录入); 否则 -> 冲突
                identical = (
                    _fp_equal(fp.get("rated_power"), data.get("rated_power"))
                    and _fp_equal(fp.get("poles"), data.get("poles"))
                    and _fp_equal(fp.get("rated_speed"), data.get("rated_speed"))
                )
                if identical:
                    skipped.append({
                        "existing_id": fp.get("id"),
                        "case_code": fp.get("case_code"),
                        "motor_type": row_motor_type,
                        "motor_model": row_motor_model,
                        "series_id": row_series_id,
                    })
                    continue
                # 参数不同 -> 冲突, 交由 AI 判断 + 人工确认
                conflicts.append({
                    "existing_id": fp.get("id"),
                    "case_code": fp.get("case_code"),
                    "motor_type": row_motor_type,
                    "motor_model": row_motor_model,
                    "series_id": row_series_id,
                    "existing": {k[0]: fp.get(k[0]) for k in MOTOR_PARAM_FIELDS},
                    "incoming": data,
                })
                _diag_conflict("CONFLICT", pos=len(conflicts), existing_id=fp.get("id"),
                               case_code=fp.get("case_code"),
                               incoming_keys=len(data),
                               incoming_empty=sum(1 for v in data.values() if v in (None, "", [])),
                               has_rated_power=bool(data.get("rated_power")),
                               has_rated_current=bool(data.get("rated_current")))
                continue
            # 无重复 -> 新增
            result = create_case_motor(data)
            imported.append(result)

        return jsonify({
            "success": True,
            "imported": len(imported),
            "skipped": len(skipped),
            "conflicts": conflicts,
            "cases": imported,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===================== 字段映射数据飞轮 (v4.01.044) =====================
# 闭环: 录入映射 learn -> 进化引擎(置信度) -> 规则库(全局共享·自动应用)
#        -> 预判 suggest -> 人工修正回灌 correct -> 持续优化
# DB 层: Core/field_mapping_flywheel.py ; 表随 init_case_motor_tables() 自动建

@app.route("/api/case-motor/mapping/learn", methods=["POST"])
def cm_mapping_learn():
    """字段映射飞轮 — 学习录入映射 (CSV导入/手动录入完成时调用)"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        from Core.field_mapping_flywheel import learn_mappings
        payload = request.get_json(force=True, silent=True) or {}
        mappings = payload.get("field_map") or payload.get("mappings") or []
        series_id = (payload.get("series_id") or "").strip()
        user_id = payload.get("user_id") or "anonymous"
        if not mappings:
            return jsonify({"error": "缺少 field_map / mappings"}), 400
        if not series_id:
            return jsonify({"error": "缺少 series_id (录入系列, 强约束)"}), 400
        result = learn_mappings(mappings, series_id, user_id)
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/mapping/suggest", methods=["GET", "POST"])
def cm_mapping_suggest():
    """字段映射飞轮 — 根据源字段预判系统字段映射 (录入前预填列头)

    支持 GET(?source_fields=重量,扭矩&series_id=Y2) 与 POST({source_fields:[...],series_id})
    source_fields 可为逗号分隔字符串或 JSON 列表
    """
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        import json as _json
        from Core.field_mapping_flywheel import suggest_mappings
        if request.method == "POST":
            payload = request.get_json(force=True, silent=True) or {}
            sf = payload.get("source_fields") or []
            if isinstance(sf, str):
                sf = [s.strip() for s in sf.split(",") if s.strip()]
            series_id = (payload.get("series_id") or "").strip()
        else:
            source_fields = request.args.get("source_fields") or ""
            if source_fields.startswith("["):
                try:
                    sf = _json.loads(source_fields)
                except Exception:
                    sf = [source_fields]
            else:
                sf = [s.strip() for s in source_fields.split(",") if s.strip()]
            series_id = (request.args.get("series_id") or "").strip()
        if not sf:
            return jsonify({"error": "缺少 source_fields"}), 400
        result = suggest_mappings(sf, series_id)
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/mapping/correct", methods=["POST"])
def cm_mapping_correct():
    """字段映射飞轮 — 人工修正回灌 (最终字段 != 建议字段时)"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        from Core.field_mapping_flywheel import record_correction
        payload = request.get_json(force=True, silent=True) or {}
        source_field = (payload.get("source_field") or "").strip()
        wrong_target = (payload.get("wrong_target") or "").strip()
        correct_target = (payload.get("correct_target") or "").strip()
        series_id = (payload.get("series_id") or "").strip()
        user_id = payload.get("user_id") or "anonymous"
        if not source_field or not correct_target:
            return jsonify({"error": "缺少 source_field / correct_target"}), 400
        result = record_correction(source_field, wrong_target, correct_target, series_id, user_id)
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/mapping/rules", methods=["GET"])
def cm_mapping_rules():
    """字段映射飞轮 — 查看已进化规则库 (管理/调试)"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        from Core.field_mapping_flywheel import list_rules
        raw_scope = (request.args.get("scope") or "all").strip()
        # "all"/"" 表示不过滤 scope (否则 SQL 会误查 scope='all' 无匹配)
        scope = raw_scope if raw_scope not in ("", "all") else None
        series_id = (request.args.get("series_id") or "").strip()
        try:
            min_confidence = float(request.args.get("min_confidence") or 0)
        except ValueError:
            min_confidence = 0.0
        only_auto = (request.args.get("only_auto") or "").lower() in ("1", "true", "yes")
        try:
            limit = int(request.args.get("limit") or 200)
        except ValueError:
            limit = 200
        rules = list_rules(
            scope=scope,
            series_id=series_id or None,
            min_confidence=min_confidence,
            only_auto=only_auto,
            limit=limit,
        )
        return jsonify({"success": True, "rules": rules, "count": len(rules)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/ai-compare", methods=["POST"])
def cm_ai_compare():
    """v4.01.026: AI 变量对照校核 — 比对已录入记录与本次导入数据, 给出冲突处理建议"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        payload = request.get_json(force=True, silent=True) or {}
        existing_id = payload.get("existing_id")
        incoming = payload.get("incoming") or {}

        # 优先从数据库取最新已录入记录 (避免传入的 existing 过期)
        existing = None
        if existing_id:
            existing = get_case_motor(int(existing_id)) if existing_id else None
        if not existing:
            existing = payload.get("existing") or {}

        rec = _cm_compare_agent.run(existing=existing, incoming=incoming)
        _adv = rec.get("advice")
        _diff = rec.get("diff_fields", []) or []
        _detail = (rec.get("detail") or "")
        # (#539 更细化诊断) 记录每个差异字段的 系统值/导入值 对照, 用于核对 UI "覆盖/跳过" 对应的数据
        _pairs = []
        for _f in _diff[:50]:
            _ev = existing.get(_f) if isinstance(existing, dict) else None
            _iv = incoming.get(_f) if isinstance(incoming, dict) else None
            _pairs.append("%s[sys=%r,in=%r]" % (_f, _ev, _iv))
        _diag_conflict("AI_COMPARE", existing_id=existing_id,
                       incoming_keys=len(incoming or {}),
                       incoming_empty=sum(1 for v in (incoming or {}).values() if v in (None, "", [])),
                       advice=_adv,
                       ui_resolved=("覆盖" if ("覆盖" in str(_adv) or "更新" in str(_adv)) else "跳过"),
                       diff_n=len(_diff),
                       diff_fields=",".join(_diff[:50]),
                       detail=_detail[:400],
                       pairs=" ; ".join(_pairs))
        return jsonify({
            "success": True,
            "advice": rec.get("advice", "保留"),
            "detail": rec.get("detail", ""),
            "diff_fields": rec.get("diff_fields", []),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case-motor/<int:case_id>/new-design", methods=["POST"])
def cm_new_design(case_id):
    """以此案例为模板发起新设计"""
    if not CM_DB_OK:
        return jsonify({"error": "案例电机模块未加载"}), 500
    try:
        case = get_case_motor(case_id)
        if not case:
            return jsonify({"error": "案例不存在"}), 404

        # 构建需求描述
        power = case.get("rated_power") or "??"
        voltage = case.get("rated_voltage") or 380
        speed = case.get("rated_speed") or "??"
        poles = case.get("poles") or "??"
        eff = case.get("efficiency") or "??"
        motor_type = case.get("motor_type", "")

        requirement = (
            f"参考案例电机 {motor_type} ({case.get('case_code', '')}), "
            f"设计一台 {power}kW、{voltage}V、{speed}r/min、{poles}极 三相异步电动机, "
            f"目标效率 >= {eff}%, "
            f"定子外径 {case.get('stator_od', '不限')}mm, "
            f"铁心长度 {case.get('core_length', '不限')}mm, "
            f"绝缘等级 {case.get('insulation_class', 'F')}, "
            f"防护等级 {case.get('protection_class', 'IP44')}, "
            f"冷却方式 {case.get('cooling_method', 'IC411')}"
        )

        return jsonify({
            "success": True,
            "requirement": requirement,
            "case_ref": {
                "case_id": case_id,
                "case_code": case.get("case_code", ""),
                "motor_type": motor_type,
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# DSH 集成路由 (阶段一: 独立运行通路) — v4.02.44
# ------------------------------------------------------------
# 依据《DSH 集成 MotorAI 可行性分析报告》9.3 改动清单。
# 设计约束: 纯新增通路, 不触碰 orchestrator.py / motor_tools.py /
#           workflow_config.py; DSH 不可用时 fail-closed 降级, 不阻断现有 pipeline。
# SSE 事件: dsh_progress (进行中) / dsh_result (完成) / dsh_error (失败)
# ============================================================
def _dsh_agent_or_none():
    """惰性导入 DSHAgent；不可用时返回 (None, err_str)。"""
    try:
        import sys as _sys
        _agents_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "Agents")
        if _agents_dir not in _sys.path:
            _sys.path.insert(0, _agents_dir)
        from dsh_agent import get_dsh_agent
        return get_dsh_agent(), ""
    except Exception as _e:
        return None, f"{type(_e).__name__}: {_e}"


def _dsh_env_flag(name, default=True):
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


# M5: DSH 生成后追加成对审查（现实检验师 + 魔鬼代言人），可用 DSH_REVIEW_ENABLED=0 关闭
_DSH_REVIEW_ENABLED = _dsh_env_flag("DSH_REVIEW_ENABLED", True)


def _dsh_run_review(res, specs):
    """M5: 对 DSH 生成结果追加成对审查，best-effort（任何异常都不影响主结果）。

    返回 {"enabled": bool, "reality": {...}, "devils": {...}, "errors": [...]}。
    """
    out = {"enabled": bool(_DSH_REVIEW_ENABLED), "reality": {}, "devils": {},
           "errors": []}
    if not _DSH_REVIEW_ENABLED:
        return out
    design = (res or {}).get("design") or {}
    if not design:
        out["errors"].append("无结构化 design，跳过审查")
        return out
    try:
        import sys as _sys
        _agents_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "Agents")
        if _agents_dir not in _sys.path:
            _sys.path.insert(0, _agents_dir)
        _sols = [dict(design)]
        try:
            from reality_checker import RealityChecker
            out["reality"] = RealityChecker().run(_sols, specs, top_n=1)
        except Exception as _e1:
            out["errors"].append(f"reality_checker: {type(_e1).__name__}: {_e1}")
        try:
            from devils_advocate import DevilsAdvocate
            out["devils"] = DevilsAdvocate().run(_sols, specs, top_n=1)
        except Exception as _e2:
            out["errors"].append(f"devils_advocate: {type(_e2).__name__}: {_e2}")
    except Exception as _e:
        out["errors"].append(f"{type(_e).__name__}: {_e}")
    return out


@app.route("/api/dsh/status", methods=["GET"])
def api_dsh_status():
    """DSH 可用性探测 (通道/端点/profile/CLI 二进制)。

    前端标签4 用它决定「生成 DSH 方案」按钮是否可用。DSH 不可用时
    返回 available=False + reason, 前端降级提示, 不影响现有 AI 团队流程。
    """
    agent, err = _dsh_agent_or_none()
    if agent is None:
        return jsonify({
            "success": False, "available": False, "mode": "none",
            "reason": f"DSHAgent 未就绪: {err}",
        }), 200
    try:
        st = agent.check_status(force=True)
        # M9 (可观测性): KB 版本 / 提示词版本 / 几何校验通过率 / 失败分类占比
        try:
            from dsh_agent import KB_CTX_VERSION, get_feedback_stats
            _fb = get_feedback_stats()
            st["kb_version"] = KB_CTX_VERSION
            st["prompt_ver"] = _fb.get("last_prompt_ver") or "v2_domain_scan"
            st["checker_pass_rate"] = _fb.get("checker_pass_rate_pct")
            st["checker_samples"] = _fb.get("checker_samples")
            st["fail_class_pct"] = _fb.get("fail_class_pct", {})
            st["fail_class_counts"] = _fb.get("fail_class_counts", {})
            st["gen_ok_rate_pct"] = _fb.get("ok_rate_pct")
            st["gen_samples"] = _fb.get("samples")
        except Exception as _he:
            st["health_error"] = f"{type(_he).__name__}: {_he}"
        st["success"] = True
        return jsonify(st), 200
    except Exception as e:
        return jsonify({
            "success": False, "available": False, "mode": "none",
            "reason": f"探测失败: {type(e).__name__}: {e}",
        }), 200


@app.route("/api/dsh/generate", methods=["POST"])
def api_dsh_generate():
    """触发 DSH 生成电机设计方案，返回 SSE 流。

    Request JSON:  {"specs": {...}, "timeout_s": 180}
    SSE events:    dsh_progress / dsh_result / dsh_error / done
    """
    agent, err = _dsh_agent_or_none()
    if agent is None:
        return jsonify({"error": f"DSHAgent 未就绪: {err}"}), 500

    data = request.get_json(silent=True) or {}
    specs = data.get("specs") or {}
    timeout_s = data.get("timeout_s") or None

    def generate():
        _q = queue.Queue()

        def _worker():
            def _on_progress(msg, level="info"):
                _q.put(("dsh_progress", {"message": msg, "level": level}))
            try:
                res = agent.generate_design(
                    specs, timeout_s=timeout_s, on_progress=_on_progress)
                if res.get("ok"):
                    _q.put(("dsh_result", res))
                    # M5: 生成后追加成对审查（best-effort，独立 SSE 事件 dsh_review）
                    try:
                        _q.put(("dsh_review", _dsh_run_review(res, specs)))
                    except Exception as _re:
                        _q.put(("dsh_review", {
                            "enabled": True,
                            "errors": [f"{type(_re).__name__}: {_re}"]}))
                else:
                    _q.put(("dsh_error", {
                        "error": res.get("error", "未知错误"),
                        "mode": res.get("mode", "none"),
                        "elapsed_s": res.get("elapsed_s", 0),
                        "fail_class": res.get("fail_class", ""),
                    }))
            except Exception as _e:  # 兜底: 任何异常都不让 SSE 流崩掉
                _q.put(("dsh_error", {"error": f"{type(_e).__name__}: {_e}"}))
            finally:
                _q.put(("__end__", {}))

        _sse_thread.Thread(target=_worker, daemon=True).start()

        yield f"data: {json.dumps({'type': 'dsh_start', 'message': 'DSH 方案生成已启动', 'specs_keys': sorted(list(specs.keys()))}, ensure_ascii=False, default=str)}\n\n"

        # M8/M12: 外层看门狗按 agent 声明的 CLI 尝试次数计算最坏墙钟（默认 1，不再隐含 ×2），
        # 并预留 M5 审查预算，保证 dsh_result / dsh_review 都能在截止前送达。
        _worst = float(timeout_s or agent.timeout_s) * max(
            1, int(getattr(agent, "cli_max_attempts", 1)))
        _review_budget = 180 if _DSH_REVIEW_ENABLED else 0
        _deadline = _sse_time.time() + _worst + 120 + _review_budget
        while True:
            if _sse_time.time() > _deadline:
                yield f"data: {json.dumps({'type': 'dsh_error', 'error': 'DSH 生成超时(外层看门狗)'}, ensure_ascii=False)}\n\n"
                break
            try:
                etype, payload = _q.get(timeout=1.0)
            except Exception:
                # 心跳保活, 防代理/客户端误判断流
                yield f"data: {json.dumps({'type': 'heartbeat'}, ensure_ascii=False)}\n\n"
                continue
            if etype == "__end__":
                break
            yield f"data: {json.dumps({'type': etype, **(payload or {})}, ensure_ascii=False, default=str)}\n\n"

        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no"},
    )


@app.route("/api/dsh/callback", methods=["POST"])
def api_dsh_callback():
    """DSH 异步回执入口 (供 DSH session 跑完后回调, 或外部系统投递结果)。

    Request JSON: {"task_id": str, "result": {...} | "report": str, "ok": bool}
    结果以内存 + 日志双写; 阶段一不做持久化, 避免触碰现有数据模型。
    """
    try:
        data = request.get_json(silent=True) or {}
        task_id = str(data.get("task_id") or "")
        report = data.get("report") or ""
        result = data.get("result") or {}
        ok = bool(data.get("ok", True))

        # 内存缓存 (供后续 /api/dsh/result 查询; 进程重启即失, 阶段一可接受)
        # key 只计算一次, 避免空 task_id 时前后两次时间戳不一致导致查不到
        if not hasattr(app, "_dsh_callback_store"):
            app._dsh_callback_store = {}
        _cb_key = task_id or f"anon-{int(_sse_time.time() * 1000)}"
        app._dsh_callback_store[_cb_key] = {
            "ok": ok, "report": report, "result": result,
            "received_at": _sse_time.time(),
        }

        # 映射到 MotorAI schema 并记审计日志
        try:
            import sys as _sys
            _agents_dir = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "Agents")
            if _agents_dir not in _sys.path:
                _sys.path.insert(0, _agents_dir)
            from dsh_agent import map_to_motorai_schema
            mapped = map_to_motorai_schema(report or json.dumps(result, ensure_ascii=False))
        except Exception:
            mapped = {"ok": False, "report": report, "design": {}, "design_raw": {}}

        # 把映射结果一并写入缓存, 使 /api/dsh/result 能查到结构化字段
        try:
            _store = getattr(app, "_dsh_callback_store", None)
            if _store is not None:
                _key = _cb_key
                _item = _store.get(_key)
                if _item is None:
                    _item = {"ok": ok, "report": report, "result": result,
                             "received_at": _sse_time.time()}
                    _store[_key] = _item
                _item["structured"] = bool(mapped.get("ok"))
                _item["design"] = mapped.get("design", {})
                _item["design_raw"] = mapped.get("design_raw", {})
        except Exception as _se:
            logger.warning("[dsh] callback 缓存写回失败: %s", _se)

        logger.info("[dsh] callback 收到 task_id=%s ok=%s report=%d字 结构化字段=%d",
                    task_id or "(anon)", ok, len(report), len(mapped.get("design", {})))
        return jsonify({"success": True, "task_id": task_id,
                        "structured": mapped.get("ok", False),
                        "design": mapped.get("design", {})}), 200
    except Exception as e:
        logger.warning("[dsh] callback 处理失败: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/dsh/result", methods=["GET"])
def api_dsh_result():
    """查询 DSH 异步回执结果 (按 task_id)。"""
    task_id = request.args.get("task_id", "")
    store = getattr(app, "_dsh_callback_store", {}) or {}
    if not task_id:
        return jsonify({"success": True, "count": len(store),
                        "task_ids": list(store.keys())[-20:]}), 200
    item = store.get(task_id)
    if item is None:
        return jsonify({"success": False, "error": f"task_id 不存在: {task_id}"}), 404
    return jsonify({"success": True, "task_id": task_id, **item}), 200


@app.route("/api/dsh/stats", methods=["GET"])
def api_dsh_stats():
    """M7: DSH 生成健康度统计（失败分类占比 / 几何校验通过率 / 成功口径）。

    数据来自本地 JSONL 回流（Agents/dsh_feedback_store），供运维观察各失败类别占比
    与最坏墙钟口径；纯读，不触碰现有数据模型。
    """
    try:
        tail = int(request.args.get("tail", 500))
    except (TypeError, ValueError):
        tail = 500
    try:
        import sys as _sys
        _agents_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "Agents")
        if _agents_dir not in _sys.path:
            _sys.path.insert(0, _agents_dir)
        from dsh_agent import get_feedback_stats, KB_CTX_VERSION, KB_CTX_AVAILABLE
        st = get_feedback_stats(tail)
        st.update({"success": True, "kb_version": KB_CTX_VERSION,
                   "kb_available": KB_CTX_AVAILABLE})
        return jsonify(st), 200
    except Exception as e:
        return jsonify({"success": False,
                        "error": f"{type(e).__name__}: {e}"}), 200


# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    _bind_host = Config.BIND_HOST
    _port = Config.FLASK_PORT
    _debug = Config.FLASK_DEBUG
    print("=" * 60)
    print(f"  {SYSTEM_NAME} Web Interface (Multi-Agent Edition)")
    print(f"  Version: {Config.VERSION_LABEL}  |  Date: {VERSION_DATE}")
    print(f"  ENV: {Config.ENV} ({Config.env_desc()})  |  Debug: {_debug}")
    print(f"  DB:  {os.path.basename(Config.DB_PATH)}")
    print(f"  URL: http://{_bind_host}:{_port}")
    if _bind_host != "127.0.0.1":
        _lan, _ts = _get_all_ips()
        for _ip in _lan:
            print(f"  LAN:  http://{_ip}:{_port}")
        if _ts:
            for _ip in _ts:
                print(f"  TS:   http://{_ip}:{_port}")
    print(f"  Cloud API: {'ON' if Config.ALLOW_CLOUD_API else 'OFF'}")
    print(f"  AI:       {'OK' if AI_OK else 'ERROR: ' + AI_ERR}")
    print(f"  Motor:    {'OK' if MOTOR_OK else 'ERROR'}")
    print(f"  DB:       {'OK' if DB_OK else 'ERROR'}")
    print(f"  Agents:   {'OK' if AGENT_OK else 'ERROR: ' + AGENT_ERR}")
    print(f"  Audit:    {'OK' if AUDIT_OK else 'ERROR'}")
    print(f"  Evolution:{'OK' if EVOLUTION_OK else 'ERROR'}")
    print(f"  Feedback: {'OK' if FEEDBACK_OK else 'ERROR'}")
    print(f"  TaskMgr:  {'OK' if TASK_MANAGER_OK else 'ERROR'}")
    print(f"  Auth:     {'OK' if AUTH_OK else 'ERROR'}")
    print(f"  Points:   {'OK' if POINTS_OK else 'ERROR'}")
    print("=" * 60)

    # ── 静默高频/正常请求日志 (v4.00.005 增强) ──
    # 客户端/管理端轮询、登录、注册等正常信号不应刷屏; 仅异常(4xx/5xx)保留可见
    import logging as _logging
    # GET 轮询端点: 成功(200/304)时不打印
    _polling_endpoints = (
        "/api/workflow/status", "/api/distribute/files", "/api/transfer/files",
        "/api/status", "/api/solver/status", "/api/tasks", "/api/tasks/active",
        "/api/admin/clients", "/api/admin/users", "/api/ai_team/results",
        "/api/case-motor/stats", "/api/case-motor/list", "/api/client/errors",
        "/api/client/errors/stats", "/api/points/balance",
        "/api/user/feedback/list",          # 客户端每 15s 轮询
        "/api/version/latest",              # 客户端更新检查
        "/api/admin/model-monitor/stats",   # 管理端模型监控 (每 5s 轮询)
        "/api/admin/model-monitor/trends",  # 管理端趋势图
        "/api/admin/model-monitor/degradation-events",  # 降级事件
        "/api/admin/agent-models/effective",  # 生效模型
    )
    # 正常 POST 成功(200)也不打屏: 登录风暴 / 客户端注册; 失败(401等)仍显示
    _normal_post_endpoints = (
        "/api/auth/login",
        "/api/client/register",
    )

    class _PollingFilter(_logging.Filter):
        """过滤正常/高频请求的 Werkzeug 访问日志; 异常(4xx/5xx)一律保留可见"""
        def filter(self, record):
            msg = record.getMessage()
            # favicon 缺失是常态, 无论状态码直接静默
            if "/favicon.ico" in msg:
                return False
            # 仅对成功响应(2xx/304/206)做静默; 4xx/5xx 异常必须保留
            if not (" 200 " in msg or " 200 -" in msg or " 304 " in msg or " 206 " in msg):
                return True
            # 静态资源一律静默
            if "/static/" in msg:
                return False
            # GET 轮询端点
            if "GET " in msg:
                for ep in _polling_endpoints:
                    if ep in msg:
                        return False
            # 正常 POST 成功 (登录/注册)
            if "POST " in msg:
                for ep in _normal_post_endpoints:
                    if ep in msg:
                        return False
            return True

    _logging.getLogger("werkzeug").addFilter(_PollingFilter())
    print("[log] 高频轮询日志已静默 (workflow/status, tasks, feedback/list, login, register, static 等)")

    # ── 单实例互斥守卫 (v4.00.005 增强) ──
    # 彻底防止「同一端口被两个 Flask 进程监听」这一双进程根因:
    #   1) 端口探测: 若端口已被占用, 进一步确认占用者是否为真正的 Flask 实例
    #      (对 /api/version/latest 做 HTTP 健康探测); 是则拒绝启动第二个实例 (exit 1)。
    #   2) OS 级文件锁二次保险 (Windows msvcrt / Linux fcntl): 保证同一端口全局
    #      同一时刻只有一个 app.py 持有锁, 进程退出时由操作系统自动释放, 无残留。
    def _ensure_single_instance(port: int, bind_host: str):
        import socket
        import urllib.request
        import tempfile

        _host = "127.0.0.1" if bind_host in ("0.0.0.0", "::", "") else bind_host

        # (1) 端口探测
        _s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _s.settimeout(1.5)
        try:
            _s.connect((_host, port))
            _occupied = True
        except (ConnectionRefusedError, OSError):
            _occupied = False
        finally:
            _s.close()

        if _occupied:
            # (2) 确认占用者是不是我们的 Flask
            _is_flask = False
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/version/latest", timeout=2
                ) as _r:
                    _is_flask = (_r.status == 200)
            except Exception:
                _is_flask = False
            if _is_flask:
                print(f"[FATAL] 端口 {port} 已被另一个 Flask 实例监听, "
                      f"拒绝启动重复进程 (双进程防护)。请先停止已有实例。")
            else:
                print(f"[FATAL] 端口 {port} 已被其他服务占用, "
                      f"拒绝启动 Flask 以免冲突。")
            sys.exit(1)

        # (3) OS 级文件锁二次保险 (进程退出自动释放, 不会残留死锁)
        _lock_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), f".flask_instance_{port}.lock"
        )
        try:
            _lf = open(_lock_path, "w")
            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(_lf.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    _lf.close()
                    print(f"[FATAL] 无法获取端口 {port} 实例锁, "
                          f"另一个 Flask 进程可能正在启动。")
                    sys.exit(1)
            else:
                import fcntl
                try:
                    fcntl.flock(_lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    _lf.close()
                    print(f"[FATAL] 无法获取端口 {port} 实例锁, "
                          f"另一个 Flask 进程可能正在启动。")
                    sys.exit(1)
            # 保留 _lf 引用, 使锁在整个进程生命周期内有效
            globals()["_INSTANCE_LOCK_FD"] = _lf
        except Exception as e:
            logger.warning(f"单实例文件锁获取失败 (忽略): {e}")

    _ensure_single_instance(_port, _bind_host)

    # v5.2: 启动 Ollama 显存看门狗 (后台守护线程, 周期轮询 /api/ps 的 size_vram,
    #        超阈值自动卸载非必需模型, 防 T600 4GB 显存边界死锁)
    try:
        from Core.ollama_watchdog import start_watchdog
        start_watchdog()
    except Exception as _e:
        logger.warning("[app] 显存看门狗启动失败(忽略): %s", _e)

    # ============================================================
    # DSH 插件管理蓝图（管理端 tab，W5/W6 合流）
    # ------------------------------------------------------------
    # 自包含、零侵入：失败仅告警，不影响主程序其余功能。
    # ============================================================
    try:
        _app_dir = os.path.dirname(os.path.abspath(__file__))
        if _app_dir not in sys.path:
            sys.path.insert(0, _app_dir)
        import admin_dsh
        admin_dsh.AUTH_ENFORCE = AUTH_OK
        app.register_blueprint(admin_dsh.bp)
        app.register_blueprint(admin_dsh.bp_page)
        print("[OK] DSH 插件管理蓝图已注册: /api/admin/dsh-plugins/*  +  /admin/dsh-plugins")
        # 启动每日 09:00 npm 下载量采集守护线程（方案 C 后端内置，零外部依赖）
        try:
            admin_dsh._start_npm_collect_scheduler()
        except Exception as _ce:
            print(f"[WARN] npm 下载量采集调度器启动失败（不影响主程序）: {_ce}")
    except Exception as _e:
        print(f"[WARN] DSH 插件管理蓝图加载失败（已跳过，不影响主程序）: {_e}")

    app.run(host=_bind_host, port=_port, debug=_debug, use_reloader=False, threaded=True)
