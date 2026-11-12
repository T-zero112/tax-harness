import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from threading import Lock
from uuid import uuid4

from .access import authorize
from .agent_tools import TOOL_INPUTS
from .business_writes import UNRESOLVED, VerificationFailure
from .context import ContextBudgetExceeded, SqliteContextBuilder, encoded, rule_ref
from .contracts import (
    Citation, ContextRequest, Error, InvoiceSaveRequest, MessageRequest, RevisionRef,
    Scope, TaskStatus, ToolResult, TurnResponse,
)
from .database import Database
from .model_adapter import AnswerDecision, AskDecision, InvalidDecision, ModelAdapter, ModelUnavailable
from .ports import BusinessReader, BusinessWriter, MemoryStore, RevisionConflict, RuleStore
from .purpose_checks import check_invoice_purposes
from .tasks import find_active_task

TASK_COMMANDS = {"继续任务", "确认保存", "取消保存"}
MODEL_CONFIGURATION_MESSAGE = "模型尚未配置，请设置赛事模型的端点、名称和密钥。"


@dataclass
class Turn:
    scope: Scope
    task_id: str
    revision: int
    state: dict
    read_only: bool
    active_month: str | None = None
    recover_operation: str | None = None
    object_id: str | None = None


@dataclass
class Outcome:
    reply: str
    citations: list[Citation] = field(default_factory=list)
    operation_ids: list[str] = field(default_factory=list)
    blocked: bool = False
    failed: bool = False
    model_unavailable: bool = False


def failed_tool(code, message) -> ToolResult:
    return ToolResult(status="failed", error=Error(code=code, message=message))


class TurnExecutor:
    def __init__(self, database: Database, model: ModelAdapter | None, reader: BusinessReader, writer: BusinessWriter,
                 memories: MemoryStore, rules: RuleStore, *, budget: int = 12000):
        self.database = database
        self.model = model
        self.reader = reader
        self.writer = writer
        self.memories = memories
        self.rules = rules
        self.context = SqliteContextBuilder(database, memories, rules)
        self.budget = budget
        self.turn_lock = Lock()
        self.model_key = getattr(model, "key", "")

    def audit_text(self, text: str) -> str:
        return text.replace(self.model_key, "[已隐藏模型密钥]") if self.model_key else text

    def audit_payload(self, payload):
        if isinstance(payload, str):
            return self.audit_text(payload)
        if isinstance(payload, dict):
            return {self.audit_text(key): self.audit_payload(value) for key, value in payload.items()}
        if isinstance(payload, list):
            return [self.audit_payload(value) for value in payload]
        return payload

    def event(self, task_id, kind, payload):
        with self.database.write() as connection:
            connection.execute("INSERT INTO events(task_id,kind,payload_json) VALUES (?,?,?)",
                               (task_id, kind, encoded(self.audit_payload(payload))))

    def start(self, user_id, request) -> Turn | TurnResponse:
        fingerprint = hashlib.sha256(encoded(request.model_dump()).encode()).hexdigest()
        command = request.text.strip()
        with self.database.write() as connection:
            active = find_active_task(connection, user_id, request.enterprise_id)
            previous = connection.execute(
                "SELECT r.payload_json,r.response_json,t.task_id,t.month,t.revision,t.state_json,t.object_id "
                "FROM requests r LEFT JOIN tasks t ON t.task_id=r.task_id "
                "WHERE r.user_id=? AND r.request_id=?", (user_id, request.request_id),
            ).fetchone()
            month = request.month or (previous["month"] if previous else None) or (active["month"] if active else None)
            if month is None:
                # authorize needs a complete Scope, but permission is independent of month.
                authorize(connection, Scope(user_id=user_id, enterprise_id=request.enterprise_id, month="0001-01"))
                raise ValueError("请提供要处理的月份，格式为 YYYY-MM。")
            scope = Scope(user_id=user_id, enterprise_id=request.enterprise_id, month=month)
            authorize(connection, scope)
            if previous:
                payload = json.loads(previous["payload_json"])
                if payload.get("kind") != "turn" or payload.get("fingerprint") != fingerprint:
                    raise RevisionConflict("请求编号已绑定不同的对话内容。")
                if previous["response_json"] is not None:
                    return TurnResponse.model_validate_json(previous["response_json"])
                checkpoint = payload.get("checkpoint")
                if not checkpoint or previous["task_id"] is None:
                    raise RevisionConflict("原对话请求尚未确认，请查询任务或保存操作记录。")
                turn = Turn(scope, previous["task_id"], checkpoint["revision"], json.loads(previous["state_json"]),
                            checkpoint["read_only"], active["month"] if active else None, object_id=previous["object_id"])
                operation_id = checkpoint.get("operation_id")
                if operation_id and connection.execute(
                    "SELECT 1 FROM operations WHERE operation_id=? AND task_id=? AND user_id=? AND enterprise_id=? AND month=?",
                    (operation_id, turn.task_id, scope.user_id, scope.enterprise_id, scope.month),
                ).fetchone():
                    turn.recover_operation = operation_id
                    return turn
                if previous["revision"] != turn.revision:
                    raise RevisionConflict("任务已变化，无法重放中断的请求。请继续当前任务。")
                if self.model is None and command not in TASK_COMMANDS:
                    raise ModelUnavailable(MODEL_CONFIGURATION_MESSAGE)
                return turn

            if self.model is None and command not in TASK_COMMANDS:
                raise ModelUnavailable(MODEL_CONFIGURATION_MESSAGE)

            read_only = bool(active and active["month"] != month)
            if active and not read_only:
                turn = Turn(scope, active["task_id"], active["revision"], json.loads(active["state_json"]), False,
                            object_id=active["object_id"])
            else:
                turn = Turn(scope, uuid4().hex, 1, {}, read_only, active["month"] if active else None)
                connection.execute(
                    "INSERT INTO tasks(task_id,user_id,enterprise_id,month,status) VALUES (?,?,?,?,?)",
                    (turn.task_id, user_id, scope.enterprise_id, month, "completed" if read_only else "running"),
                )
            checkpoint = {"revision": turn.revision, "read_only": read_only}
            proposal = turn.state.get("proposal")
            if command in {"确认保存", "取消保存"} and proposal and proposal["session_id"] == request.session_id:
                checkpoint["operation_id"] = "turn-op-" + proposal["id"]
            payload = encoded({"kind": "turn", "fingerprint": fingerprint, "checkpoint": checkpoint})
            connection.execute(
                "INSERT INTO requests(user_id,request_id,session_id,payload_json,task_id,status) VALUES (?,?,?,?,?,'running')",
                (user_id, request.request_id, request.session_id, payload, turn.task_id),
            )
            return turn

    def execute(self, user_id: str, request: MessageRequest) -> TurnResponse:
        if not request.text.strip():
            raise ValueError("请输入消息。")
        if not self.turn_lock.acquire(blocking=False):
            raise RevisionConflict("当前有一轮对话正在处理，请稍后重试。")
        try:
            text = self.audit_text(request.text.strip())
            with self.database.mutation_lock:
                turn = self.start(user_id, request)
                if isinstance(turn, TurnResponse):
                    return turn
                # Task commands use the same revision through confirmation and save.
                if turn.recover_operation or text in TASK_COMMANDS:
                    if turn.recover_operation:
                        outcome = self.recover_saved(turn, turn.recover_operation, cancel=text == "取消保存")
                    elif text == "继续任务":
                        outcome = self.resume(turn, request)
                    else:
                        outcome = self.confirm(turn, request, cancel=text == "取消保存")
                    return self.finish(turn, request, text, outcome)
            previous_proposal = turn.state.get("proposal")
            try:
                outcome = self.run_model(turn, request, text)
            except ContextBudgetExceeded as exc:
                outcome = Outcome(str(exc), failed=True)
            except ModelUnavailable as exc:
                self.event(turn.task_id, "model_unavailable", {"message": str(exc)})
                outcome = Outcome(str(exc), failed=True, model_unavailable=True)
            except (PermissionError, sqlite3.Error):
                outcome = Outcome("本轮无法读取业务资料，请检查权限或数据服务。", failed=True)
            if turn.state.get("proposal") is previous_proposal:
                if previous_proposal and outcome.failed:
                    previous_proposal["requires_review"] = True
                else:
                    turn.state.pop("proposal", None)
            return self.finish(turn, request, text, outcome)
        finally:
            self.turn_lock.release()

    def run_model(self, turn: Turn, request: MessageRequest, text: str) -> Outcome:
        results = []
        for _ in range(6):
            context = self.context.build(ContextRequest(
                scope=turn.scope, task_id=turn.task_id, task_revision=turn.revision,
                task_state={**turn.state, "object_id": turn.object_id, "historical_read_only": turn.read_only}, user_input=text,
                tool_results=results, input_token_budget=self.budget,
            ))
            self.event(turn.task_id, "model_input", {"request_id": request.request_id, **context.model_dump(mode="json")})
            try:
                decision = self.model.decide(context)
            except InvalidDecision as exc:
                self.event(turn.task_id, "invalid_model_decision", {"message": str(exc)})
                results.append(failed_tool("invalid_argument", str(exc)))
                continue
            self.event(turn.task_id, "model_decision", decision.model_dump(mode="json"))
            if isinstance(decision, AnswerDecision):
                available = set(context.sources)
                citations = {encoded(rule.citation.model_dump()): rule_ref(rule)
                             for rule in self.rules.search(turn.scope, "")}
                if any(citations.get(encoded(citation.model_dump())) not in available for citation in decision.citations):
                    results.append(failed_tool("invalid_argument", "请重新查询并引用当前上下文中可核验的规则。"))
                    continue
                if decision.resolved_pending_question:
                    turn.state.pop("question", None)
                return Outcome(decision.reply, citations=decision.citations)
            if isinstance(decision, AskDecision):
                if not turn.read_only:
                    turn.state["question"] = decision.reply
                return Outcome(decision.reply)

            try:
                definition = TOOL_INPUTS.get(decision.name)
                if definition is None:
                    raise ValueError("工具不在允许列表中。")
                arguments = definition[0].model_validate(decision.arguments)
                if decision.name == "propose_invoice_update":
                    return self.propose(turn, request, arguments, context.sources)
                result = self.run_read_tool(turn.scope, decision.name, arguments)
            except PermissionError:
                result = failed_tool("forbidden", "当前用户无权执行此操作。")
            except KeyError:
                result = failed_tool("not_found", "当前范围内没有找到该记录。")
            except ValueError:
                result = failed_tool("invalid_argument", "工具参数无效，请依据当前范围修正或向用户追问。")
            except (sqlite3.Error, VerificationFailure):
                result = failed_tool("tool_failed", "业务查询失败，请勿把失败当成没有记录。")
            self.event(turn.task_id, "tool_result", {"name": decision.name, "result": result.model_dump(mode="json")})
            results.append(result)
        return Outcome("本轮已达到处理步数上限，请补充具体对象或拆分请求。", failed=True)

    def run_read_tool(self, scope, name, arguments) -> ToolResult:
        if name == "list_invoices":
            return self.reader.list_invoices(scope, arguments)
        if name == "list_bank_transactions":
            return self.reader.list_bank_transactions(scope, arguments)
        if name == "get_invoice":
            return self.reader.get_record(scope, arguments.invoice_id)
        if name == "check_invoice_purposes":
            return check_invoice_purposes(scope, arguments, self.reader, self.rules)
        if name == "get_operation":
            return self.writer.get_operation(scope, arguments.operation_id)
        hits = self.rules.search(scope, arguments.query)
        return ToolResult(status="succeeded", data={"rules": [hit.model_dump() for hit in hits]})

    def propose(self, turn, request, arguments, sources) -> Outcome:
        if turn.read_only:
            return Outcome("该企业还有其他月份的未完成任务，请先处理原任务。")
        if turn.object_id is not None and turn.object_id != arguments.invoice_id:
            raise ValueError("修改目标与任务绑定的发票不一致。")
        with self.database.connect() as connection:
            authorize(connection, turn.scope, write=True)
        record = self.reader.get_record(turn.scope, arguments.invoice_id).data
        patch = arguments.patch.model_dump(exclude_unset=True)
        proposal_id = hashlib.sha256(encoded([turn.scope.user_id, request.request_id]).encode()).hexdigest()
        turn.state["proposal"] = {
            "id": proposal_id, "session_id": request.session_id, "invoice_id": arguments.invoice_id,
            "expected_revision": record["revision"], "patch": patch,
            "sources": [source.model_dump() for source in sources],
        }
        turn.state.pop("question", None)
        self.event(turn.task_id, "save_proposal", turn.state["proposal"])
        return self.describe_proposal(turn)

    def describe_proposal(self, turn: Turn) -> Outcome:
        proposal = turn.state["proposal"]
        patch = proposal["patch"]
        changes = "，".join(f"{label}改为{encoded(patch[key]) if patch[key] is not None else '空'}"
                          for key, label in (("purpose", "用途"), ("note", "备注")) if key in patch)
        return Outcome(f"拟修改 {turn.scope.enterprise_id} {turn.scope.month} 发票 {proposal['invoice_id']}：{changes}。请回复‘确认保存’或‘取消保存’。")

    def resume(self, turn: Turn, request: MessageRequest) -> Outcome:
        if turn.read_only:
            return Outcome(f"该企业还有 {turn.active_month} 的未完成任务，请切回该月份继续。")
        unresolved = self.unresolved_outcome(turn)
        if unresolved:
            return unresolved
        proposal = turn.state.get("proposal")
        if proposal:
            operation_id = "turn-op-" + proposal["id"]
            try:
                recorded = self.writer.get_operation(turn.scope, operation_id)
            except KeyError:
                recorded = None
            except (sqlite3.Error, VerificationFailure, PermissionError):
                return Outcome(f"无法核验原保存，请查看操作 {operation_id}。", operation_ids=[operation_id], blocked=True)
            if recorded is not None:
                return self.readback(turn, recorded)
            proposal["session_id"] = request.session_id
            proposal.pop("requires_review", None)
            self.event(turn.task_id, "task_resumed", {"session_id": request.session_id, "proposal_id": proposal["id"]})
            return self.describe_proposal(turn)
        if turn.state.get("question"):
            return Outcome(f"已恢复 {turn.scope.enterprise_id} {turn.scope.month} 的任务。{turn.state['question']}")
        return Outcome("当前范围没有待补充事项。")

    def unresolved_outcome(self, turn: Turn) -> Outcome | None:
        with self.database.connect() as connection:
            unresolved = connection.execute(
                "SELECT operation_id FROM operations WHERE task_id=? AND status IN (?,?,?) ORDER BY operation_id",
                (turn.task_id, *UNRESOLVED),
            ).fetchall()
        if unresolved:
            operation_ids = [row["operation_id"] for row in unresolved]
            return Outcome(f"保存结果待核验，请查看操作 {'、'.join(operation_ids)}。",
                           operation_ids=operation_ids, blocked=True)
        return None

    def recover_saved(self, turn: Turn, operation_id: str, *, cancel: bool) -> Outcome:
        try:
            recorded = self.writer.get_operation(turn.scope, operation_id)
        except (sqlite3.Error, VerificationFailure, PermissionError, KeyError):
            return Outcome(f"无法核验原保存，请查看操作 {operation_id}。", operation_ids=[operation_id], blocked=True)
        self.event(turn.task_id, "turn_recovered", {"operation_id": operation_id, "status": recorded.status})
        return self.readback(turn, recorded, cancel=cancel)

    def readback(self, turn: Turn, saved: ToolResult, *, cancel: bool = False) -> Outcome:
        operation_id = saved.operation_id
        if saved.status in {"pending", "unknown"}:
            return Outcome(f"保存结果待核验，请查看操作 {operation_id}。", operation_ids=[operation_id], blocked=True)
        proposal = turn.state.get("proposal")
        matches = proposal and "turn-op-" + proposal["id"] == operation_id
        if saved.status == "failed":
            if matches:
                turn.state.pop("proposal")
            return Outcome(f"原保存操作失败，请查看操作 {operation_id} 并重新说明修改。", operation_ids=[operation_id], failed=True)
        try:
            current = self.reader.get_record(turn.scope, saved.object_id).data
        except (PermissionError, KeyError, sqlite3.Error):
            return Outcome(f"原保存已确认，本轮回查失败，请查看操作 {operation_id}。",
                           operation_ids=[operation_id], blocked=True, failed=True)
        if matches:
            turn.state.pop("proposal")
        if current != saved.data["record"]:
            return Outcome(f"原操作已确认，当前记录有变化，请查看操作 {operation_id}。", operation_ids=[operation_id])
        self.event(turn.task_id, "save_readback", {"operation_id": operation_id, "record": current})
        reply = "原操作已保存并回查，无法通过取消撤回。" if cancel else f"发票 {saved.object_id} 已保存并回查确认。"
        return Outcome(reply, operation_ids=[operation_id])

    def confirm(self, turn: Turn, request: MessageRequest, *, cancel: bool) -> Outcome:
        unresolved = self.unresolved_outcome(turn)
        if unresolved:
            return unresolved
        proposal = turn.state.get("proposal")
        if not proposal or turn.read_only:
            return Outcome("当前范围没有待确认的修改，请说明发票和要补充的内容。")
        if proposal["session_id"] != request.session_id:
            return Outcome("请先发送‘继续任务’查看原修改，再确认保存。")
        if proposal.get("requires_review") and not cancel:
            return Outcome("待确认内容需要重新核对，请先发送‘继续任务’。")
        operation_id = "turn-op-" + proposal["id"]
        try:
            try:
                recorded = self.writer.get_operation(turn.scope, operation_id)
            except KeyError:
                recorded = None
            if recorded is None and cancel:
                turn.state.pop("proposal")
                return Outcome("已取消本次待确认修改。")
            if recorded is None:
                for item in proposal["sources"]:
                    reference = RevisionRef.model_validate(item)
                    store = self.memories if reference.kind == "memory" else self.rules
                    if not store.is_current(turn.scope, reference):
                        turn.state.pop("proposal")
                        return Outcome("修改所用依据已变化，请重新说明并确认修改。", failed=True)
                saved = self.writer.update_fields(turn.scope, proposal["invoice_id"], InvoiceSaveRequest(
                    task_id=turn.task_id, session_id=request.session_id, request_id="turn-save-" + proposal["id"],
                    operation_id=operation_id, expected_revision=proposal["expected_revision"], patch=proposal["patch"],
                ))
            else:
                saved = recorded
            return self.readback(turn, saved, cancel=cancel)
        except (PermissionError, KeyError, RevisionConflict, ValueError):
            turn.state.pop("proposal", None)
            return Outcome("当前权限、任务或发票状态不支持保存，请重新读取并确认。", failed=True)
        except (sqlite3.Error, VerificationFailure, TimeoutError):
            proposal["requires_review"] = True
            return Outcome(f"本轮无法确认保存，请核对操作 {operation_id}。",
                           operation_ids=[operation_id], blocked=True, failed=True)

    def finish(self, turn, request, text, outcome):
        with self.database.write() as connection:
            task = connection.execute("SELECT status,revision FROM tasks WHERE task_id=?", (turn.task_id,)).fetchone()
            pending = []
            if task["revision"] != turn.revision:
                status = TaskStatus(task["status"])
                if not outcome.operation_ids:
                    outcome.reply = "任务状态已变化，请重新读取任务和相关操作。"
                    outcome.failed = True
            elif turn.read_only:
                status = TaskStatus.COMPLETED
            else:
                unresolved = connection.execute(
                    "SELECT 1 FROM operations WHERE task_id=? AND status IN (?,?,?)", (turn.task_id, *UNRESOLVED),
                ).fetchone()
                if outcome.blocked or unresolved:
                    status = TaskStatus.BLOCKED
                    pending.append("核验保存操作")
                elif turn.state.get("proposal"):
                    status = TaskStatus.WAITING_USER
                    pending.append("确认或取消保存")
                elif turn.state.get("question"):
                    status = TaskStatus.WAITING_USER
                    pending.append(turn.state["question"])
                elif outcome.model_unavailable:
                    status = TaskStatus.RUNNING
                else:
                    status = TaskStatus.COMPLETED
                connection.execute("UPDATE tasks SET status=?,state_json=?,revision=revision+1 WHERE task_id=?",
                                   (status, encoded(self.audit_payload(turn.state)), turn.task_id))
            response = TurnResponse(request_id=request.request_id, task_id=turn.task_id, task_status=status,
                                    reply=self.audit_text(outcome.reply), pending_items=[self.audit_text(item) for item in pending],
                                    operation_ids=outcome.operation_ids, citations=outcome.citations)
            connection.executemany(
                "INSERT INTO messages(message_id,task_id,session_id,request_id,role,content) VALUES (?,?,?,?,?,?)",
                [(uuid4().hex, turn.task_id, request.session_id, request.request_id, "user", text),
                 (uuid4().hex, turn.task_id, request.session_id, request.request_id, "assistant", response.reply)],
            )
            connection.execute("UPDATE requests SET status=?,response_json=? WHERE user_id=? AND request_id=?",
                               ("failed" if outcome.failed else "completed", response.model_dump_json(), turn.scope.user_id, request.request_id))
            return response
