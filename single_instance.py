"""单实例守卫 —— 优先用 wake.exe（快），回退到主进程管道线程。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from config import APP_NAME

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
_listener_ready = threading.Event()

_wake_helper: subprocess.Popen | None = None


# ── 主进程内管道监听（回退方案）────────────────────────────────────────
def _listener_thread() -> None:
    while True:
        pipe = kernel32.CreateNamedPipeW(
            PIPE_NAME, PIPE_ACCESS_INBOUND,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
            1, 0, 0, 0, None,
        )
        if pipe == INVALID_HANDLE:
            _listener_ready.set()
            break
        _listener_ready.set()
        ok = kernel32.ConnectNamedPipe(pipe, None)
        if ok:
            buf = ctypes.create_string_buffer(64)
            kernel32.ReadFile(pipe, buf, 64, ctypes.byref(ctypes.wintypes.DWORD()), None)
        kernel32.CloseHandle(pipe)
        if _SHOW_CALLBACK is not None:
            _SHOW_CALLBACK()


def _send_direct() -> bool:
    """直接连接管道发送唤醒（回退方案）。"""
    _listener_ready.wait(timeout=5)
    kernel32.WaitNamedPipeW(PIPE_NAME, 3000)
    h = kernel32.CreateFileW(PIPE_NAME, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE:
        return False
    written = ctypes.wintypes.DWORD()
    kernel32.WriteFile(h, b"WAKE", 4, ctypes.byref(written), None)
    kernel32.CloseHandle(h)
    return True


# ── wake helper 管理 ─────────────────────────────────────────────────
def _wake_exe_path() -> Path | None:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent
    exe = base / "wake.exe"
    return exe if exe.exists() else None


def _start_helper_listener() -> None:
    """启动 wake.exe 常驻监听进程。"""
    global _wake_helper
    exe = _wake_exe_path()
    if exe is None:
        return
    try:
        _wake_helper = subprocess.Popen(
            [str(exe), "--listener"],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def _send_via_helper() -> bool:
    """通过 wake.exe 发送唤醒信号。"""
    exe = _wake_exe_path()
    if exe is None:
        return False
    try:
        subprocess.run([str(exe), "--wake"], timeout=5,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# ── 公开接口 ─────────────────────────────────────────────────────────
def single_instance_guard() -> None:
    kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        # 优先用 helper（快），失败则用直接管道
        if not _send_via_helper():
            _send_direct()
        sys.exit(0)
    # 首个实例：启动 helper 监听 + 主进程回退监听
    _start_helper_listener()
    threading.Thread(target=_listener_thread, daemon=True).start()


def register_show_callback(callback: Callable[[], None]) -> None:
    global _SHOW_CALLBACK
    _SHOW_CALLBACK = callback


def stop_helper() -> None:
    global _wake_helper
    if _wake_helper and _wake_helper.poll() is None:
        _wake_helper.terminate()
        _wake_helper = None
