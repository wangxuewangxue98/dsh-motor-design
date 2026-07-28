# -*- coding: utf-8 -*-
"""
template_manager.py - Motor-CAD .mot 模板自动管理

功能:
  1. 检测 D:\\MotorDesign\\Templates 目录下的 .mot 模板
  2. 如果模板缺失, 自动从 Motor-CAD 安装目录搜索 .mot 文件并复制
  3. 如果搜索不到, 使用已知模板 (Templates.mot / 160kw_assm_2222.mot) 复制
  4. 提供模板信息查询接口

Motor-CAD v11.1.5 不自带大量模板, 通常需要用户手动创建。
本模块确保至少有一个可用的 .mot 模板文件供 COM 自动化使用。
"""
import os
import shutil
import logging
from datetime import datetime
from typing import List, Dict, Optional

logger = logging.getLogger("template_manager")

# ============================================================
#  路径配置
# ============================================================
BASE_DIR = r"D:\MotorDesign"
TEMPLATE_DIR = os.path.join(BASE_DIR, "Templates")
MOTORCAD_INSTALL_DIR = os.getenv(
    "MOTORCAD_INSTALL_DIR", r"D:\Motor-CAD\11_1_5"
)

# 已知模板文件 (优先级从高到低)
KNOWN_TEMPLATES = [
    "Templates.mot",
    "160kw_assm_2222.mot",
]

# Motor-CAD 安装目录中可能包含 .mot 的子目录
SEARCH_SUBDIRS = [
    "",  # 安装根目录
    "ActiveX_Automation_in_Motor-CAD",
    "templates",
    "Templates",
    "examples",
    "Examples",
    "demo",
    "Demo",
    "data",
    "Data",
    "motors",
    "Motor",
]


def ensure_template_dir() -> str:
    """确保模板目录存在, 返回路径"""
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    return TEMPLATE_DIR


def list_templates() -> List[Dict]:
    """
    列出模板目录中所有 .mot 文件
    返回: [{name, path, size_kb, modified, is_default}, ...]
    """
    ensure_template_dir()
    templates = []
    if not os.path.isdir(TEMPLATE_DIR):
        return templates

    for fname in sorted(os.listdir(TEMPLATE_DIR)):
        if fname.lower().endswith(".mot"):
            fpath = os.path.join(TEMPLATE_DIR, fname)
            if os.path.isfile(fpath):
                stat = os.stat(fpath)
                templates.append({
                    "name": fname,
                    "path": fpath,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "is_default": fname in KNOWN_TEMPLATES,
                })

    return templates


def get_default_template() -> Optional[str]:
    """获取默认模板文件路径, 不存在返回 None"""
    ensure_template_dir()

    # 按优先级查找已知模板
    for name in KNOWN_TEMPLATES:
        fpath = os.path.join(TEMPLATE_DIR, name)
        if os.path.exists(fpath):
            return fpath

    # 降级: 目录中任意 .mot 文件
    if os.path.isdir(TEMPLATE_DIR):
        for fname in os.listdir(TEMPLATE_DIR):
            if fname.lower().endswith(".mot"):
                return os.path.join(TEMPLATE_DIR, fname)

    return None


def search_mot_files(search_dir: str, max_depth: int = 2) -> List[str]:
    """
    在指定目录中搜索 .mot 文件 (递归, 限制深度)
    返回找到的 .mot 文件路径列表
    """
    found = []
    if not os.path.isdir(search_dir):
        return found

    try:
        for root, dirs, files in os.walk(search_dir):
            # 限制搜索深度
            depth = root[len(search_dir):].count(os.sep)
            if depth >= max_depth:
                dirs.clear()  # 不再深入
                continue

            for f in files:
                if f.lower().endswith(".mot"):
                    found.append(os.path.join(root, f))

            if len(found) >= 20:  # 限制结果数量
                break
    except Exception as e:
        logger.warning(f"搜索 .mot 文件失败 ({search_dir}): {e}")

    return found


def auto_setup_templates() -> Dict:
    """
    自动设置模板: 确保模板目录存在且有可用 .mot 文件
    如果模板缺失, 从 Motor-CAD 安装目录搜索并复制

    返回: {
        "template_dir": str,
        "templates": [...],
        "default_template": str or None,
        "actions": [str],  # 执行的操作日志
        "created": bool,   # 是否新建了模板
    }
    """
    actions = []
    template_dir = ensure_template_dir()
    actions.append(f"模板目录: {template_dir}")

    # 检查现有模板
    existing = list_templates()
    if existing:
        actions.append(f"已有 {len(existing)} 个模板: {[t['name'] for t in existing]}")
        default = get_default_template()
        return {
            "template_dir": template_dir,
            "templates": existing,
            "default_template": default,
            "actions": actions,
            "created": False,
        }

    # 没有模板, 需要创建/复制
    actions.append("模板目录为空, 开始搜索 .mot 文件...")

    # 1. 在 D:\MotorDesign 下搜索
    found = search_mot_files(BASE_DIR, max_depth=1)
    actions.append(f"D:\\MotorDesign 下找到 {len(found)} 个 .mot 文件")

    # 2. 在 Motor-CAD 安装目录搜索
    if not found and os.path.isdir(MOTORCAD_INSTALL_DIR):
        for subdir in SEARCH_SUBDIRS:
            search_path = os.path.join(MOTORCAD_INSTALL_DIR, subdir)
            if os.path.isdir(search_path):
                found = search_mot_files(search_path, max_depth=2)
                if found:
                    actions.append(f"在 {subdir or '根目录'} 下找到 {len(found)} 个 .mot 文件")
                    break

    # 3. 在安装目录根搜索 (兜底)
    if not found and os.path.isdir(MOTORCAD_INSTALL_DIR):
        found = search_mot_files(MOTORCAD_INSTALL_DIR, max_depth=3)
        actions.append(f"安装目录全搜索找到 {len(found)} 个 .mot 文件")

    if found:
        # 复制第一个找到的 .mot 文件作为模板
        src = found[0]
        dst = os.path.join(template_dir, "Templates.mot")
        try:
            shutil.copy2(src, dst)
            actions.append(f"已复制: {src} -> {dst}")
        except Exception as e:
            actions.append(f"复制失败: {e}")
            # 尝试其他文件
            for f in found[1:]:
                try:
                    shutil.copy2(f, dst)
                    actions.append(f"已复制: {f} -> {dst}")
                    break
                except Exception:
                    continue
    else:
        actions.append("未找到任何 .mot 文件!")
        actions.append("请在 Motor-CAD GUI 中 File -> New 创建新电机, Save As .mot 文件")
        actions.append(f"保存到: {template_dir}")

    # 重新检查
    templates = list_templates()
    default = get_default_template()

    return {
        "template_dir": template_dir,
        "templates": templates,
        "default_template": default,
        "actions": actions,
        "created": len(templates) > 0,
    }


def get_template_info() -> Dict:
    """
    获取模板状态信息 (供 Web UI 显示)
    """
    templates = list_templates()
    default = get_default_template()

    return {
        "template_dir": TEMPLATE_DIR,
        "template_exists": default is not None,
        "template_path": default or "",
        "templates": templates,
        "template_count": len(templates),
    }


if __name__ == "__main__":
    # 命令行测试
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("=" * 60)
    print("Motor-CAD 模板管理器")
    print("=" * 60)

    result = auto_setup_templates()
    print(f"\n模板目录: {result['template_dir']}")
    print(f"默认模板: {result['default_template']}")
    print(f"模板数量: {len(result['templates'])}")
    print("\n操作日志:")
    for a in result["actions"]:
        print(f"  - {a}")

    if result["templates"]:
        print("\n模板列表:")
        for t in result["templates"]:
            tag = " [默认]" if t["is_default"] else ""
            print(f"  {t['name']}{tag} ({t['size_kb']} KB, {t['modified']})")
    else:
        print("\n[WARN] 没有可用模板!")
        print("请在 Motor-CAD GUI 中创建:")
        print("  1. 启动 Motor-CAD")
        print("  2. File -> New 选择电机类型")
        print("  3. 设置基本参数")
        print("  4. File -> Save As -> D:\\MotorDesign\\Templates\\Templates.mot")
