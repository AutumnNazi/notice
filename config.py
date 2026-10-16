from __future__ import annotations

from pathlib import Path
import os
import sys


APP_NAME = "时辰筏"
APP_ID = "ShiChenFa.Desktop.Leaf"
DEFAULT_TITLE = "时辰有讯"

# 数据目录放在 exe / 脚本同级的 data 文件夹下
if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).resolve().parent
else:
    _BASE = Path(__file__).resolve().parent
DATA_DIR = _BASE / "data"
# 运行日志目录：--windowed 打包后没有控制台，出问题只能翻这里的文件
LOG_DIR = DATA_DIR / "logs"
TASKS_FILE = DATA_DIR / "tasks.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
ICON_FILE = DATA_DIR / "tray_icon.png"
ICON_ICO_FILE = DATA_DIR / "app_icon.ico"
ICON_PNG_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
ICON_PNG_FILES = tuple(DATA_DIR / f"app_icon_{size}.png" for size in ICON_PNG_SIZES)
TOAST_IMAGE_FILE = DATA_DIR / "toast_image.png"
BOAT_ART_FILE = DATA_DIR / "boat_art.png"
FONT_DIR = _BASE / "fonts"
SETTINGS_DEFAULTS = {
    "font_family": "",
    "custom_font_dir": "",
    "close_action": "ask",
}
START_MENU_DIR = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
SHORTCUT_FILE = START_MENU_DIR / f"{APP_NAME}.lnk"

TRIGGER_TYPES = {
    "once": "单次定时",
    "interval": "循环间隔",
    "weekly": "按星期触发",
    "weekly_last_workday": "每周最后一个工作日",
    "yearly": "按月日触发",
    "cron": "Cron 表达式",
}

WEEKDAY_LABELS = [
    ("周一", 0),
    ("周二", 1),
    ("周三", 2),
    ("周四", 3),
    ("周五", 4),
    ("周六", 5),
    ("周日", 6),
]
