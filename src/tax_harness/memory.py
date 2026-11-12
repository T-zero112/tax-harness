from uuid import uuid4

from .access import authorize
from .contracts import HistoryMessage, Memory, MutationReceipt, RevisionRef, Scope
from .database import Database
from .ports import RevisionConflict

MEMORY_FIELDS = "memory_id,user_id,enterprise_id,content,source_message_id,revision"
_PLACEHOLDER = "[该偏好已被更正]"


def _redact_message(message: HistoryMessage, pairs: list[tuple[str, str]]) -> HistoryMessage:
    updated = message.content
    for old, current in sorted(pairs, key=lambda item: len(item[0] or ""), reverse=True):
        old = (old or "").strip()
        if len(old) < 4 or old not in updated:
            continue
        current = (current or "").strip()
        if current and old in current and current in updated:
            continue
        updated = updated.replace(old, _PLACEHOLDER)
    if updated == message.content:
        return message
    return message.model_copy(update={"content": updated})


class SqliteMemoryStore:
    def __init__(self, database: Database):
        self.database = database

    def list_active(self, scope: Scope) -> list[Memory]:
        with self.database.connect() as connection:
            authorize(connection, scope)
            rows = connection.execute(
                f"SELECT {MEMORY_FIELDS} FROM memories WHERE user_id=? AND active=1 "
                "AND (enterprise_id=? OR enterprise_id IS NULL) ORDER BY memory_id",
                (scope.user_id, scope.enterprise_id),
            )
            return [Memory(**dict(row)) for row in rows]

    def create(self, scope: Scope, content: str, source_message_id: str, *, user_wide: bool = False) -> Memory:
        if not content.strip():
            raise ValueError("偏好内容不能为空")
        with self.database.write() as connection:
            authorize(connection, scope)
            source = connection.execute(
                "SELECT 1 FROM messages m JOIN tasks t ON t.task_id=m.task_id "
                "WHERE m.message_id=? AND m.role='user' AND t.user_id=? AND t.enterprise_id=?",
                (source_message_id, scope.user_id, scope.enterprise_id),
            ).fetchone()
            if source is None:
                raise KeyError("找不到当前用户和企业下的来源消息")
            memory = Memory(memory_id=uuid4().hex, user_id=scope.user_id,
                            enterprise_id=None if user_wide else scope.enterprise_id,
                            content=content.strip(), source_message_id=source_message_id, revision=1)
            connection.execute(
                f"INSERT INTO memories({MEMORY_FIELDS}) VALUES (?,?,?,?,?,?)",
                tuple(memory.model_dump().values()),
            )
        return memory

    def correct(self, scope: Scope, memory_id: str, content: str, expected_revision: int) -> MutationReceipt:
        if not content.strip():
            raise ValueError("更正内容不能为空")
        return self._revise(scope, memory_id, expected_revision, content.strip())

    def delete(self, scope: Scope, memory_id: str, expected_revision: int) -> MutationReceipt:
        return self._revise(scope, memory_id, expected_revision, None)

    def _revise(self, scope, memory_id, expected_revision, content):
        with self.database.write() as connection:
            authorize(connection, scope)
            row = connection.execute(
                "SELECT revision,source_message_id FROM memories WHERE memory_id=? AND user_id=? "
                "AND active=1 AND (enterprise_id=? OR enterprise_id IS NULL)",
                (memory_id, scope.user_id, scope.enterprise_id),
            ).fetchone()
            if row is None:
                raise KeyError("找不到可管理的偏好")
            if row["revision"] != expected_revision:
                raise RevisionConflict("偏好已更新，请重新读取")
            revision = expected_revision + 1
            source_id = row["source_message_id"]
            previous = connection.execute("SELECT content FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            connection.execute("UPDATE memories SET content=?,revision=?,active=? WHERE memory_id=?",
                               (content or "", revision, int(content is not None), memory_id))
            superseded = previous["content"] if content != previous["content"] else None
            connection.execute(
                "INSERT INTO memory_invalidations(memory_id,message_id,revision,superseded_content) VALUES (?,?,?,?)",
                (memory_id, source_id, revision, superseded),
            )
        return MutationReceipt(reference=RevisionRef(kind="memory", identifier=memory_id, revision=revision),
                               invalidated_message_ids=[source_id])

    def filter_history(self, scope: Scope, messages: list[HistoryMessage]) -> list[HistoryMessage]:
        with self.database.connect() as connection:
            authorize(connection, scope)
            rows = connection.execute(
                "SELECT m.message_id,m.role,m.content FROM messages m JOIN tasks t ON t.task_id=m.task_id "
                "WHERE t.user_id=? AND t.enterprise_id=? AND t.month=? AND NOT EXISTS ("
                "SELECT 1 FROM memory_invalidations i JOIN memories p ON p.memory_id=i.memory_id "
                "WHERE i.message_id=m.message_id AND p.user_id=t.user_id "
                "AND (p.enterprise_id=t.enterprise_id OR p.enterprise_id IS NULL))",
                (scope.user_id, scope.enterprise_id, scope.month),
            )
            allowed = {row["message_id"]: HistoryMessage(**dict(row)) for row in rows}
            superseded = connection.execute(
                "SELECT i.superseded_content, CASE WHEN p.active=1 THEN p.content ELSE '' END "
                "FROM memory_invalidations i JOIN memories p ON p.memory_id=i.memory_id "
                "WHERE p.user_id=? AND (p.enterprise_id=? OR p.enterprise_id IS NULL) "
                "AND i.superseded_content IS NOT NULL",
                (scope.user_id, scope.enterprise_id),
            ).fetchall()
        pairs = [(row[0], row[1] or "") for row in superseded]
        return [_redact_message(allowed[message.message_id], pairs)
                for message in messages if message.message_id in allowed]

    def is_current(self, scope: Scope, reference: RevisionRef) -> bool:
        return reference.kind == "memory" and any(
            memory.memory_id == reference.identifier and memory.revision == reference.revision
            for memory in self.list_active(scope)
        )
