from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from config import DEFAULT_TITLE


TRIGGER_TYPE_VALUES = {"once", "interval", "weekly", "weekly_last_workday", "yearly", "cron"}


def validate_interval_config(config: dict[str, Any]) -> dict[str, int]:
    values = {key: int(config.get(key, 0)) for key in ("weeks", "days", "hours", "minutes", "seconds")}
    if any(value < 0 for value in values.values()):
        raise ValueError("循环间隔不能为负数。")
    if not any(values.values()):
        raise ValueError("循环间隔至少有一项大于 0。")
    return values


@dataclass
class Task:
    id: str = field(default_factory=lambda: uuid4().hex)
    title: str = DEFAULT_TITLE
    body: str = ""
    trigger_type: str = "once"
    trigger_config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    sound: bool = False
    priority: str = "normal"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    next_run: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __post_init__(self) -> None:
        # 读取本地数据时若记录不合法会被降级处理，这里登记读取阶段发现的问题，
        # 供启动日志与排查使用；它不是数据字段，不会写回 tasks.json。
        self.load_warning: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], strict: bool = True) -> "Task":
        task = cls(
            id=data.get("id", uuid4().hex),
            title=data.get("title") or DEFAULT_TITLE,
            body=data.get("body", ""),
            trigger_type=data.get("trigger_type", "once"),
            trigger_config=data.get("trigger_config") or {},
            enabled=bool(data.get("enabled", True)),
            sound=bool(data.get("sound", False)),
            priority=data.get("priority", "normal"),
            created_at=data.get("created_at", datetime.now().isoformat(timespec="seconds")),
            next_run=data.get("next_run"),
        )
        if strict:
            task.validate()
            return task
        return task._degrade_if_broken()

    def _degrade_if_broken(self) -> "Task":
        """读取本地已有提醒时的容错：坏记录降级为可展示、可编辑的停用任务。

        只有读文件这条路径走容错：一条坏记录不能让整批提醒都读不出来，更不能让窗口化的
        程序启动即崩。validate() 的校验语义不变，界面保存（TaskDialog._save）与调度
        （TaskScheduler._schedule_task）仍走 strict，负数、全零、未知触发方式照样拒绝。
        """
        problems: list[str] = []
        # 字段被写成了别的类型（标题写成数字、id 写成对象）时，后续渲染与搜索按文本处理、
        # 调度器按字典键索引，读取阶段先收敛成程序能处理的类型，避免只是类型不对就让整条不可用。
        if not isinstance(self.id, str):
            self.id = str(self.id)
        if not isinstance(self.title, str):
            self.title = str(self.title)
        if not isinstance(self.body, str):
            self.body = str(self.body)
        if not isinstance(self.trigger_config, dict):
            # 配置被写成了非对象（例如字符串），卡片与编辑对话框都按字典读取，
            # 只能按空配置读取并把原值记进原因，用户重新选择触发方式后即可保存。
            problems.append(f"触发配置不是配置对象，已按空配置读取（原值 {self.trigger_config!r}）")
            self.trigger_config = {}
        try:
            self.validate()
        except Exception as exc:
            problems.append(f"{type(exc).__name__}: {exc}")
            if not isinstance(self.trigger_type, str) or self.trigger_type not in TRIGGER_TYPE_VALUES:
                # 未知触发方式会让编辑对话框查表直接 KeyError（卡片打得开、改不了），
                # 因此退回默认的单次提醒；原始值已记在原因里，用户改选真实方式后再保存。
                self.trigger_type = "once"
            # 降级成停用：不排程，也不影响同文件里的其它提醒，用户在界面上改好再启用。
            self.enabled = False
            self.next_run = None
        if problems:
            # 降级不得静默：原因交给 TaskStore 汇总写入运行日志，便于窗口化运行后排查。
            self.load_warning = "；".join(problems)
        return self

    def validate(self) -> None:
        if self.trigger_type not in TRIGGER_TYPE_VALUES:
            raise ValueError(f"Unsupported trigger type: {self.trigger_type}")
        if not self.title.strip():
            self.title = DEFAULT_TITLE
        if self.priority not in {"normal", "high"}:
            self.priority = "normal"
        if self.trigger_type == "interval":
            validate_interval_config(self.trigger_config)
