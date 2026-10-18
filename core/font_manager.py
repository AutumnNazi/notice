from __future__ import annotations

import ctypes
from pathlib import Path
from threading import Lock

from core.app_logging import get_logger

_LOGGER = get_logger(__name__)
_FONT_LOCK = Lock()
_registered_fonts: set[Path] = set()

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
    newly_registered = False
    for path in fonts:
        try:
            canonical = path.resolve()
            with _FONT_LOCK:
                # 目录选择与设置保存会重复导入；已有字体仍返回给选择器，不重复增加 GDI 注册。
                if canonical in _registered_fonts:
                    loaded.append(path)
                elif ctypes.windll.gdi32.AddFontResourceExW(str(canonical), FR_PRIVATE, 0):
                    _registered_fonts.add(canonical)
                    loaded.append(path)
                    newly_registered = True
                else:
                    _LOGGER.warning("字体文件 %s 无法注册，已跳过", path)
        except (OSError, ValueError) as exc:
            _LOGGER.warning("注册字体文件 %s 失败，已跳过：%s", path, exc)
    if newly_registered:
        try:
            if not ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_FONTCHANGE, 0, 0, SMTO_ABORTIFHUNG, 1000, 0):
                _LOGGER.warning("字体已注册，但广播字体更新失败：%s", font_dir)
        except OSError as exc:
            _LOGGER.warning("字体已注册，但广播字体更新失败：%s：%s", font_dir, exc)
    return loaded


def register_font_directories(font_dirs: list[Path]) -> list[Path]:
    loaded: list[Path] = []
    seen: set[Path] = set()
    for font_dir in font_dirs:
        for path in register_font_directory(font_dir):
            canonical = path.resolve()
            if canonical not in seen:
                seen.add(canonical)
                loaded.append(path)
    return loaded
