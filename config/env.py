"""
env.py - Motor-AI 环境配置模块 (v1.03.01)

根据 MOTOR_ENV 环境变量加载不同的 .env 文件，实现调试版/试用版双轨分离。

使用方式:
    # 在启动脚本中设置环境变量
    set MOTOR_ENV=dev    # 或 trial

    # 在 Python 代码中
    from config.env import Config
    print(Config.FLASK_PORT)  # 5000 (dev) 或 5001 (trial)
"""
import os
from dotenv import load_dotenv

# ============================================================
# 环境检测
# ============================================================
# MOTOR_ENV 可选值: dev / trial
# 未设置时默认 dev
ENV = os.getenv("MOTOR_ENV", "dev")

# 根据 ENV 加载对应的 .env 文件
# 优先级: .env.{ENV} > .env (向后兼容)
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOTENV_PATH = os.path.join(_BASE_DIR, f".env.{ENV}")
_FALLBACK_DOTENV = os.path.join(_BASE_DIR, ".env")

if os.path.exists(_DOTENV_PATH):
    load_dotenv(_DOTENV_PATH, override=True)
elif os.path.exists(_FALLBACK_DOTENV):
    # 向后兼容: 如果没有 .env.{ENV}，回退到 .env
    load_dotenv(_FALLBACK_DOTENV, override=True)


class Config:
    """全局配置类，所有模块共享"""

    # ---- 环境 ----
    ENV = ENV  # "dev" 或 "trial"
    IS_DEV = ENV == "dev"
    IS_TRIAL = ENV == "trial"

    # ---- 系统版本 (按环境分离) ----
    # dev 使用新版本号 (2.x), trial 使用冻结版本号 (1.x)
    SYSTEM_NAME = "Motor-AI"
    SYSTEM_VERSION = "2.03.02" if IS_DEV else "2.02.02"
    VERSION_DATE = "2026-07-28 01:45"
    VERSION_LABEL = f"{SYSTEM_VERSION}-dev" if IS_DEV else f"{SYSTEM_VERSION}-trial"

    # ---- Flask ----
    FLASK_PORT = int(os.getenv("FLASK_PORT", "5000" if IS_DEV else "5001"))
    FLASK_DEBUG = IS_DEV  # 仅调试版开启热重载
    BIND_HOST = os.getenv("BIND_HOST", "127.0.0.1" if IS_DEV else "0.0.0.0")

    # ---- 数据库 ----
    # 调试版: KnowledgeBase/designs_dev.db
    # 试用版: KnowledgeBase/designs_trial.db
    DB_PATH = os.getenv(
        "DB_PATH",
        os.path.join(_BASE_DIR, "KnowledgeBase", f"designs_{ENV}.db"),
    )

    # ---- 路径 ----
    BASE_DIR = _BASE_DIR
    SCRIPTS_DIR = os.path.join(_BASE_DIR, "Scripts")
    AGENTS_DIR = os.path.join(_BASE_DIR, "Agents")
    CORE_DIR = os.path.join(_BASE_DIR, "Core")
    KNOWLEDGE_DIR = os.path.join(_BASE_DIR, "Knowledge")
    KNOWLEDGE_BASE_DIR = os.path.join(_BASE_DIR, "KnowledgeBase")
    REPORTS_DIR = os.path.join(_BASE_DIR, "Reports")
    TEMPLATES_DIR = os.path.join(_BASE_DIR, "Templates")

    # ---- 本地模型 (Ollama) ----
    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    LOCAL_MODEL_NAME = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")
    USE_GPU = os.getenv("USE_GPU", "true") == "true"

    # ---- 云端模型 (DeepSeek) ----
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-your-key-here")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    CLOUD_MODEL_NAME = os.getenv("CLOUD_MODEL_NAME", "deepseek-chat")

    # 试用版强制关闭云端 API (安全限制)
    # 即使 .env.trial 中设置了真实 key，试用版也不允许调用云端
    ALLOW_CLOUD_API = (
        IS_DEV
        and os.getenv("ALLOW_CLOUD_API", "true") == "true"
        and DEEPSEEK_API_KEY != "sk-your-key-here"
    )

    # ---- Motor-CAD ----
    MOTORCAD_EXE_PATH = os.getenv(
        "MOTORCAD_EXE_PATH", r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"
    )
    MOTORCAD_INSTALL_DIR = os.getenv(
        "MOTORCAD_INSTALL_DIR", r"D:\Motor-CAD\11_1_5"
    )
    MOTORCAD_PROGID = os.getenv("MOTORCAD_PROGID", "MotorCAD.AppAutomation")
    MOTORCAD_USE_MODE = os.getenv("MOTORCAD_USE_MODE", "auto")
    MOTORCAD_TEMPLATE_FILE = os.getenv("MOTORCAD_TEMPLATE_FILE", "Templates.mot")

    # ---- 多租户预留 (v1.03+ 未启用) ----
    CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")  # 客户端通信密钥预留
    USER_AUTH_ENABLED = False  # 用户认证暂未启用

    # ---- 环境描述 (前端展示用) ----
    @classmethod
    def env_label(cls):
        """返回环境标签文本"""
        if cls.IS_DEV:
            return "DEV"
        elif cls.IS_TRIAL:
            return "BETA"
        return cls.ENV.upper()

    @classmethod
    def env_color(cls):
        """返回环境标签颜色 (CSS)"""
        if cls.IS_DEV:
            return "#f59e0b"  # 橙色
        elif cls.IS_TRIAL:
            return "#3b82f6"  # 蓝色
        return "#6b7280"

    @classmethod
    def env_desc(cls):
        """返回环境中文描述"""
        if cls.IS_DEV:
            return "调试版"
        elif cls.IS_TRIAL:
            return "发布试用版"
        return cls.ENV

    @classmethod
    def to_dict(cls):
        """返回配置摘要 (供 API 返回)"""
        return {
            "env": cls.ENV,
            "env_label": cls.env_label(),
            "env_color": cls.env_color(),
            "env_desc": cls.env_desc(),
            "version": cls.SYSTEM_VERSION,
            "version_label": cls.VERSION_LABEL,
            "version_date": cls.VERSION_DATE,
            "flask_port": cls.FLASK_PORT,
            "flask_debug": cls.FLASK_DEBUG,
            "bind_host": cls.BIND_HOST,
            "db_path": cls.DB_PATH,
            "allow_cloud_api": cls.ALLOW_CLOUD_API,
            "user_auth_enabled": cls.USER_AUTH_ENABLED,
        }


# 模块加载时打印环境信息
if __name__ != "__main__":
    _label = Config.env_label()
    _color = Config.env_color()
    print(f"[Config] MOTOR_ENV={ENV} | Version={Config.VERSION_LABEL} | Port={Config.FLASK_PORT}")
    print(f"[Config] DB={os.path.basename(Config.DB_PATH)} | CloudAPI={'ON' if Config.ALLOW_CLOUD_API else 'OFF'} | Debug={Config.FLASK_DEBUG}")
