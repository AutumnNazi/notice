from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from config import DATA_DIR, SETTINGS_DEFAULTS, SETTINGS_FILE, TASKS_FILE
from core.app_logging import get_logger
from core.models import Task


_LOGGER = get_logger(__name__)


def _finite_json_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"JSON 数值必须有限：{value}")
    return number


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 对象包含重复字段：{key!r}")
        result[key] = value
    return result


def _validate_json_text(data: object) -> None:
    pending = [data]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            value.encode("utf-8")
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


class TaskStore:
    def __init__(self, path: Path = TASKS_FILE) -> None:
        self.path = path
        self._unprotected_paths: set[Path] = set()
        self._unread_paths: set[Path] = set()
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def load_tasks(self) -> list[Task]:
        if not self.path.exists():
            self._unread_paths.discard(self.path)
            return []
        data = self._read_json(self.path, "提醒数据")
        if data is None:
            return []
        records = data.get("tasks")
        if not isinstance(records, list):
            # 结构不对时整份文件都不可信，旁边任何一条都可能带着错误类型；整体降级为空列表，
            # 原件留档不删，等用户手工修或由下一次正常保存覆盖。
            self._quarantine(
                self.path, "提醒数据",
                '缺少 "tasks" 数组' if "tasks" not in data
                else f'"tasks" 字段应为数组，实际是 {type(records).__name__}',
            )
            return []
        # 逐条读取：一条坏记录不能让整批提醒都读不出来，更不能让窗口化运行的程序启动即崩。
        # 坏记录由 Task.from_dict(strict=False) 降级成“已停用”，这里只负责汇总并把数量与
        # 原因写入运行日志，窗口化运行时也能追查。
        tasks: list[Task] = []
        problems: list[str] = []
        reserved_ids = {str(item["id"]) for item in records if isinstance(item, dict) and "id" in item}
        seen_ids: set[str] = set()
        for index, item in enumerate(records):
            if not isinstance(item, dict):
                # 连提醒对象都不是（被写成了字符串/数字等），既没法展示也没法编辑，只能跳过。
                problems.append(f"第 {index + 1} 条不是提醒对象，已跳过：{item!r}")
                continue
            task = Task.from_dict(item, strict=False)
            if task.id in seen_ids or not task.id or task.id.startswith("snooze:"):
                original_id = task.id
                task.id = Task().id
                while task.id in reserved_ids:
                    task.id = Task().id
                reserved_ids.add(task.id)
                task.enabled = False
                task.next_run = None
                reason = f"提醒 ID 重复或不可用（原值 {original_id!r}），已分配新 ID 并停用"
                task.load_warning = f"{task.load_warning}；{reason}" if task.load_warning else reason
            seen_ids.add(task.id)
            tasks.append(task)
            if task.load_warning:
                problems.append(f"第 {index + 1} 条（{task.id}）：{task.load_warning}")
        if problems:
            _LOGGER.warning("[提醒数据] %s 条记录读取异常，已降级为停用或跳过：", len(problems))
            for message in problems:
                _LOGGER.warning("  - %s", message)
            self._quarantine(self.path, "提醒数据", "存在异常条目，保存容错结果前保留完整原文")
        return tasks

    def save_tasks(self, tasks: list[Task]) -> None:
        payload = {"tasks": [task.to_dict() for task in tasks]}
        self._save_json(self.path, payload)

    def load_settings(self, path: Path = SETTINGS_FILE) -> dict[str, object]:
        if not path.exists():
            self._unread_paths.discard(path)
            return SETTINGS_DEFAULTS.copy()
        data = self._read_json(path, "提醒设置")
        if data is None:
            return SETTINGS_DEFAULTS.copy()
        return SETTINGS_DEFAULTS | data

    def save_settings(self, settings: dict[str, object], path: Path = SETTINGS_FILE) -> None:
        self._save_json(path, settings)

    def _read_json(self, path: Path, label: str) -> dict | None:
        """读 JSON；文件级损坏时返回 None，由调用方回退到安全值。

        窗口化打包后没有控制台，每次降级都要在运行日志中写清路径与原因，
        不做静默兜底。
        """
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle, parse_float=_finite_json_number, parse_constant=_finite_json_number,
                                 object_pairs_hook=_unique_json_object)
            # JSON 允许转义孤立代理字符，但它无法用 UTF-8 写回；读取边界先拒绝并留档。
            _validate_json_text(data)
        except UnicodeDecodeError as error:
            self._unread_paths.discard(path)
            self._quarantine(path, label, f"UTF-8 解码失败：{error}")
            return None
        except ValueError as error:
            self._unread_paths.discard(path)
            self._quarantine(path, label, f"JSON 解析失败：{error}")
            return None
        except OSError as error:
            # 文件被占用或权限不足时连备份都读不出来，这里只提示、不改动也无法改动原文件。
            _LOGGER.warning("[%s] 读取 %s 失败，本次已改用安全默认值：%s", label, path, error)
            self._unread_paths.add(path)
            return None
        self._unread_paths.discard(path)
        if not isinstance(data, dict):
            self._quarantine(path, label, f"顶层应为 JSON 对象，实际是 {type(data).__name__}")
            return None
        return data

    def _quarantine(self, path: Path, label: str, reason: str) -> bool:
        """损坏文件只复制留档、绝不删改，用户可能想手工修回里面的内容。

        下一次正常保存就会覆盖原文件，所以先按时间戳留一份副本；同一份内容已留过档就不再
        重复复制，免得用户反复双击启动时堆出一目录备份。备份自身失败（磁盘满、权限不足）
        也不能把启动拖崩，此时只报告并继续用安全值。
        """
        _LOGGER.warning("[%s] %s 内容损坏，本次已改用安全默认值", label, path)
        _LOGGER.warning("  - 原因：%s", reason)
        self._unprotected_paths.add(path)
        backup = None
        try:
            broken = path.read_bytes()
        except OSError as error:
            _LOGGER.warning("  - 备份失败，无法读取原文件：%s，原文件保持原样", error)
            return False
        for candidate in sorted(path.parent.glob(f"{path.name}.corrupt-*")):
            try:
                if candidate.is_file() and candidate.read_bytes() == broken:
                    backup = candidate
                    break
            except OSError:
                continue
        if backup is None:
            base = f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            suffix = 0
            while True:
                backup = path.with_name(base if suffix == 0 else f"{base}-{suffix}")
                created = False
                try:
                    # 独占创建防止同秒留档覆盖；写入的是本次已读原文，而非再次读取变动中的文件。
                    with backup.open("xb") as handle:
                        created = True
                        handle.write(broken)
                        handle.flush()
                        os.fsync(handle.fileno())
                    break
                except FileExistsError:
                    suffix += 1
                except OSError as error:
                    if created:
                        try:
                            backup.unlink()
                        except OSError as cleanup_error:
                            _LOGGER.warning("  - 无法移除不完整备份 %s：%s", backup, cleanup_error)
                    _LOGGER.warning("  - 备份失败（%s）：%s，原文件保持原样", backup, error)
                    return False
        self._unprotected_paths.discard(path)
        _LOGGER.warning("  - 原文件未改动，已留档副本：%s", backup)
        return True

    def _save_json(self, path: Path, payload: dict) -> None:
        # 临时读取失败不代表内容损坏，空回退值不能覆盖尚未读过的合法提醒或设置。
        if path in self._unread_paths:
            raise OSError(f"原文件尚未成功读取，请解除占用后重新读取或重启程序：{path}")
        # 完整写入并落盘后才替换原文件；失败时旧提醒和设置仍然可读。
        text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path in self._unprotected_paths and path.exists():
            if not self._quarantine(path, "本地数据", "重新尝试留档，成功前禁止覆盖原文件"):
                raise OSError(f"原文件尚未成功留档，拒绝覆盖：{path}")
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
            self._unprotected_paths.discard(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
