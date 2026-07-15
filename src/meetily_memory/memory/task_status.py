from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meetily_memory.db.schema import index_connection
from meetily_memory.user_state import UserStateRepository, task_identity

ValidateTaskStatus = Callable[[str], None]
NowProvider = Callable[[], str]


@dataclass(frozen=True)
class TaskStatusContext:
    index_path: Path
    user_state: UserStateRepository
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
                """
                SELECT e.*, m.external_id AS meeting_external_id,
                       c.external_id AS chunk_external_id,
                       s.kind AS source_kind, s.path AS source_path
                FROM action_items e
                JOIN meetings m ON m.id = e.meeting_id
                JOIN sources s ON s.id = m.source_id
                JOIN chunks c ON c.id = e.source_chunk_id
                WHERE e.id = ?
                """,
                (action_item_id,),
            ).fetchone()
            if task is None:
                message = f"Task not found: {action_item_id}"
                raise ValueError(message)
            source_uuid = self.context.user_state.source_uuid(
                str(task["source_kind"]),
                str(task["source_path"]),
                now=now,
            )
            identity = task_identity(
                source_uuid,
                str(task["meeting_external_id"]),
                str(task["chunk_external_id"]),
                str(task["text"]),
            )
            self.context.user_state.set_task_state(
                identity,
                status,
                note=note,
                source="manual",
                now=now,
            )
            return {
                "id": int(task["id"]),
                "text": str(task["text"]),
                "status": status,
                "status_note": note,
                "status_source": "manual",
                "status_updated_at": now,
            }
