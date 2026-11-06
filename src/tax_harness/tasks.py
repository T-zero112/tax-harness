import json

from .access import authorize
from .business import read_invoice
from .business_writes import TERMINAL_TASKS, UNRESOLVED, assert_no_pending_write, dump
from .contracts import Scope, TaskCorrectionRequest
from .database import Database
from .ports import RevisionConflict


def find_active_task(connection, user_id: str, enterprise_id: str):
    return connection.execute(
        "SELECT * FROM tasks WHERE user_id=? AND enterprise_id=? AND status IN ('running','waiting_user','blocked')",
        (user_id, enterprise_id),
    ).fetchone()


def active_task_status(database: Database, scope: Scope):
    with database.connect() as connection:
        authorize(connection, scope)
        task = find_active_task(connection, scope.user_id, scope.enterprise_id)
        if task is None:
            return {"task": None}
        matches = task["month"] == scope.month
        operations = connection.execute(
            "SELECT operation_id,status FROM operations WHERE task_id=? AND status IN (?,?,?) ORDER BY created_at,operation_id",
            (task["task_id"], *UNRESOLVED),
        ).fetchall() if matches else []
        return {"task": {"task_id": task["task_id"], "month": task["month"], "status": task["status"],
                         "revision": task["revision"], "matches_month": matches,
                         "unresolved_operations": [dict(operation) for operation in operations]}}


def scoped_task(connection, scope: Scope, task_id: str):
    row = connection.execute("SELECT * FROM tasks WHERE task_id=? AND user_id=? AND enterprise_id=? AND month=?",
                             (task_id, scope.user_id, scope.enterprise_id, scope.month)).fetchone()
    if row is None:
        raise KeyError("当前范围内没有这项任务")
    return row


def task_view(row) -> dict:
    state = json.loads(row["state_json"])
    return {"task_id": row["task_id"], "status": row["status"], "revision": row["revision"],
            "object_id": row["object_id"], "question": state.get("question"),
            "has_proposal": bool(state.get("proposal"))}


def read_task(database: Database, scope: Scope, task_id: str):
    with database.connect() as connection:
        authorize(connection, scope)
        return task_view(scoped_task(connection, scope, task_id))


def correct_task(database: Database, scope: Scope, task_id: str, request: TaskCorrectionRequest):
    with database.write() as connection:
        authorize(connection, scope)
        row = scoped_task(connection, scope, task_id)
        assert_no_pending_write(connection, enterprise_id=scope.enterprise_id, task_id=task_id)
        if row["status"] in TERMINAL_TASKS or row["revision"] != request.expected_revision:
            raise RevisionConflict("任务状态已变化，请重新读取。")
        state = json.loads(row["state_json"])
        if "question" in request.model_fields_set:
            state.pop("question", None)
            if request.question is not None:
                state["question"] = request.question.strip()
        object_id = request.object_id if "object_id" in request.model_fields_set else row["object_id"]
        if object_id is not None:
            read_invoice(connection, scope, object_id)
        state.pop("proposal", None)
        status = "waiting_user" if state.get("question") else "running"
        connection.execute("UPDATE tasks SET object_id=?,status=?,state_json=?,revision=revision+1 WHERE task_id=?",
                           (object_id, status, dump(state), task_id))
        result = task_view(scoped_task(connection, scope, task_id))
        connection.execute("INSERT INTO events(task_id,kind,payload_json) VALUES (?,'task_corrected',?)",
                           (task_id, dump({"revision": result["revision"], "object_id": object_id})))
        return result
