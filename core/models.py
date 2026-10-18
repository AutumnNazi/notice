from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from config import DEFAULT_TITLE


TRIGGER_TYPE_VALUES = {"once", "interval", "weekly", "weekly_last_workday", "yearly", "cron"}


def validate_interval_config(config: dict[str, Any]) -> dict[str, int]:
    values: dict[str, int] = {}
    for key in ("weeks", "days", "hours", "minutes", "seconds"):
        raw = config.get(key, 0)
        if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
            raise ValueError("循环间隔必须使用整数，不能使用布尔值或小数。")
        try:
            values[key] = int(raw)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("循环间隔必须是有效整数。") from exc
    if any(value < 0 for value in values.values()):
        raise ValueError("循环间隔不能为负数。")
    if not any(values.values()):
        raise ValueError("循环间隔至少有一项大于 0。")
    try:
        # 调度器会以当前时间加间隔作为首次触发，间隔和结果日期都必须可表示。
        datetime.now() + timedelta(**values)
    except OverflowError as exc:
        raise ValueError("循环间隔超出可调度的日期范围。") from exc
    return values


def validate_calendar_config(trigger_type: str, config: dict[str, Any]) -> None:
    if trigger_type not in {"weekly", "weekly_last_workday", "yearly"}:
        return
    if not isinstance(config, dict):
        raise ValueError("提醒时间配置必须是对象。")
    values: dict[str, int] = {}
    fields = ("hour", "minute", "second", "month", "day") if trigger_type == "yearly" else ("hour", "minute", "second")
    for key in fields:
        default = 18 if key == "hour" and trigger_type == "weekly_last_workday" else 0
        raw = config.get(key, default)
        if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
            raise ValueError(f"{key} 必须是整数，不能使用布尔值或小数。")
        try:
            values[key] = int(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{key} 必须是有效整数。") from exc
    if not (0 <= values["hour"] <= 23 and 0 <= values["minute"] <= 59 and 0 <= values["second"] <= 59):
        raise ValueError("提醒时间须在 00:00:00 至 23:59:59 之间。")
    if trigger_type == "yearly":
        # 用闰年校验月日，允许 2 月 29 日，但不接受永远不会到来的日期。
        try:
            datetime(2000, values["month"], values["day"])
        except (ValueError, OverflowError) as exc:
            raise ValueError("提醒月日不是有效日期。") from exc
    if trigger_type == "weekly":
        weekdays = config.get("weekdays")
        if not isinstance(weekdays, list) or not weekdays or any(
                type(day) is not int or not 0 <= day <= 6 for day in weekdays):
            raise ValueError("星期必须是包含 0 至 6 整数的非空数组。")


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
            enabled=data.get("enabled", True),
            sound=data.get("sound", False),
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
        for name in ("enabled", "sound"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                problems.append(f"{name} 应为布尔值，实际为 {value!r}")
                setattr(self, name, False)
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
            self.enabled = False
            self.next_run = None
            # 降级不得静默：原因交给 TaskStore 汇总写入运行日志，便于窗口化运行后排查。
            self.load_warning = "；".join(problems)
        return self

    def validate(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(self.sound, bool):
            raise ValueError("enabled 和 sound 必须是布尔值。")
        if self.trigger_type not in TRIGGER_TYPE_VALUES:
            raise ValueError(f"Unsupported trigger type: {self.trigger_type}")
        if not self.title.strip():
            self.title = DEFAULT_TITLE
        if not isinstance(self.priority, str) or self.priority not in {"normal", "high"}:
            self.priority = "normal"
        if self.trigger_type == "interval":
            validate_interval_config(self.trigger_config)
        validate_calendar_config(self.trigger_type, self.trigger_config)
