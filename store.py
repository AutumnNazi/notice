from __future__ import annotations

import json
from pathlib import Path

from config import DATA_DIR, SETTINGS_DEFAULTS, SETTINGS_FILE, TASKS_FILE
from core.models import Task


class TaskStore:
    def __init__(self, path: Path = TASKS_FILE) -> None:
        self.path = path
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def load_tasks(self) -> list[Task]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return [Task.from_dict(item) for item in data.get("tasks", [])]

    def save_tasks(self, tasks: list[Task]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tasks": [task.to_dict() for task in tasks]}
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def load_settings(self, path: Path = SETTINGS_FILE) -> dict[str, object]:
        if not path.exists():
            return SETTINGS_DEFAULTS.copy()
        with path.open("r", encoding="utf-8") as handle:
            return SETTINGS_DEFAULTS | json.load(handle)

    def save_settings(self, settings: dict[str, object], path: Path = SETTINGS_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(settings, handle, ensure_ascii=False, indent=2)
