from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pythoncom
from win32com.client import Dispatch
from win32com.propsys import propsys, pscon

from config import APP_ID, APP_NAME, ICON_ICO_FILE, SHORTCUT_FILE, START_MENU_DIR, TOAST_IMAGE_FILE
from core.icon_assets import ensure_icon_assets
from core.models import Task

try:
    from winotify import Notification
except ImportError:  # pragma: no cover - depends on local environment
    Notification = None


class Notifier:
    def __init__(self) -> None:
        ensure_icon_assets()
        self._ensure_start_menu_shortcut()

    def send(self, task: Task) -> None:
        if Notification is None:
            return

        # 调度器使用工作线程，COM 必须在实际调用线程初始化并配对释放。
        pythoncom.CoInitialize()
        try:
            self._ensure_start_menu_shortcut()
            toast = Notification(
                app_id=APP_ID,
                title=task.title,
                msg=task.body or APP_NAME,
                icon=self._toast_image(),
                duration="long" if task.priority == "high" else "short",
            )
            toast.show()
        finally:
            pythoncom.CoUninitialize()

    def _ensure_start_menu_shortcut(self) -> None:
        START_MENU_DIR.mkdir(parents=True, exist_ok=True)
        self._create_shortcut()

        try:
            store = propsys.SHGetPropertyStoreFromParsingName(str(SHORTCUT_FILE), None, 2, propsys.IID_IPropertyStore)
            current_value = store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
            if current_value != APP_ID:
                store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
                store.Commit()
        except Exception:
            # Recreate once if shortcut metadata is broken.
            self._create_shortcut()
            store = propsys.SHGetPropertyStoreFromParsingName(str(SHORTCUT_FILE), None, 2, propsys.IID_IPropertyStore)
            store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
            store.Commit()

    def _create_shortcut(self) -> None:
        shell = Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(str(SHORTCUT_FILE))
        target, arguments, working_dir = self._launch_command()
        shortcut.TargetPath = target
        shortcut.Arguments = arguments
        shortcut.WorkingDirectory = working_dir
        shortcut.Description = APP_NAME
        icon = self._notification_icon()
        if icon:
            shortcut.IconLocation = f"{icon},0"
        shortcut.Save()

    def _launch_command(self) -> tuple[str, str, str]:
        if getattr(sys, "frozen", False):
            executable = Path(sys.executable).resolve()
            return str(executable), "", str(executable.parent)

        pythonw = Path(sys.executable).with_name("pythonw.exe")
        interpreter = pythonw if pythonw.exists() else Path(sys.executable)
        script = Path(__file__).resolve().parents[1] / "main.py"
        arguments = f'"{script}"'
        return str(interpreter), arguments, str(script.parent)

    def _notification_icon(self) -> str:
        if ICON_ICO_FILE.exists():
            return str(ICON_ICO_FILE.resolve())
        if getattr(sys, "frozen", False):
            return str(Path(sys.executable).resolve())
        return ""

    def _toast_image(self) -> str:
        if TOAST_IMAGE_FILE.exists():
            return str(TOAST_IMAGE_FILE.resolve())
        return ""
