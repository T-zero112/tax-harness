from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from .access import authorize
from .contracts import Citation, Contract, Error, Identifier, Month, RuleHit, Scope
from .database import Database
from .memory import SqliteMemoryStore
from .ports import RevisionConflict
from .rules import SqliteRuleStore


class PreferenceInput(Contract):
    content: str
    source_message_id: Identifier
    user_wide: bool = False


class PreferenceEdit(Contract):
    content: str
    expected_revision: int = Field(ge=1)


class RevisionInput(Contract):
    expected_revision: int = Field(ge=1)


class RuleVersionInput(Contract):
    name: str
    version: Identifier
    effective_from: str
    entry: Identifier = "main"
    content: str

    def to_hit(self, scope: Scope, doc_id: str) -> RuleHit:
        return RuleHit(citation=Citation(doc_id=doc_id, name=self.name, version=self.version, entry=self.entry),
                       enterprise_id=scope.enterprise_id, effective_from=self.effective_from,
                       content=self.content, revision=1)


class RuleInput(RuleVersionInput):
    doc_id: Identifier


def request_scope(enterprise_id: Identifier, month: Month, x_demo_user: Annotated[Identifier, Header()]) -> Scope:
    return Scope(user_id=x_demo_user, enterprise_id=enterprise_id, month=month)


ScopedRequest = Annotated[Scope, Depends(request_scope)]


def call(action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except PermissionError:
        status, code, message = 403, "forbidden", "当前用户无权在该企业执行此操作。"
    except KeyError as exc:
        status, code, message = 404, "not_found", str(exc.args[0])
    except RevisionConflict as exc:
        status, code, message = 409, "conflict", str(exc)
    except ValueError as exc:
        status, code, message = 400, "invalid_argument", str(exc)
    return JSONResponse(status_code=status, content=Error(code=code, message=message).model_dump())


def read_records(database, scope):
    with database.connect() as connection:
        authorize(connection, scope)
        params = (scope.user_id, scope.enterprise_id, scope.month)
        tasks = connection.execute(
            "SELECT task_id,status,object_id,revision FROM tasks "
            "WHERE user_id=? AND enterprise_id=? AND month=? ORDER BY rowid DESC LIMIT 30", params,
        ).fetchall()
        operations = connection.execute(
            "SELECT operation_id,task_id,object_id,status,created_at FROM operations "
            "WHERE user_id=? AND enterprise_id=? AND month=? ORDER BY rowid DESC LIMIT 30", params,
        ).fetchall()
        events = connection.execute(
            "SELECT e.event_id,e.task_id,e.kind,e.payload_json,e.created_at FROM events e JOIN tasks t ON t.task_id=e.task_id "
            "WHERE t.user_id=? AND t.enterprise_id=? AND t.month=? ORDER BY e.event_id DESC LIMIT 30", params,
        ).fetchall()
        messages = connection.execute(
            "SELECT m.message_id,m.content,m.created_at FROM messages m JOIN tasks t ON t.task_id=m.task_id "
            "WHERE t.user_id=? AND t.enterprise_id=? AND t.month=? AND m.role='user' ORDER BY m.rowid DESC LIMIT 30", params,
        ).fetchall()
        month_prefix = scope.month + "-%"
        invoice_count = connection.execute("SELECT count(*) FROM invoices WHERE enterprise_id=? AND invoice_date LIKE ?",
                                           (scope.enterprise_id, month_prefix)).fetchone()[0]
        bank_count = connection.execute("SELECT count(*) FROM bank_transactions WHERE enterprise_id=? AND transaction_date LIKE ?",
                                        (scope.enterprise_id, month_prefix)).fetchone()[0]
    return {"invoice_count": invoice_count, "bank_count": bank_count,
            "tasks": [dict(row) for row in tasks], "operations": [dict(row) for row in operations],
            "events": [dict(row) for row in events], "source_messages": [dict(row) for row in messages]}


def mount_workbench(app, database: Database):
    memories = SqliteMemoryStore(database)
    rules = SqliteRuleStore(database)
    app.state.memory_store = memories
    app.state.rule_store = rules
    router = APIRouter()

    @router.get("/api/workbench/records")
    def records(scope: ScopedRequest):
        return call(read_records, database, scope)

    @router.get("/api/memories")
    def list_memories(scope: ScopedRequest):
        return call(memories.list_active, scope)

    @router.post("/api/memories", status_code=201)
    def create_memory(scope: ScopedRequest, item: PreferenceInput):
        return call(memories.create, scope, item.content, item.source_message_id, user_wide=item.user_wide)

    @router.patch("/api/memories/{memory_id}")
    def correct_memory(scope: ScopedRequest, memory_id: str, item: PreferenceEdit):
        return call(memories.correct, scope, memory_id, item.content, item.expected_revision)

    @router.delete("/api/memories/{memory_id}")
    def delete_memory(scope: ScopedRequest, memory_id: str, expected_revision: int):
        return call(memories.delete, scope, memory_id, expected_revision)

    @router.get("/api/rules")
    def list_rules(scope: ScopedRequest, query: str | None = None):
        if query is None:
            return call(rules.list_versions, scope)
        return call(rules.search, scope, query)

    @router.post("/api/rules", status_code=201)
    def add_rule(scope: ScopedRequest, item: RuleInput):
        return call(rules.add_version, scope, item.to_hit(scope, item.doc_id))

    @router.post("/api/rules/{doc_id}/versions", status_code=201)
    def add_rule_version(scope: ScopedRequest, doc_id: Identifier, item: RuleVersionInput):
        return call(rules.add_version, scope, item.to_hit(scope, doc_id))

    @router.post("/api/rules/{doc_id}/deactivate")
    def deactivate_rule(scope: ScopedRequest, doc_id: str, version: str, item: RevisionInput):
        return call(rules.deactivate, scope, doc_id, version, item.expected_revision)

    web = Path(__file__).with_name("web")

    @router.get("/", include_in_schema=False)
    def index():
        return FileResponse(web / "index.html")

    app.include_router(router)
    app.mount("/assets", StaticFiles(directory=web), name="workbench_assets")
