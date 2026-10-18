"""轻量唤醒助手：只发送信号，由主进程统一监听。"""

import ctypes
import ctypes.wintypes
import sys

PIPE_NAME = r"\\.\pipe\NoticeHelper_WakeUp"

kernel32 = ctypes.windll.kernel32

# ── 函数签名 ─────────────────────────────────────────────────────────
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

INVALID_HANDLE = ctypes.wintypes.HANDLE(-1).value
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3


def _send_to_pipe(data: bytes) -> bool:
    if not kernel32.WaitNamedPipeW(PIPE_NAME, 3000):
        return False
    h = kernel32.CreateFileW(PIPE_NAME, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE:
        return False
    try:
        written = ctypes.wintypes.DWORD()
        return bool(kernel32.WriteFile(h, data, len(data), ctypes.byref(written), None)) and written.value == len(data)
    finally:
        kernel32.CloseHandle(h)


# ── wake 模式：发送信号后退出 ─────────────────────────────────────────
def _run_wake() -> None:
    sys.exit(0 if _send_to_pipe(b"WAKE") else 1)


# ── 入口 ─────────────────────────────────────────────────────────────
def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "--wake":
        _run_wake()
    else:
        print("用法:")
        print("  wake.exe --wake       发送唤醒信号")
        sys.exit(1)


if __name__ == "__main__":
    main()
