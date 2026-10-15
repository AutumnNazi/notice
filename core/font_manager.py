from __future__ import annotations

import ctypes
from pathlib import Path

FR_PRIVATE = 0x10
HWND_BROADCAST = 0xFFFF
SMTO_ABORTIFHUNG = 0x0002
WM_FONTCHANGE = 0x001D


def register_font_directory(font_dir: Path) -> list[Path]:
    if not font_dir.exists():
        return []
    loaded: list[Path] = []
    for path in sorted(font_dir.glob("*.ttf")) + sorted(font_dir.glob("*.otf")):
        if ctypes.windll.gdi32.AddFontResourceExW(str(path), FR_PRIVATE, 0):
            loaded.append(path)
    if loaded:
        ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_FONTCHANGE, 0, 0, SMTO_ABORTIFHUNG, 1000, 0)
    return loaded


def register_font_directories(font_dirs: list[Path]) -> list[Path]:
    loaded: list[Path] = []
    for font_dir in font_dirs:
        loaded.extend(register_font_directory(font_dir))
    return loaded
