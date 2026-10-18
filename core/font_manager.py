from __future__ import annotations

import ctypes
from pathlib import Path

from core.app_logging import get_logger

_LOGGER = get_logger(__name__)

FR_PRIVATE = 0x10
HWND_BROADCAST = 0xFFFF
SMTO_ABORTIFHUNG = 0x0002
WM_FONTCHANGE = 0x001D


def register_font_directory(font_dir: Path) -> list[Path]:
    try:
        if not font_dir.exists():
            return []
        fonts = sorted(font_dir.glob("*.ttf")) + sorted(font_dir.glob("*.otf"))
    except (OSError, ValueError) as exc:
        _LOGGER.warning("扫描字体目录 %s 失败，已跳过：%s", font_dir, exc)
        return []
    loaded: list[Path] = []
    for path in fonts:
        try:
            if ctypes.windll.gdi32.AddFontResourceExW(str(path), FR_PRIVATE, 0):
                loaded.append(path)
            else:
                _LOGGER.warning("字体文件 %s 无法注册，已跳过", path)
        except (OSError, ValueError) as exc:
            _LOGGER.warning("注册字体文件 %s 失败，已跳过：%s", path, exc)
    if loaded:
        try:
            if not ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_FONTCHANGE, 0, 0, SMTO_ABORTIFHUNG, 1000, 0):
                _LOGGER.warning("字体已注册，但广播字体更新失败：%s", font_dir)
        except OSError as exc:
            _LOGGER.warning("字体已注册，但广播字体更新失败：%s：%s", font_dir, exc)
    return loaded


def register_font_directories(font_dirs: list[Path]) -> list[Path]:
    loaded: list[Path] = []
    for font_dir in font_dirs:
        loaded.extend(register_font_directory(font_dir))
    return loaded
