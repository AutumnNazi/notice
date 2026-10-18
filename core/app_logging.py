"""应用运行日志：把失败信息落到文件，打包后仍可查。

PyInstaller 以 --windowed 打包后没有控制台，stderr 也不可见；Python 默认的
lastResort handler 只处理 WARNING 及以上，天气、农历、定位这类失败会静默丢失。
这里统一配置根 logger：写 data/logs/notice.log，按大小轮转，目录不可用时降级。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path
from typing import Any

import config


LOG_FILE_NAME = "notice.log"
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 3
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# 未捕获异常用独立 logger 名，便于在日志里与业务模块的失败区分开
UNCAUGHT_LOGGER_NAME = "app"

# handler 名称：重复调用时靠它识别本模块安装的 handler，避免日志重复输出
FILE_HANDLER_NAME = "notice-file"
FALLBACK_HANDLER_NAME = "notice-stderr"

_installed_log_dir: Path | None = None
_original_excepthook: Any = None
_original_thread_excepthook: Any = None


def get_logger(name: str) -> logging.Logger:
    """取模块 logger，供各业务模块记录失败信息；输出目标由 setup_logging() 决定。"""
    return logging.getLogger(name)


def _remove_own_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if handler.get_name() in (FILE_HANDLER_NAME, FALLBACK_HANDLER_NAME):
            root.removeHandler(handler)
            handler.close()


def _build_file_handler(log_dir: Path) -> logging.Handler:
    """建轮转文件 handler；目录建不出来或无写权限时抛 OSError，交给调用方降级。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / LOG_FILE_NAME,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",  # 中文日志按 UTF-8 落盘，避免乱码
    )
    handler.set_name(FILE_HANDLER_NAME)
    return handler


def setup_logging(log_dir: Path | str | None = None, level: int = logging.INFO) -> logging.Logger:
    """配置根 logger 并返回它。

    重复调用是安全的：目标目录不变时复用已有 handler，日志不会成倍输出。
    日志目录无法创建或无写权限时降级为仅输出 stderr，绝不因日志问题导致程序起不来。
    """
    global _installed_log_dir

    root = logging.getLogger()
    target = Path(log_dir) if log_dir is not None else Path(config.LOG_DIR)
    if target == _installed_log_dir and any(
            handler.get_name() == FILE_HANDLER_NAME for handler in root.handlers):
        return root

    _remove_own_handlers(root)
    fallback_reason = ""
    try:
        handler: logging.Handler = _build_file_handler(target)
    except OSError as exc:
        # 例如程序装在只读目录、用户无权限：退到 stderr，保证程序照常启动
        handler = logging.StreamHandler()
        handler.set_name(FALLBACK_HANDLER_NAME)
        fallback_reason = f"{type(exc).__name__}: {exc}"
        _installed_log_dir = None
    else:
        _installed_log_dir = target

    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
    if fallback_reason:
        root.warning("日志目录 %s 不可用（%s），已降级为仅输出到 stderr", target, fallback_reason)
    return root


def install_excepthook() -> None:
    """把未捕获异常写进日志，再交回原有 hook；重复调用只包装一次。

    --windowed 打包后 stderr 不可见，单实例校验、Tk 初始化等启动阶段的崩溃
    不会留下任何痕迹，只能靠这条记录事后定位。
    """
    global _original_excepthook, _original_thread_excepthook
    if _original_excepthook is None:
        _original_excepthook = sys.excepthook

        def _log_uncaught(exc_type, exc_value, exc_tb) -> None:
            logging.getLogger(UNCAUGHT_LOGGER_NAME).critical(
                "未捕获异常导致退出", exc_info=(exc_type, exc_value, exc_tb))
            _original_excepthook(exc_type, exc_value, exc_tb)

        sys.excepthook = _log_uncaught

    if _original_thread_excepthook is not None:
        return
    _original_thread_excepthook = threading.excepthook

    def _log_thread_uncaught(args) -> None:
        # threading 不会把未捕获异常交给 sys.excepthook；正常 SystemExit 不作为崩溃记录。
        if args.exc_type is not SystemExit:
            logging.getLogger(UNCAUGHT_LOGGER_NAME).error(
                "后台线程异常：%s", args.thread.name if args.thread else "unknown",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        _original_thread_excepthook(args)

    threading.excepthook = _log_thread_uncaught
