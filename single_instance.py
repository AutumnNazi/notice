"""单实例守卫：主进程单独监听，wake.exe 仅负责发送唤醒信号。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from core.app_logging import get_logger

_LOGGER = get_logger(__name__)

MUTEX_NAME = "Global\\NoticeHelper_SingleInstance"
PIPE_NAME = r"\\.\pipe\NoticeHelper_WakeUp"

kernel32 = ctypes.windll.kernel32

kernel32.CreateMutexW.argtypes = [ctypes.wintypes.LPVOID, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
kernel32.GetLastError.restype = ctypes.wintypes.DWORD
kernel32.CreateNamedPipeW.argtypes = [
    ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
    ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.LPVOID,
]
kernel32.CreateNamedPipeW.restype = ctypes.wintypes.HANDLE
kernel32.ConnectNamedPipe.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.LPVOID]
kernel32.ConnectNamedPipe.restype = ctypes.wintypes.BOOL
kernel32.ReadFile.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.wintypes.LPVOID, ctypes.wintypes.DWORD,
    ctypes.POINTER(ctypes.wintypes.DWORD), ctypes.wintypes.LPVOID,
]
kernel32.ReadFile.restype = ctypes.wintypes.BOOL
kernel32.CreateFileW.argtypes = [
    ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
    ctypes.wintypes.LPVOID, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.HANDLE,
]
kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
kernel32.WriteFile.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.wintypes.LPVOID, ctypes.wintypes.DWORD,
    ctypes.POINTER(ctypes.wintypes.DWORD), ctypes.wintypes.LPVOID,
]
kernel32.WriteFile.restype = ctypes.wintypes.BOOL
kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
kernel32.WaitNamedPipeW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD]
kernel32.WaitNamedPipeW.restype = ctypes.wintypes.BOOL

ERROR_ALREADY_EXISTS = 183
INVALID_HANDLE = ctypes.wintypes.HANDLE(-1).value
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
PIPE_ACCESS_INBOUND = 0x00000001
PIPE_TYPE_MESSAGE = 0x00000004
PIPE_READMODE_MESSAGE = 0x00000002
PIPE_WAIT = 0x00000000

_SHOW_CALLBACK: Callable[[], None] | None = None
_callback_lock = threading.Lock()
_pending_wake = False
_listener_ready = threading.Event()

_mutex_handle = None
_listener_stop = threading.Event()


# ── 主进程内管道监听（回退方案）────────────────────────────────────────
def _listener_thread() -> None:
    global _pending_wake
    while not _listener_stop.is_set():
        pipe = kernel32.CreateNamedPipeW(
            PIPE_NAME, PIPE_ACCESS_INBOUND,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
            1, 0, 0, 0, None,
        )
        if pipe == INVALID_HANDLE:
            _LOGGER.error("创建唤醒管道失败：WinError=%s", kernel32.GetLastError())
            _listener_ready.set()
            break
        _listener_ready.set()
        try:
            ok = kernel32.ConnectNamedPipe(pipe, None)
            # 客户端先于 ConnectNamedPipe 连上时，ERROR_PIPE_CONNECTED 同样表示可读。
            if ok or kernel32.GetLastError() == 535:
                buf = ctypes.create_string_buffer(64)
                read = ctypes.wintypes.DWORD()
                received = kernel32.ReadFile(pipe, buf, 64, ctypes.byref(read), None)
                if received and buf.raw[:read.value] == b"WAKE" and not _listener_stop.is_set():
                    with _callback_lock:
                        callback = _SHOW_CALLBACK
                        if callback is None:
                            _pending_wake = True
                    if callback is not None:
                        callback()
        except Exception:
            _LOGGER.exception("处理唤醒请求失败")
        finally:
            kernel32.CloseHandle(pipe)


def _send_direct() -> bool:
    """直接连接管道发送唤醒（回退方案）。"""
    if not kernel32.WaitNamedPipeW(PIPE_NAME, 3000):
        return False
    h = kernel32.CreateFileW(PIPE_NAME, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE:
        return False
    try:
        written = ctypes.wintypes.DWORD()
        return bool(kernel32.WriteFile(h, b"WAKE", 4, ctypes.byref(written), None)) and written.value == 4
    finally:
        kernel32.CloseHandle(h)


# ── wake helper 管理 ─────────────────────────────────────────────────
def _wake_exe_path() -> Path | None:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent
    exe = base / "wake.exe"
    return exe if exe.exists() else None


def _send_via_helper() -> bool:
    """通过 wake.exe 发送唤醒信号。"""
    exe = _wake_exe_path()
    if exe is None:
        return False
    try:
        result = subprocess.run([str(exe), "--wake"], timeout=5,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        _LOGGER.warning("唤醒助手失败，改用直接管道", exc_info=True)
        return False


# ── 公开接口 ─────────────────────────────────────────────────────────
def single_instance_guard() -> None:
    global _mutex_handle
    _mutex_handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not _mutex_handle:
        raise ctypes.WinError(kernel32.GetLastError())
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        # 优先用 helper（快），失败则用直接管道
        sent = _send_via_helper() or _send_direct()
        kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None
        if not sent:
            _LOGGER.error("已有实例，但唤醒信号发送失败")
        sys.exit(0 if sent else 1)
    _listener_stop.clear()
    threading.Thread(target=_listener_thread, daemon=True).start()


def register_show_callback(callback: Callable[[], None]) -> None:
    global _SHOW_CALLBACK, _pending_wake
    # 初始化期间收到的唤醒合并成一次，窗口就绪后再执行，回调本身不占用锁。
    with _callback_lock:
        _SHOW_CALLBACK = callback
        pending = _pending_wake
        _pending_wake = False
    if pending and not _listener_stop.is_set():
        callback()


def stop_helper() -> None:
    _listener_stop.set()
    _send_direct()
    # 互斥句柄留到进程退出由 Windows 释放，避免退出清理期间第二实例抢先启动。
