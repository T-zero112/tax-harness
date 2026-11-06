import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tax_harness.app import create_app
from tax_harness.contracts import Citation, ContextBundle, ContextRequest, MessageRequest, Scope, ToolResult
from tax_harness.model_adapter import (
    AnswerDecision, AskDecision, ChatCompletionsModel, ModelUnavailable, ToolDecision,
)

HEADERS = {"X-Demo-User": "U001"}
PARAMS = {"enterprise_id": "C001", "month": "2026-09"}
ALPHA = Scope(user_id="U001", **PARAMS)


class ScriptedModel:
    def __init__(self, *decisions):
        self.decisions = iter(decisions)
        self.inputs = []

    def decide(self, context):
        self.inputs.append(context)
        decision = next(self.decisions)
        if isinstance(decision, Exception):
            raise decision
        return decision(context) if callable(decision) else decision


def send(client, request_id, text, *, session="s1", headers=HEADERS, **scope):
    return client.post("/api/messages", headers=headers, json={
        **PARAMS, **scope, "session_id": session, "request_id": request_id, "text": text,
    })


def seed(app):
    with app.state.database.write() as connection:
        connection.execute(
            "INSERT INTO invoices(enterprise_id,invoice_id,invoice_date,seller,item_name,amount_cents) "
            "VALUES ('C001','F001','2026-09-01','供方','办公资料',50100)"
        )
        connection.execute(
            "INSERT INTO rules(doc_id,version,enterprise_id,entry,name,effective_from,content,active) "
            "VALUES ('R002','2.0','C001','main','甲企业发票用途规则','2026-09-01','发票用途需填写',1)"
        )


def read_invoice(client):
    return client.get("/api/invoices/F001", params=PARAMS, headers=HEADERS).json()["data"]


def proposal():
    return ToolDecision(kind="tool", name="propose_invoice_update",
                        arguments={"invoice_id": "F001", "patch": {"purpose": "办公资料"}})


def test_model_driven_query_question_proposal_confirmation_and_readback(tmp_path):
    citation = Citation(doc_id="R002", name="甲企业发票用途规则", version="2.0", entry="main")

    def answer(context):
        assert any('"invoice_cents":50100' in item["content"] for item in context.messages)
        assert any(source.kind == "rule" for source in context.sources)
        return AnswerDecision(kind="answer", reply="发票共 501.00 元。", citations=[citation])

    def supplement(context):
        assert any(item["content"] == "请说明 F001 的用途。" for item in context.messages)
        return proposal()

    model = ScriptedModel(ToolDecision(kind="tool", name="list_invoices", arguments={}),
                          ToolDecision(kind="tool", name="search_rules", arguments={"query": "发票用途"}),
                          answer, AskDecision(kind="ask", reply="请说明 F001 的用途。"), supplement)
    app = create_app(tmp_path / "agent.sqlite3", model=model)
    with TestClient(app) as client:
        seed(app)
        queried = send(client, "q", "查询发票金额和用途规则")
        assert queried.status_code == 200 and queried.json()["citations"] == [citation.model_dump()]
        asked = send(client, "ask", "帮我补充 F001")
        assert asked.json()["task_status"] == "waiting_user"
        proposed = send(client, "propose", "用途是办公资料")
        assert proposed.json()["task_id"] == asked.json()["task_id"]
        assert "确认保存" in proposed.json()["reply"]
        assert read_invoice(client)["purpose"] is None
        assert send(client, "other", "确认保存", session="s2").json()["operation_ids"] == []
        confirmed = send(client, "confirm", "确认保存")
        assert confirmed.status_code == 200 and "回查确认" in confirmed.json()["reply"]
        assert confirmed.json()["task_status"] == "completed"
        assert read_invoice(client)["purpose"] == "办公资料"
        assert send(client, "confirm", "确认保存").json() == confirmed.json()
        assert send(client, "confirm", "取消保存").status_code == 409
        assert len(model.inputs) == 5
        with app.state.database.connect() as connection:
            assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
            kinds = {row[0] for row in connection.execute("SELECT kind FROM events")}
            assert {"model_input", "tool_result", "invoice_saved", "save_readback"} <= kinds


def test_memory_filtering_rule_revision_and_stale_confirmation(tmp_path):
    model = ScriptedModel(AnswerDecision(kind="answer", reply="已收到偏好。"), proposal())
    app = create_app(tmp_path / "memory.sqlite3", model=model)
    with TestClient(app) as client:
        seed(app)
        send(client, "origin", "以后先汇总再列明细")
        with app.state.database.connect() as connection:
            message_id = connection.execute("SELECT message_id FROM messages WHERE role='user'").fetchone()[0]
        store = app.state.memory_store
        memory = store.create(ALPHA, "先汇总再列明细", message_id)
        store.correct(ALPHA, memory.memory_id, "先列明细再汇总", 1)
        proposed = send(client, "proposal", "请把 F001 的发票用途填写为办公资料")
        assert "确认保存" in proposed.json()["reply"]
        context = model.inputs[-1]
        assert message_id in context.omitted_message_ids
        assert any("先列明细再汇总" in item["content"] for item in context.messages)
        assert not any("以后先汇总再列明细" in item["content"] for item in context.messages)
        app.state.rule_store.deactivate(ALPHA, "R002", "2.0", 1)
        response = send(client, "confirm", "确认保存")
        assert "依据已变化" in response.json()["reply"]
        assert read_invoice(client)["purpose"] is None


def test_invalid_tools_are_bounded_and_read_only_scope_is_enforced(tmp_path):
    model = ScriptedModel(AskDecision(kind="ask", reply="请补充用途。"), proposal(),
                          *[ToolDecision(kind="tool", name="write_sql", arguments={})] * 6)
    app = create_app(tmp_path / "bounded.sqlite3", model=model)
    with TestClient(app) as client:
        seed(app)
        assert send(client, "first", "补充发票").json()["task_status"] == "waiting_user"
        historical = send(client, "other-month", "修改发票", month="2026-08")
        assert "其他月份" in historical.json()["reply"]
        assert send(client, "bad", "执行未知工具").json()["reply"].startswith("本轮已达到")
        assert read_invoice(client)["purpose"] is None
        assert len(model.inputs) == 8
        assert send(client, "denied", "查询", headers={"X-Demo-User": "U002"}, enterprise_id="C002").status_code == 403


def test_intervening_model_failure_invalidates_unconfirmed_proposal(tmp_path):
    model = ScriptedModel(proposal(), ModelUnavailable("模型请求失败或超时，请稍后重试。"))
    app = create_app(tmp_path / "timeout.sqlite3", model=model)
    with TestClient(app) as client:
        seed(app)
        assert "确认保存" in send(client, "propose", "请将用途改为办公资料").json()["reply"]
        assert "超时" in send(client, "change", "改为差旅").json()["reply"]
        assert send(client, "confirm", "确认保存").json()["operation_ids"] == []
        assert read_invoice(client)["purpose"] is None


def test_unconfigured_model_and_missing_month(tmp_path, monkeypatch):
    for key in ("HARNESS_MODEL_ENDPOINT", "HARNESS_MODEL_NAME", "HARNESS_MODEL_KEY"):
        monkeypatch.delenv(key, raising=False)
    with TestClient(create_app(tmp_path / "unconfigured.sqlite3")) as client:
        assert send(client, "q", "查询").status_code == 503
    model = ScriptedModel()
    with TestClient(create_app(tmp_path / "missing.sqlite3", model=model)) as client:
        assert send(client, "q", "查询", month=None).status_code == 400
        assert not model.inputs


def test_http_adapter_uses_messages_and_excludes_credentials_from_audit(tmp_path):
    key = "fixture-key-should-be-redacted"
    sent = []

    def provider(request):
        assert request.headers["authorization"] == f"Bearer {key}"
        body = json.loads(request.content)
        assert body["model"] == "fixture-model"
        assert key not in request.content.decode()
        sent.append(body["messages"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "kind": "answer", "reply": f"已处理 {key}",
        })}}]})

    model = ChatCompletionsModel("https://model.test/v1/chat/completions", "fixture-model", key,
                                 transport=httpx.MockTransport(provider))
    app = create_app(tmp_path / "adapter.sqlite3", model=model)
    with TestClient(app) as client:
        response = send(client, "q", f"测试消息 {key}")
        assert response.status_code == 200 and key not in response.text
        with app.state.database.connect() as connection:
            events = connection.execute("SELECT kind,payload_json FROM events").fetchall()
            assert all(key not in row["payload_json"] for row in events)
            model_input = next(json.loads(row["payload_json"]) for row in events if row["kind"] == "model_input")
            assert model_input["messages"] == sent[0]


def test_http_adapter_maps_transport_timeout():
    def timeout(request):
        raise httpx.ReadTimeout("fixture timeout", request=request)

    model = ChatCompletionsModel("https://model.test/v1/chat/completions", "fixture", "fixture-key",
                                 transport=httpx.MockTransport(timeout))
    context = ContextBundle(messages=[], tools=[], sources=[], omitted_message_ids=[], input_tokens=0,
                            token_count_method="test")
    with pytest.raises(ModelUnavailable):
        model.decide(context)


def test_context_budget_preserves_scope_pending_and_totals(tmp_path):
    model = ScriptedModel(AskDecision(kind="ask", reply="请补充 F001 的用途。"))
    app = create_app(tmp_path / "budget.sqlite3", model=model)
    with TestClient(app) as client:
        response = send(client, "first", "帮我补充 F001").json()
        with app.state.database.write() as connection:
            connection.execute(
                "INSERT INTO messages(message_id,task_id,session_id,role,content) VALUES ('long',?,'s1','user',?)",
                (response["task_id"], "旧内容" * 2000),
            )
        request = ContextRequest(scope=ALPHA, task_id=response["task_id"], task_revision=2,
                                 task_state={"question": "请补充 F001 的用途。"}, user_input="这张票用于办公",
                                 input_token_budget=12000)
        builder = app.state.turn_executor.context
        baseline = builder.build(request)
        result = ToolResult(status="succeeded", data={"invoice_cents": 50000, "records": [
            {"invoice_id": f"F{index:03}", "note": "明细备注" * 35} for index in range(40)
        ]})
        budget = baseline.input_tokens + 1300
        context = builder.build(request.model_copy(update={"input_token_budget": budget, "tool_results": [result]}))
        assert context.input_tokens <= budget
        assert context.token_count_method == "serialized_utf8_bytes_estimate"
        assert "long" in context.omitted_message_ids
        assert any('"enterprise_id":"C001"' in item["content"] and "F001" in item["content"] for item in context.messages)
        assert any('"invoice_cents":50000' in item["content"] and '"omitted_records":38' in item["content"]
                   for item in context.messages)


def active_task(client, **params):
    return client.get("/api/tasks/active", params={**PARAMS, **params}, headers=HEADERS).json()["task"]


def test_resume_after_restart_requires_new_session_to_review(tmp_path):
    db = tmp_path / "resume.sqlite3"
    app = create_app(db, model=ScriptedModel(proposal()))
    with TestClient(app) as client:
        seed(app)
        first = send(client, "propose", "将 F001 用途改为办公资料").json()
    with TestClient(create_app(db, model=ScriptedModel())) as client:
        assert active_task(client)["task_id"] == first["task_id"]
        wrong = send(client, "unreviewed", "确认保存", session="new").json()
        assert not wrong["operation_ids"]
        resumed = send(client, "resume", "继续任务", session="new", month=None).json()
        assert resumed["task_id"] == first["task_id"]
        assert "F001" in resumed["reply"] and "办公资料" in resumed["reply"]
        assert read_invoice(client)["purpose"] is None
        assert not send(client, "old-session", "确认保存").json()["operation_ids"]
        saved = send(client, "reviewed", "确认保存", session="new").json()
        assert saved["task_id"] == first["task_id"] and saved["task_status"] == "completed"
        assert read_invoice(client)["purpose"] == "办公资料"


def test_enterprise_and_month_switch_keep_separate_tasks_and_context(tmp_path):
    def check_context(context):
        assert not any("甲企业专属待办" in message["content"] for message in context.messages)
        return AskDecision(kind="ask", reply="乙企业专属待办")

    model = ScriptedModel(AskDecision(kind="ask", reply="甲企业专属待办"), check_context,
                          AnswerDecision(kind="answer", reply="已查询八月资料。"),
                          AskDecision(kind="ask", reply="八月待办"))
    app = create_app(tmp_path / "switch.sqlite3", model=model)
    with TestClient(app) as client:
        alpha = send(client, "alpha", "记录甲企业待办").json()
        beta = send(client, "beta", "处理乙企业", enterprise_id="C002").json()
        assert beta["task_id"] != alpha["task_id"]
        old = active_task(client, month="2026-08")
        assert old["task_id"] == alpha["task_id"] and not old["matches_month"]
        historical = send(client, "history", "只查询八月", month="2026-08").json()
        assert historical["task_status"] == "completed"
        assert active_task(client)["task_id"] == alpha["task_id"]
        assert "切回" in send(client, "wrong-month", "继续任务", month="2026-08").json()["reply"]
        assert "甲企业专属待办" in send(client, "resume", "继续任务", session="new").json()["reply"]
        assert active_task(client, enterprise_id="C002")["task_id"] == beta["task_id"]
        assert client.get("/api/tasks/active", params={**PARAMS, "enterprise_id": "C002"},
                          headers={"X-Demo-User": "U002"}).status_code == 403
        revision = active_task(client)["revision"]
        assert client.patch(f"/api/tasks/{alpha['task_id']}/status", params=PARAMS, headers=HEADERS,
                            json={"status": "cancelled", "expected_revision": revision}).status_code == 200
        august = send(client, "new-month", "开始八月工作", month="2026-08").json()
        assert august["task_status"] == "waiting_user"
        assert active_task(client)["month"] == "2026-08"
        with app.state.database.connect() as connection:
            assert connection.execute("SELECT count(*) FROM tasks WHERE status='waiting_user'").fetchone()[0] == 2


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_confirmation_recovery_never_duplicates_write(tmp_path, committed):
    path = tmp_path / "restart.sqlite3"
    app = create_app(path, model=ScriptedModel(proposal()))
    confirmation = MessageRequest(**PARAMS, session_id="s1", request_id="confirm", text="确认保存")
    with TestClient(app) as client:
        seed(app)
        send(client, "propose", "保存 F001 的用途")
        if committed:
            with app.state.database.write() as connection:
                connection.execute("CREATE TRIGGER lose_reply BEFORE UPDATE ON requests WHEN OLD.request_id='confirm' "
                                   "BEGIN SELECT RAISE(ABORT,'interrupted finalization'); END")
            assert send(client, "confirm", "确认保存").status_code == 500
            assert read_invoice(client)["revision"] == 2
            with app.state.database.write() as connection:
                connection.execute("DROP TRIGGER lose_reply")
        else:
            app.state.turn_executor.start("U001", confirmation)
            assert read_invoice(client)["revision"] == 1
    with TestClient(create_app(path, model=ScriptedModel())) as client:
        recovered = send(client, "confirm", "确认保存")
        assert recovered.status_code == 200
        assert "回查确认" in recovered.json()["reply"]
        assert send(client, "confirm", "确认保存").json() == recovered.json()
        assert read_invoice(client)["revision"] == 2
    with app.state.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM events WHERE kind='invoice_saved'").fetchone()[0] == 1


@pytest.mark.parametrize("evidence", ["missing", "matching", "mismatch"])
def test_unknown_reconciliation_requires_original_receipt_and_matching_readback(tmp_path, evidence):
    app = create_app(tmp_path / "unknown.sqlite3", model=ScriptedModel(proposal()))
    with TestClient(app) as client:
        seed(app)
        send(client, "propose", "保存 F001 的用途")
        saved = send(client, "confirm", "确认保存").json()
        operation_id = saved["operation_ids"][0]
        with app.state.database.write() as connection:
            connection.execute("UPDATE tasks SET status='blocked' WHERE task_id=?", (saved["task_id"],))
            connection.execute("UPDATE operations SET status='unknown',result_json=NULL,verification_json=NULL WHERE operation_id=?",
                               (operation_id,))
            if evidence == "missing":
                connection.execute("UPDATE requests SET response_json=NULL WHERE request_id=(SELECT request_id FROM operations WHERE operation_id=?)",
                                   (operation_id,))
            if evidence == "mismatch":
                connection.execute("UPDATE invoices SET note='外部变化' WHERE enterprise_id='C001' AND invoice_id='F001'")
        resumed = send(client, "resume", "继续任务", session="new").json()
        assert resumed["task_status"] == "blocked" and resumed["operation_ids"] == [operation_id]
        assert active_task(client)["unresolved_operations"] == [{"operation_id": operation_id, "status": "unknown"}]
        assert send(client, "cannot-cancel", "取消保存", session="new").json()["operation_ids"] == [operation_id]
        path = f"/api/operations/{operation_id}/reconcile"
        assert client.post(path, params=PARAMS, headers={"X-Demo-User": "U002"}).status_code == 403
        status = client.post(path, params=PARAMS, headers=HEADERS).json()["status"]
        assert status == ("succeeded" if evidence == "matching" else "unknown")
        task = active_task(client)
        closed = client.patch(f"/api/tasks/{task['task_id']}/status", params=PARAMS, headers=HEADERS,
                              json={"status": "cancelled", "expected_revision": task["revision"]})
        assert closed.status_code == (200 if evidence == "matching" else 409)
        assert read_invoice(client)["revision"] == 2


def test_timeout_preserves_pending_question_across_sessions(tmp_path):
    model = ScriptedModel(AskDecision(kind="ask", reply="请补充 F001 的用途。"),
                          ModelUnavailable("模型请求失败或超时，请稍后重试。"))
    app = create_app(tmp_path / "timeout-question.sqlite3", model=model)
    with TestClient(app) as client:
        first = send(client, "ask", "补充 F001").json()
        failed = send(client, "fail", "办公用途").json()
        assert "超时" in failed["reply"] and failed["task_status"] == "waiting_user"
        assert send(client, "fail", "办公用途").json() == failed
        resumed = send(client, "resume", "继续任务", session="new").json()
        assert resumed["task_id"] == first["task_id"]
        assert "请补充 F001 的用途" in resumed["reply"]
        assert len(model.inputs) == 2
