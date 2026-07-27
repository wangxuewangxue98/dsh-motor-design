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
import traceback
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, g

# ============================================================
# 环境配置 (v1.03.01: 从 config.env 加载)
# ============================================================
# 将项目根目录加入 sys.path 以导入 config 包
_PROJECT_ROOT = r"D:\MotorDesign"
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config.env import Config

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

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"),
    static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
)
app.config["JSON_AS_ASCII"] = False

# ============================================================
# v1.03.01: 多租户预留 — 每次请求注入 g.user (当前为 None)
# 未来引入用户认证时，只需修改此函数即可
# ============================================================
@app.before_request
def _load_user_context():
    g.user = None  # 预留: 未来从 session/token 解析用户

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
    )
except Exception as e:
    MOTOR_OK = False

DB_OK = True
try:
    from db_tools import init_db, save_result, get_all_results, get_top_solutions, export_csv, get_stats
    init_db()
except Exception as e:
    DB_OK = False

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

AUDIT_OK = True
try:
    import audit_logger
except Exception:
    AUDIT_OK = False

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
        "cloud": {"configured": False, "model": "", "allowed": Config.ALLOW_CLOUD_API},
        "db": {"available": DB_OK, "total": 0, "max_efficiency": 0, "avg_efficiency": 0, "path": Config.DB_PATH},
        "motorcad": {"available": False, "simulation_mode": True, "version": "11.1.5", "exe_path": ""},
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
        status["ollama"]["model"] = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:7b")
        status["ollama"]["gpu_mode"] = os.getenv("USE_GPU", "false").lower() == "true"
    except Exception as e:
        status["errors"].append(f"Ollama: {e}")

    # 云端 API
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if api_key and api_key != "sk-your-key-here":
        status["cloud"]["configured"] = True
        status["cloud"]["model"] = os.getenv("CLOUD_MODEL_NAME", "deepseek-chat")

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
        params = generate_param_matrix(specs, count=count)
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

    def generate():
        try:
            if MOTOR_OK:
                reset_solve_log()

            param_list = generate_param_matrix(specs, count=count)
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
                yield f"data: {json.dumps({'type': 'connecting', 'message': '正在连接 Motor-CAD COM (约 2-3 分钟, 请耐心等待)...'}, ensure_ascii=False)}\n\n"

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

            yield f"data: {json.dumps(start_data, ensure_ascii=False)}\n\n"

            # ---- 逐个求解 (复用同一 COM 连接) ----
            for i, params in enumerate(param_list):
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
                        save_result(save_params, result)

                    yield f"data: {json.dumps({'type': 'progress', 'index': i + 1, 'total': total, 'result': result}, ensure_ascii=False)}\n\n"

                except Exception as e:
                    yield f"data: {json.dumps({'type': 'error', 'index': i + 1, 'total': total, 'error': str(e)})}\n\n"

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
            }, ensure_ascii=False)}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'fatal', 'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
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
                sid = save_result(params, res)
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
        sid = save_result(save_params, res)
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
        param_list = generate_param_matrix(specs, count)
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
            sid = save_result(params, res)
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
        param_list = generate_param_matrix(specs, count)
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
    """获取所有求解结果"""
    if not DB_OK:
        return jsonify({"error": "数据库不可用"}), 500
    try:
        results = get_all_results()
        stats = get_stats()
        return jsonify({"results": results, "stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/results/top")
def api_results_top():
    """获取 Top N 方案"""
    n = int(request.args.get("n", 10))
    min_eff = float(request.args.get("min_eff", 0))
    if not DB_OK:
        return jsonify({"error": "数据库不可用"}), 500
    try:
        results = get_top_solutions(n=n, min_efficiency=min_eff)
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
# API：报告生成
# ============================================================
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
        "local_model_name": env.get("LOCAL_MODEL_NAME", "qwen2.5:7b"),
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

    write_env_file(updates)

    # 重新加载 .env 到环境变量，使 os.getenv 获取最新值
    load_dotenv(ENV_PATH, override=True)

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
# API：多智能体工作流（SSE 流式）
# ============================================================
@app.route("/api/workflow/run", methods=["POST"])
def api_workflow_run():
    """启动多智能体工作流，返回 SSE 流"""
    if not AGENT_OK or not _orchestrator:
        return jsonify({"error": f"Agent system not available: {AGENT_ERR}"}), 500

    data = request.get_json()
    raw_text = data.get("raw_text", "").strip()
    solve_count = data.get("solve_count", 10)
    use_cloud_review = data.get("use_cloud_review", False)
    use_cloud_report = data.get("use_cloud_report", False)

    if not raw_text:
        return jsonify({"error": "Please provide raw_text"}), 400

    def generate():
        try:
            yield f"data: {json.dumps({'type': 'start', 'message': 'Workflow started (辩证制衡双轨)', 'steps': ['parse_requirement', 'generate_designs', 'review_solutions', 'convergence_review', 'decision_gate', 'generate_report']}, ensure_ascii=False)}\n\n"

            for update in _orchestrator.run_streaming(
                raw_text, solve_count=solve_count,
                use_cloud_review=use_cloud_review,
                use_cloud_report=use_cloud_report
            ):
                event_type = update.get("type", "")

                # ---- step_start: 节点开始 ----
                if event_type == "step_start":
                    yield f"data: {json.dumps({'type': 'step_start', 'step': update.get('step', ''), 'message': update.get('message', '')}, ensure_ascii=False)}\n\n"

                # ---- progress: 子步骤进度 ----
                elif event_type == "progress":
                    yield f"data: {json.dumps({'type': 'progress', 'step': update.get('step', ''), 'message': update.get('message', ''), 'level': update.get('level', 'info'), 'timestamp': update.get('timestamp', '')}, ensure_ascii=False)}\n\n"

                # ---- step_done: 节点完成 ----
                elif event_type == "step_done":
                    step = update.get("step", "")

                    if step == "parse_requirement":
                        if "specs" in update:
                            yield f"data: {json.dumps({'type': 'step_done', 'step': 'parse_requirement', 'specs': update['specs'], 'confidence': update.get('confidence', 0), 'project_id': update.get('project_id', '')}, ensure_ascii=False)}\n\n"
                        elif "errors" in update:
                            yield f"data: {json.dumps({'type': 'step_error', 'step': 'parse_requirement', 'error': update['errors'][0] if update['errors'] else 'unknown'}, ensure_ascii=False)}\n\n"

                    elif step == "generate_designs":
                        if "results" in update:
                            results = update["results"]
                            yield f"data: {json.dumps({'type': 'step_done', 'step': 'generate_designs', 'results_count': len(results), 'top_results': results[:5], 'matched_templates': update.get('matched_templates', [])}, ensure_ascii=False)}\n\n"
                        elif "errors" in update:
                            yield f"data: {json.dumps({'type': 'step_error', 'step': 'generate_designs', 'error': update['errors'][0] if update['errors'] else 'unknown'}, ensure_ascii=False)}\n\n"

                    elif step == "review_solutions":
                        if "review" in update:
                            yield f"data: {json.dumps({'type': 'step_done', 'step': 'review_solutions', 'review': update['review'], 'recommendation': update.get('recommendation', ''), 'top_solutions': update.get('top_solutions', [])}, ensure_ascii=False)}\n\n"
                        elif "errors" in update:
                            yield f"data: {json.dumps({'type': 'step_error', 'step': 'review_solutions', 'error': update['errors'][0] if update['errors'] else 'unknown'}, ensure_ascii=False)}\n\n"

                    elif step == "convergence_review":
                        if "con_report" in update:
                            yield f"data: {json.dumps({'type': 'step_done', 'step': 'convergence_review', 'con_report': update['con_report'], 'convergence_findings': update.get('convergence_findings', {})}, ensure_ascii=False)}\n\n"
                        elif "errors" in update:
                            yield f"data: {json.dumps({'type': 'step_error', 'step': 'convergence_review', 'error': update['errors'][0] if update['errors'] else 'unknown'}, ensure_ascii=False)}\n\n"

                    elif step == "decision_gate":
                        yield f"data: {json.dumps({'type': 'step_done', 'step': 'decision_gate', 'pro_report': update.get('pro_report', ''), 'con_report': update.get('con_report', ''), 'ready_for_confirm': update.get('ready_for_confirm', False)}, ensure_ascii=False)}\n\n"

                    elif step == "generate_report":
                        if "report" in update:
                            yield f"data: {json.dumps({'type': 'step_done', 'step': 'generate_report', 'report': update['report'], 'report_filepath': update.get('report_filepath', ''), 'report_filename': update.get('report_filename', '')}, ensure_ascii=False)}\n\n"
                        elif "errors" in update:
                            yield f"data: {json.dumps({'type': 'step_error', 'step': 'generate_report', 'error': update['errors'][0] if update['errors'] else 'unknown'}, ensure_ascii=False)}\n\n"

                # ---- fatal: 致命错误 ----
                elif event_type == "fatal":
                    yield f"data: {json.dumps({'type': 'fatal', 'error': update.get('error', 'unknown')}, ensure_ascii=False)}\n\n"

            yield f"data: {json.dumps({'type': 'done', 'message': 'Workflow completed (双轨辩证完成)'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'fatal', 'error': str(e)}, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    """获取审计日志"""
    if not AUDIT_OK:
        return jsonify({"error": "Audit system not available"}), 500
    limit = int(request.args.get("limit", 50))
    return jsonify({"logs": audit_logger.get_recent_logs(limit)})


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
    load_dotenv(ENV_PATH, override=True)

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
    print("=" * 60)
    app.run(host=_bind_host, port=_port, debug=_debug, threaded=True)
