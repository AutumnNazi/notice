from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from PIL import Image

from config import APP_NAME, ICON_FILE, ICON_ICO_FILE, TOAST_IMAGE_FILE
from core.icon_assets import ensure_icon_assets

try:
    import pystray
except ImportError:  # pragma: no cover - depends on local environment
    pystray = None


class TrayController:
    def __init__(
        self,
        on_open: Callable[[], None],
        on_add_task: Callable[[], None],
        on_open_settings: Callable[[], None],
        on_pause_toggle: Callable[[], None],
        is_paused: Callable[[], bool],
        on_exit: Callable[[], None],
    ) -> None:
        self.on_open = on_open
        self.on_add_task = on_add_task
        self.on_open_settings = on_open_settings
        self.on_pause_toggle = on_pause_toggle
        self.is_paused = is_paused
        self.on_exit = on_exit
        self.icon = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if pystray is None or self._thread:
            return
        image = self._load_icon()
        self.icon = pystray.Icon(APP_NAME, image, APP_NAME, self._build_menu())
        self._thread = threading.Thread(target=self.icon.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.icon:
            self.icon.stop()
            self.icon = None
            self._thread = None

    def refresh(self) -> None:
        if self.icon:
            self.icon.menu = self._build_menu()
            self.icon.update_menu()

    def _build_menu(self):
        if pystray is None:
            return None
        pause_label = "恢复全部提醒" if self.is_paused() else "暂停全部提醒"
        return pystray.Menu(
            pystray.MenuItem("显示主界面", lambda icon, item: self.on_open(), default=True, visible=False),
            pystray.MenuItem("新建提醒", lambda icon, item: self.on_add_task()),
            pystray.MenuItem("打开设置", lambda icon, item: self.on_open_settings()),
            pystray.MenuItem(pause_label, lambda icon, item: self.on_pause_toggle()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出时辰筏", lambda icon, item: self.on_exit()),
        )

    def _load_icon(self):
        ensure_icon_assets()
        return Image.open(Path(ICON_FILE))
