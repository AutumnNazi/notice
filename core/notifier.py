from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

import pythoncom
from win32com.client import Dispatch
from win32com.propsys import propsys, pscon

from config import APP_ID, APP_NAME, ICON_ICO_FILE, SHORTCUT_FILE, START_MENU_DIR, TOAST_IMAGE_FILE
from core.icon_assets import ensure_icon_assets
from core.app_logging import get_logger
from core.models import Task, validate_notification_text

_LOGGER = get_logger(__name__)
POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
NOTIFICATION_TIMEOUT_SECONDS = 15


class Notifier:
    def __init__(self) -> None:
        ensure_icon_assets()
        self._ensure_start_menu_shortcut()

    def send(self, task: Task) -> None:
        validate_notification_text(task.title, task.body)
        # 调度器使用工作线程，COM 必须在实际调用线程初始化并配对释放。
        pythoncom.CoInitialize()
        try:
            self._ensure_start_menu_shortcut()
            toast = Element("toast", duration="long" if task.priority == "high" else "short")
            binding = SubElement(SubElement(toast, "visual"), "binding", template="ToastImageAndText02")
            SubElement(binding, "image", id="1", src=self._toast_image())
            SubElement(binding, "text", id="1").text = task.title
            SubElement(binding, "text", id="2").text = task.body or APP_NAME
            SubElement(toast, "audio", silent="true")
            # XML 编码后只作为数据解码；标题、正文和路径不能参与 PowerShell 语法解析。
            xml_data = base64.b64encode(tostring(toast, encoding="utf-8")).decode("ascii")
            app_id = APP_ID.replace("'", "''")
            tag = task.id[:16].replace("'", "''")
            script = f"""
$ErrorActionPreference = 'Stop';
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;
$xml = [Windows.Data.Xml.Dom.XmlDocument]::new();
$xml.LoadXml([System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([Console]::In.ReadToEnd())));
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);
$toast.Tag = '{tag}';
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}').Show($toast);
"""
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            subprocess.run(
                [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                check=True, capture_output=True, encoding="utf-8", errors="replace",
                input=xml_data,
                timeout=NOTIFICATION_TIMEOUT_SECONDS, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _LOGGER.error("提醒 %s 发送失败：%s；系统输出=%s", task.id, exc, getattr(exc, "stderr", ""))
            raise
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
