import json

from .access import authorize
from .business import read_invoice
from .contracts import Error, InvoiceSaveRequest, OperationStatus, Scope, TaskCloseRequest, TaskStatus, ToolResult
from .database import Database
from .ports import RevisionConflict

UNRESOLVED = (OperationStatus.PREPARED, OperationStatus.DISPATCHED, OperationStatus.UNKNOWN)
TERMINAL_TASKS = (TaskStatus.COMPLETED, TaskStatus.CANCELLED)


class VerificationFailure(RuntimeError):
    """The saved invoice or its recorded result could not be verified."""


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def load_operation(connection, scope: Scope, operation_id: str):
    row = connection.execute(
        "SELECT * FROM operations WHERE operation_id=? AND user_id=? AND enterprise_id=? AND month=?",
        (operation_id, scope.user_id, scope.enterprise_id, scope.month),
    ).fetchone()
    if row is None:
        raise KeyError("当前范围内没有这项操作")
    return row


def operation_result(row) -> ToolResult:
    status = row["status"]
    if status == OperationStatus.CONFIRMED:
        if row["result_json"] is None or row["verification_json"] is None:
            raise VerificationFailure("操作缺少可核对的保存结果")
        return ToolResult.model_validate_json(row["result_json"])

    error = None
    tool_status = "pending"
    if status == OperationStatus.UNKNOWN:
        tool_status = "unknown"
        error = Error(code="unknown_result", message="保存结果待核验。")
    elif status == OperationStatus.FAILED:
        tool_status = "failed"
        error = Error(code="tool_failed", message="保存失败。")
    return ToolResult(status=tool_status, object_id=row["object_id"], operation_id=row["operation_id"],
                      data={"operation_status": status, "task_id": row["task_id"]}, error=error)


def assert_no_pending_write(connection, *, enterprise_id: str, object_id: str | None = None,
                            task_id: str | None = None):
    if task_id is not None:
        predicate, value = "task_id", task_id
    else:
        predicate, value = "object_id", object_id
    pending = connection.execute(
        f"SELECT 1 FROM operations WHERE enterprise_id=? AND {predicate}=? AND status IN (?,?,?)",
        (enterprise_id, value, *UNRESOLVED),
    ).fetchone()
    if pending:
        raise RevisionConflict("存在未决写入，请先核验保存结果。")


def load_task(connection, scope: Scope, task_id: str):
    return connection.execute(
        "SELECT task_id,status,object_id,revision FROM tasks WHERE task_id=? AND user_id=? AND enterprise_id=? AND month=?",
        (task_id, scope.user_id, scope.enterprise_id, scope.month),
    ).fetchone()


def ensure_writable_task(connection, scope: Scope, task_id: str, object_id: str):
    task = load_task(connection, scope, task_id)
    if task:
        if task["status"] in TERMINAL_TASKS:
            raise RevisionConflict("任务已关闭，请在未完成任务中保存。")
        if task["object_id"] is not None and task["object_id"] != object_id:
            raise RevisionConflict("发票与当前任务的目标不一致。")
        return

    if connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
        raise KeyError("当前范围内没有这项任务")
    if connection.execute(
        "SELECT 1 FROM tasks WHERE user_id=? AND enterprise_id=? AND status IN ('running','waiting_user','blocked')",
        (scope.user_id, scope.enterprise_id),
    ).fetchone():
        raise RevisionConflict("该企业已有未完成任务，请继续或先关闭该任务。")
    connection.execute(
        "INSERT INTO tasks(task_id,user_id,enterprise_id,month,status) VALUES (?,?,?,?,'running')",
        (task_id, scope.user_id, scope.enterprise_id, scope.month),
    )


class SqliteBusinessWriter:
    def __init__(self, database: Database):
        self.database = database

    def update_fields(self, scope: Scope, object_id: str, request: InvoiceSaveRequest) -> ToolResult:
        patch = request.patch.model_dump(exclude_unset=True)
        payload = {"kind": "invoice_save", "scope": scope.model_dump(), "object_id": object_id,
                   **request.model_dump(exclude={"patch"}), "patch": patch}

        with self.database.write() as connection:
            authorize(connection, scope, write=True)
            previous = connection.execute(
                "SELECT payload_json FROM requests WHERE user_id=? AND request_id=?",
                (scope.user_id, request.request_id),
            ).fetchone()
            if previous:
                if json.loads(previous["payload_json"]) != payload:
                    raise RevisionConflict("请求编号已绑定不同的保存内容。")
                return operation_result(load_operation(connection, scope, request.operation_id))
            if connection.execute("SELECT 1 FROM operations WHERE operation_id=?", (request.operation_id,)).fetchone():
                raise RevisionConflict("操作编号已使用，请核对原请求编号。")

            before = read_invoice(connection, scope, object_id)
            assert_no_pending_write(connection, enterprise_id=scope.enterprise_id, object_id=object_id)
            if before["revision"] != request.expected_revision:
                raise RevisionConflict("发票已更新，请重新读取后再保存。")
            ensure_writable_task(connection, scope, request.task_id, object_id)

            connection.execute(
                "INSERT INTO requests(user_id,request_id,session_id,payload_json,task_id,status) VALUES (?,?,?,?,?,'running')",
                (scope.user_id, request.request_id, request.session_id, dump(payload), request.task_id),
            )
            connection.execute(
                "INSERT INTO operations(operation_id,request_id,task_id,user_id,enterprise_id,month,object_id,patch_json,status) "
                "VALUES (?,?,?,?,?,?,?,?,'prepared')",
                (request.operation_id, request.request_id, request.task_id, scope.user_id, scope.enterprise_id,
                 scope.month, object_id, dump(patch)),
            )

            expected = {**before, **patch}
            if expected != before:
                updated = connection.execute(
                    "UPDATE invoices SET purpose=?,note=?,revision=revision+1 "
                    "WHERE enterprise_id=? AND invoice_id=? AND substr(invoice_date,1,7)=? AND revision=?",
                    (expected["purpose"], expected["note"], scope.enterprise_id, object_id, scope.month, request.expected_revision),
                )
                if updated.rowcount != 1:
                    raise RevisionConflict("发票已更新，请重新读取后再保存。")
                expected["revision"] += 1
            after = read_invoice(connection, scope, object_id)
            if after != expected:
                raise VerificationFailure("发票回查与预期不一致，保存已回滚。")

            verification = {"matched": True, "before_revision": before["revision"],
                            "after_revision": after["revision"], "fields": sorted(patch),
                            "before": {field: before[field] for field in patch},
                            "after": {field: after[field] for field in patch},
                            "changed": after != before}
            result = ToolResult(status="succeeded", object_id=object_id, operation_id=request.operation_id,
                                data={"operation_status": OperationStatus.CONFIRMED, "task_id": request.task_id,
                                      "record": after, "verification": verification})
            result_json = result.model_dump_json()
            connection.execute("UPDATE operations SET status='confirmed',result_json=?,verification_json=? WHERE operation_id=?",
                               (result_json, dump(verification), request.operation_id))
            connection.execute("UPDATE requests SET status='completed',response_json=? WHERE user_id=? AND request_id=?",
                               (result_json, scope.user_id, request.request_id))
            connection.execute("INSERT INTO events(task_id,kind,payload_json) VALUES (?,'invoice_saved',?)",
                               (request.task_id, result_json))
            return result

    def get_operation(self, scope: Scope, operation_id: str) -> ToolResult:
        with self.database.connect() as connection:
            authorize(connection, scope)
            return operation_result(load_operation(connection, scope, operation_id))

    def reconcile_operation(self, scope: Scope, operation_id: str) -> ToolResult:
        with self.database.write() as connection:
            authorize(connection, scope, write=True)
            operation = load_operation(connection, scope, operation_id)
            if operation["status"] not in UNRESOLVED:
                return operation_result(operation)
            request = connection.execute(
                "SELECT response_json FROM requests WHERE user_id=? AND request_id=? AND task_id=? AND status='completed'",
                (scope.user_id, operation["request_id"], operation["task_id"]),
            ).fetchone()
            if not request or not request["response_json"]:
                return operation_result(operation)
            try:
                receipt = ToolResult.model_validate_json(request["response_json"])
                data = receipt.data or {}
                verification = data.get("verification", {})
                record = read_invoice(connection, scope, operation["object_id"])
            except (KeyError, ValueError):
                return operation_result(operation)
            patch = json.loads(operation["patch_json"])
            if not (receipt.status == "succeeded" and receipt.operation_id == operation_id
                    and receipt.object_id == operation["object_id"] and data.get("task_id") == operation["task_id"]
                    and data.get("operation_status") == OperationStatus.CONFIRMED
                    and isinstance(verification, dict) and verification.get("matched") is True
                    and verification.get("after") == patch and verification.get("after_revision") == record["revision"]
                    and data.get("record") == record):
                return operation_result(operation)
            connection.execute(
                "UPDATE operations SET status='confirmed',result_json=?,verification_json=? WHERE operation_id=?",
                (receipt.model_dump_json(), dump(verification), operation_id),
            )
            connection.execute("INSERT INTO events(task_id,kind,payload_json) VALUES (?,'save_reconciled',?)",
                               (operation["task_id"], dump({"operation_id": operation_id, "revision": record["revision"]})))
            return receipt

    def close_task(self, scope: Scope, task_id: str, request: TaskCloseRequest) -> dict:
        with self.database.write() as connection:
            authorize(connection, scope)
            task = load_task(connection, scope, task_id)
            if task is None:
                raise KeyError("当前范围内没有这项任务")
            assert_no_pending_write(connection, enterprise_id=scope.enterprise_id, task_id=task_id)
            if task["status"] == request.status:
                return {"task_id": task_id, "status": task["status"], "revision": task["revision"]}
            if task["status"] in TERMINAL_TASKS or task["revision"] != request.expected_revision:
                raise RevisionConflict("任务状态已变化，请重新读取。")
            result = {"task_id": task_id, "status": request.status, "revision": task["revision"] + 1}
            connection.execute("UPDATE tasks SET status=?,revision=revision+1 WHERE task_id=?", (request.status, task_id))
            connection.execute("INSERT INTO events(task_id,kind,payload_json) VALUES (?,'task_closed',?)", (task_id, dump(result)))
            return result
