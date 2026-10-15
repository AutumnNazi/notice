"""轻量唤醒助手 —— 双模式：listener（后台中继）/ wake（发信号后退出）。"""

import ctypes
import ctypes.wintypes
import sys

MUTEX_NAME = "Global\\NoticeHelper_SingleInstance"
PIPE_NAME = r"\\.\pipe\NoticeHelper_WakeUp"

kernel32 = ctypes.windll.kernel32

# ── 函数签名 ─────────────────────────────────────────────────────────
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


def _send_to_pipe(data: bytes) -> bool:
    kernel32.WaitNamedPipeW(PIPE_NAME, 3000)
    h = kernel32.CreateFileW(PIPE_NAME, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE:
        return False
    written = ctypes.wintypes.DWORD()
    kernel32.WriteFile(h, data, len(data), ctypes.byref(written), None)
    kernel32.CloseHandle(h)
    return True


# ── listener 模式：收到唤醒信号后转发给主进程管道 ─────────────────────
def _run_listener() -> None:
    """常驻进程：监听第二个实例的连接，转发 WAKE 给主进程。"""
    while True:
        pipe = kernel32.CreateNamedPipeW(
            PIPE_NAME, PIPE_ACCESS_INBOUND,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
            1, 0, 0, 0, None,
        )
        if pipe == INVALID_HANDLE:
            break
        kernel32.ConnectNamedPipe(pipe, None)
        buf = ctypes.create_string_buffer(64)
        kernel32.ReadFile(pipe, buf, 64, ctypes.byref(ctypes.wintypes.DWORD()), None)
        kernel32.CloseHandle(pipe)
        # 不直接操作窗口，转发给主进程的回退监听线程
        _send_to_pipe(b"WAKE")


# ── wake 模式：发送信号后退出 ─────────────────────────────────────────
def _run_wake() -> None:
    _send_to_pipe(b"WAKE")


# ── 入口 ─────────────────────────────────────────────────────────────
def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "--listener":
        _run_listener()
    elif len(sys.argv) >= 2 and sys.argv[1] == "--wake":
        _run_wake()
    else:
        print("用法:")
        print("  wake.exe --listener   后台监听模式")
        print("  wake.exe --wake       发送唤醒信号")
        sys.exit(1)


if __name__ == "__main__":
    main()
