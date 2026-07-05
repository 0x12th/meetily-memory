from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meetily_memory.db.schema import index_connection

ValidateTaskStatus = Callable[[str], None]
NowProvider = Callable[[], str]


@dataclass(frozen=True)
class TaskStatusContext:
    index_path: Path
    validate_status: ValidateTaskStatus
    now: NowProvider


class TaskStatusRepository:
    def __init__(self, context: TaskStatusContext) -> None:
        self.context = context

    def set_task_status(
        self,
        action_item_id: int,
        status: str,
        *,
        note: str | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        self.context.validate_status(status)
        now = now or self.context.now()
        with index_connection(self.context.index_path) as conn:
            task = conn.execute(
                "SELECT * FROM action_items WHERE id = ?",
                (action_item_id,),
            ).fetchone()
            if task is None:
                message = f"Task not found: {action_item_id}"
                raise ValueError(message)
            conn.execute(
                """
                INSERT INTO task_status_overrides (
                  action_item_id, status, note, source, created_at, updated_at
                )
                VALUES (?, ?, ?, 'manual', ?, ?)
                ON CONFLICT(action_item_id) DO UPDATE SET
                  status = excluded.status,
                  note = excluded.note,
                  source = excluded.source,
                  updated_at = excluded.updated_at
                """,
                (action_item_id, status, note, now, now),
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT e.id, e.text, COALESCE(o.status, 'open') AS status,
                       o.note AS status_note, o.source AS status_source,
                       o.updated_at AS status_updated_at
                FROM action_items e
                LEFT JOIN task_status_overrides o ON o.action_item_id = e.id
                WHERE e.id = ?
                """,
                (action_item_id,),
            ).fetchone()
            return dict(row)
