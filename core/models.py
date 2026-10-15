from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from config import DEFAULT_TITLE


TRIGGER_TYPE_VALUES = {"once", "interval", "weekly", "weekly_last_workday", "yearly", "cron"}


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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
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
        task.validate()
        return task

    def validate(self) -> None:
        if self.trigger_type not in TRIGGER_TYPE_VALUES:
            raise ValueError(f"Unsupported trigger type: {self.trigger_type}")
        if not self.title.strip():
            self.title = DEFAULT_TITLE
        if self.priority not in {"normal", "high"}:
            self.priority = "normal"
