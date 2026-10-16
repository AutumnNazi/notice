from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from config import DATA_DIR, SETTINGS_DEFAULTS, SETTINGS_FILE, TASKS_FILE
from core.models import Task


class TaskStore:
    def __init__(self, path: Path = TASKS_FILE) -> None:
        self.path = path
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def load_tasks(self) -> list[Task]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        # 逐条读取：一条坏记录不能让整批提醒都读不出来，更不能让窗口化运行的程序启动即崩。
        # 坏记录由 Task.from_dict(strict=False) 降级成“已停用”，这里只负责汇总并把数量与
        # 原因写到 stderr —— 窗口化运行时看不到 stderr，但必须留下可查的痕迹。
        tasks: list[Task] = []
        problems: list[str] = []
        for index, item in enumerate(data.get("tasks", [])):
            if not isinstance(item, dict):
                # 连提醒对象都不是（被写成了字符串/数字等），既没法展示也没法编辑，只能跳过。
                problems.append(f"第 {index + 1} 条不是提醒对象，已跳过：{item!r}")
                continue
            task = Task.from_dict(item, strict=False)
            tasks.append(task)
            if task.load_warning:
                problems.append(f"第 {index + 1} 条（{task.id}）：{task.load_warning}")
        if problems:
            print(f"[提醒数据] {len(problems)} 条记录读取异常，已降级为停用或跳过：", file=sys.stderr)
            for message in problems:
                print(f"  - {message}", file=sys.stderr)
        return tasks

    def save_tasks(self, tasks: list[Task]) -> None:
        payload = {"tasks": [task.to_dict() for task in tasks]}
        self._save_json(self.path, payload)

    def load_settings(self, path: Path = SETTINGS_FILE) -> dict[str, object]:
        if not path.exists():
            return SETTINGS_DEFAULTS.copy()
        with path.open("r", encoding="utf-8-sig") as handle:
            return SETTINGS_DEFAULTS | json.load(handle)

    def save_settings(self, settings: dict[str, object], path: Path = SETTINGS_FILE) -> None:
        self._save_json(path, settings)

    def _save_json(self, path: Path, payload: dict) -> None:
        # 完整写入并落盘后才替换原文件；失败时旧提醒和设置仍然可读。
        text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.read_bytes() if path.exists() else b""
        encoding = "utf-8-sig" if previous.startswith(b"\xef\xbb\xbf") else "utf-8"
        newline = "\r\n" if b"\r\n" in previous else "\n"
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding=encoding, newline=newline,
                                    dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
                                    delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
