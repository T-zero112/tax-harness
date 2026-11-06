"""Acceptance scenarios run against disposable copies of the B import."""

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tax_harness.app import create_app
from tax_harness.contracts import ContextRequest, Scope, ToolResult
from tax_harness.model_adapter import AnswerDecision, AskDecision, ChatCompletionsModel, ModelUnavailable, ToolDecision
from test_agent import HEADERS, PARAMS, ScriptedModel, send

SOURCE = os.getenv("HARNESS_ACCEPTANCE_DB")
pytestmark = pytest.mark.skipif(not SOURCE, reason="需要 HARNESS_ACCEPTANCE_DB 指向 B 的 v1.1.0 数据库")


@pytest.fixture
def database_path(tmp_path):
    source = Path(SOURCE).resolve(strict=True)
    path = tmp_path / "acceptance.sqlite3"
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader, closing(sqlite3.connect(path)) as writer:
        reader.backup(writer)
    return path


def request(client, path, method="GET", body=None, *, user="U001", **params):
    return client.request(method, path, params={**PARAMS, **params}, headers={"X-Demo-User": user}, json=body)


def invoice(client, invoice_id="F002", **params):
    return request(client, f"/api/invoices/{invoice_id}", **params).json()["data"]


def call_tool(name, **arguments):
    return ToolDecision(kind="tool", name=name, arguments=arguments)


def tool_data(context):
    frames = [json.loads(message["content"]) for message in context.messages
              if message["content"].startswith('{"kind":"工具结果"')]
    assert frames
    return frames[-1]["data"]


def save_body(label, revision, patch, *, session="s1", task_id="a6-task"):
    return {"task_id": task_id, "session_id": session, "request_id": f"a6-r-{label}", "operation_id": f"a6-op-{label}",
            "expected_revision": revision, "patch": patch}


def test_B01_B26_B27_scope_totals_and_empty_failure(database_path):
    app = create_app(database_path, model=ScriptedModel())
    with TestClient(app) as client:
        expected = {("C001", "2026-08"): (904635, 572099, 1485075), ("C001", "2026-09"): (962206, 718074, 1300050),
                    ("C002", "2026-08"): (1366705, 934110, 1880025), ("C002", "2026-09"): (1266724, 808724, 1370075)}
        for (enterprise, month), amounts in expected.items():
            filters = {"enterprise_id": enterprise, "month": month}
            invoices = request(client, "/api/invoices", **filters).json()["data"]
            bank = request(client, "/api/bank-transactions", **filters).json()["data"]
            assert (invoices["invoice_count"], bank["bank_count"]) == (10, 12)
            assert (invoices["invoice_cents"], bank["expense_cents"], bank["income_cents"]) == amounts
            assert all(row["enterprise_id"] == enterprise and row["invoice_date"].startswith(month) for row in invoices["records"])
            assert all(row["enterprise_id"] == enterprise and row["transaction_date"].startswith(month) for row in bank["records"])
        empty = request(client, "/api/invoices", month="2026-07").json()
        assert empty["status"] == "succeeded" and empty["data"]["invoice_cents"] == 0
        bank = request(client, "/api/bank-transactions", month="2026-07").json()["data"]
        assert bank == {"records": [], "bank_count": 0, "income_cents": 0, "expense_cents": 0}
        denied = request(client, "/api/invoices", user="U002", enterprise_id="C002")
        assert denied.status_code == 403 and "data" not in denied.json()
        missing = send(client, "missing", "查询发票", month=None)
        assert missing.status_code == 400 and "月份" in missing.json()["message"]
        with app.state.database.write() as connection:
            connection.execute("ALTER TABLE invoices RENAME TO injected_failure")
        failed = request(client, "/api/invoices")
        assert failed.status_code == 500 and failed.json()["code"] == "tool_failed" and "data" not in failed.json()


def test_B02_B03_B28_B29_purpose_rules_and_missing_evidence(database_path):
    def purpose_reply(ids, version, doc_id):
        def answer(context):
            data = tool_data(context)["data"]
            assert {record["invoice_id"] for record in data["records"]} == ids
            citation = data["rules"][0]["citation"]
            assert (citation["doc_id"], citation["version"]) == (doc_id, version)
            return AnswerDecision(kind="answer", reply="缺项 " + "、".join(sorted(ids)), citations=[citation])
        return answer

    def missing(context):
        assert tool_data(context)["data"]["rules"] == []
        return AnswerDecision(kind="answer", reply="现有材料没有依据，请提供对应的规则文档。")

    def august(context):
        assert tool_data(context)["status"] == "failed" and "9 月" in tool_data(context)["error"]["message"]
        return AnswerDecision(kind="answer", reply="八月可查金额，用途检查限于九月。")

    model = ScriptedModel(call_tool("check_invoice_purposes"), purpose_reply({"F002", "F003", "F004", "F006", "F009"}, "2.0", "R002"),
                          call_tool("check_invoice_purposes"), purpose_reply({"F002", "F005", "F006"}, "1.0", "R003"),
                          call_tool("check_invoice_purposes"), august,
                          call_tool("search_rules", query="unknown-fixture-topic"), missing)
    app = create_app(database_path, model=model)
    with TestClient(app) as client:
        with app.state.database.write() as connection:
            connection.execute("UPDATE invoices SET purpose='  ',note='备注不能替代用途' WHERE enterprise_id='C002' AND invoice_id='F002'")
        alpha = send(client, "alpha", "检查甲企业用途").json()
        assert alpha["citations"][0]["version"] == "2.0"
        beta = send(client, "beta", "检查乙企业用途", enterprise_id="C002").json()
        assert beta["citations"][0]["entry"] == "main"
        assert "限于九月" in send(client, "august", "检查八月用途", month="2026-08").json()["reply"]
        unsupported = send(client, "unsupported", "查询未提供的规则").json()
        assert "请提供" in unsupported["reply"] and not unsupported["citations"]


def test_B04_B05_B06_B11_B31_B32_interrupted_question_and_save(database_path):
    def check_pending(context):
        assert any("请指定 F002 或 F003" in item["content"] for item in context.messages)
        assert any("甲企业先汇总" in item["content"] for item in context.messages)
        return call_tool("propose_invoice_update", invoice_id="F002", patch={"purpose": "A6 明确用途", "note": "A6 核验备注"})

    def beta_question(context):
        assert all("甲企业先汇总" not in item["content"] for item in context.messages)
        return AskDecision(kind="ask", reply="请在乙企业明确发票编号。")

    def bank_answer(context):
        assert tool_data(context)["data"]["income_cents"] == 1300050
        return AnswerDecision(kind="answer", reply="银行收入 13000.50 元。")

    def invoice_answer(context):
        assert tool_data(context)["data"]["invoice_count"] > 0
        return AnswerDecision(kind="answer", reply="已按票面项目定位。现有材料无法确认已付款，实际用途需要你补充。")

    model = ScriptedModel(call_tool("check_invoice_purposes"), AskDecision(kind="ask", reply="请指定 F002 或 F003 的用途。"),
                          AskDecision(kind="ask", reply="两张候选发票，请指定编号。"),
                          call_tool("list_bank_transactions"), bank_answer,
                          beta_question, AnswerDecision(kind="answer", reply="八月查询已完成。"), check_pending,
                          call_tool("list_invoices", item_name="打印"), invoice_answer)
    with TestClient(create_app(database_path, model=model)) as client:
        initial = invoice(client)
        started = send(client, "start", "查缺用途并帮我补充，请记住甲企业先汇总").json()
        assert started["task_status"] == "waiting_user"
        source_id = request(client, "/api/workbench/records").json()["source_messages"][0]["message_id"]
        assert request(client, "/api/memories", "POST", {"content": "甲企业先汇总", "source_message_id": source_id}).status_code == 201
        assert send(client, "ambiguous", "用途为资料").json()["task_status"] == "waiting_user"
        assert invoice(client) == initial
        inserted = send(client, "bank", "插问银行收入").json()
        assert inserted["task_id"] == started["task_id"] and inserted["task_status"] == "waiting_user"
        assert inserted["pending_items"] == ["两张候选发票，请指定编号。"]
        beta = send(client, "switch", "补充用途", enterprise_id="C002").json()
        assert beta["task_id"] != started["task_id"] and not beta["operation_ids"]
        history = send(client, "history", "查八月资料", month="2026-08").json()
        assert history["task_status"] == "completed" and history["task_id"] != started["task_id"]
        assert request(client, "/api/tasks/active", month="2026-08").json()["task"]["task_id"] == started["task_id"]
        resumed = send(client, "resume", "继续任务", session="new").json()
        assert resumed["task_id"] == started["task_id"]
        proposed = send(client, "propose", "F002 补充用途和备注", session="new").json()
        assert proposed["task_id"] == started["task_id"] and "F002" in proposed["reply"]
        assert invoice(client) == initial
        assert not send(client, "wrong-session", "确认保存").json()["operation_ids"]
        saved = send(client, "confirm", "确认保存", session="new").json()
        operation = request(client, f"/api/operations/{saved['operation_ids'][0]}").json()["data"]
        assert operation["record"] == invoice(client)
        assert operation["verification"]["before"] == {"purpose": initial["purpose"], "note": initial["note"]}
        assert operation["verification"]["after"] == {"purpose": "A6 明确用途", "note": "A6 核验备注"}
        assert invoice(client, enterprise_id="C002")["purpose"] is None
        reply = send(client, "locate", "按打印项目查票，能确认付款吗").json()
        assert "无法确认已付款" in reply["reply"] and not reply["operation_ids"]


def test_B07_B12_B17_B30_rejected_tool_fields_and_access(database_path):
    decisions = iter(["broken json", call_tool("unknown_tool").model_dump_json(),
                      call_tool("propose_invoice_update", invoice_id="F002", patch={"amount_cents": 1}).model_dump_json(),
                      AskDecision(kind="ask", reply="仅可填写用途或备注，请补充允许的内容。").model_dump_json()])
    adapter = ChatCompletionsModel("https://fixture.test/chat", "fixture", "fixture-a6-key",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": [{"message": {"content": next(decisions)}}]})))
    app = create_app(database_path, model=adapter)
    with TestClient(app) as client:
        initial = invoice(client)
        body = save_body("denied", initial["revision"], {"purpose": "拒绝"})
        assert request(client, "/api/invoices/F002", "PATCH", body, user="U002").status_code == 403
        assert request(client, "/api/invoices/F002", "PATCH", body, month="2026-08").status_code == 404
        for field in ("amount_cents", "invoice_date", "invoice_id", "enterprise_id", "seller", "item_name"):
            invalid = request(client, "/api/invoices/F002", "PATCH", {**body, "patch": {field: "非法"}})
            assert invalid.status_code == 422
        assert request(client, "/api/invoices/F002", "PATCH", {**body, "patch": {"purpose": 1}}).status_code == 422
        for method in ("POST", "PATCH", "DELETE"):
            assert request(client, "/api/bank-transactions", method).status_code == 405
        response = send(client, "bad-tool", "尝试非法工具").json()
        assert response["task_status"] == "waiting_user" and not response["operation_ids"]
        assert invoice(client) == initial
        records = request(client, "/api/workbench/records").json()
        events = [(event["kind"], json.loads(event["payload_json"])) for event in records["events"]]
        assert any(kind == "invalid_model_decision" for kind, _ in events)
        assert sum(kind == "tool_result" and value["result"]["error"]["code"] == "invalid_argument" for kind, value in events) == 2
        task_id = response["task_id"]
        assert request(client, f"/api/tasks/{task_id}", user="U002").status_code == 404
        assert request(client, "/api/workbench/records", user="U002").json()["events"] == []
        assert request(client, "/api/workbench/records", user="U002", enterprise_id="C002").status_code == 403


def test_B08_B16_operation_states_are_presented_without_rewriting(database_path):
    def answer(expected, text):
        def decide(context):
            assert tool_data(context)["status"] == expected
            return AnswerDecision(kind="answer", reply=text)
        return decide

    statuses = [("confirmed", "succeeded", "保存已核验。"), ("dispatched", "pending", "仍在处理中，请稍后核验。"),
                ("failed", "failed", "原操作失败，请重新核对。"), ("unknown", "unknown", "保存结果待核验，请查看原操作。")]
    model = ScriptedModel(*[item for _, state, reply in statuses for item in
                           (call_tool("get_operation", operation_id="a6-op-one"), answer(state, reply))])
    app = create_app(database_path, model=model)
    with TestClient(app) as client:
        body = save_body("one", invoice(client)["revision"], {"note": "状态测试"})
        assert request(client, "/api/invoices/F002", "PATCH", body).status_code == 200
        saved = invoice(client)
        for index, (status, _, reply) in enumerate(statuses):
            with app.state.database.write() as connection:
                connection.execute("UPDATE tasks SET status='blocked' WHERE task_id='a6-task'")
                connection.execute("UPDATE operations SET status=? WHERE operation_id='a6-op-one'", (status,))
            assert send(client, f"state-{index}", "查询保存进度").json()["reply"] == reply
        assert request(client, "/api/invoices/F002", "PATCH", save_body("two", saved["revision"], {"note": "不可重写"})).status_code == 409
        assert invoice(client) == saved


def test_B09_B19_B20_B21_B35_concurrent_writes_rollback_and_restart(database_path):
    app = create_app(database_path, model=ScriptedModel())
    with TestClient(app) as client:
        initial = invoice(client, "F001")
        beta = invoice(client, "F001", enterprise_id="C002")
        one = save_body("one", initial["revision"], {"purpose": "甲"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = list(pool.map(lambda _: request(client, "/api/invoices/F001", "PATCH", one), range(2)))
        assert a.status_code == b.status_code == 200 and a.json() == b.json()
        assert request(client, "/api/invoices/F001", "PATCH", {**one, "patch": {"purpose": "冲突"}}).status_code == 409
        second = save_body("two", initial["revision"] + 1, {"purpose": "乙"}, session="s2")
        stale = save_body("stale", initial["revision"], {"purpose": "过期"}, session="s3")
        with ThreadPoolExecutor(max_workers=2) as pool:
            updated, rejected = list(pool.map(lambda item: request(client, "/api/invoices/F001", "PATCH", item), (second, stale)))
        assert updated.status_code == 200 and rejected.status_code == 409
        third = save_body("three", initial["revision"] + 2, {"purpose": "甲"})
        assert request(client, "/api/invoices/F001", "PATCH", third).status_code == 200
        committed = invoice(client, "F001")
        with app.state.database.write() as connection:
            connection.execute("CREATE TRIGGER a6_fault BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'A6 rollback'); END")
        failed = request(client, "/api/invoices/F001", "PATCH", save_body("failure", committed["revision"], {"note": "回滚"}))
        assert failed.status_code == 500 and invoice(client, "F001") == committed
        assert request(client, "/api/operations/a6-op-failure").status_code == 404
        with app.state.database.connect() as connection:
            assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 3
    with TestClient(create_app(database_path, model=ScriptedModel())) as restarted:
        assert invoice(restarted, "F001") == committed
        assert invoice(restarted, "F001", enterprise_id="C002") == beta


def test_B10_B12_B15_B33_memory_visibility_and_model_audit(database_path):
    key = "fixture-a6-audit-key"
    captured = []
    decisions = iter([AnswerDecision(kind="answer", reply="收到。"),
                      call_tool("search_rules", query="发票用途"),
                      AnswerDecision(kind="answer", reply="按最新偏好检查。"),
                      AnswerDecision(kind="answer", reply="已查询。")])

    def provider(http_request):
        assert http_request.headers["Authorization"] == f"Bearer {key}"
        payload = json.loads(http_request.content)
        captured.append(payload["messages"])
        return httpx.Response(200, json={"choices": [{"message": {"content": next(decisions).model_dump_json()}}]})

    model = ChatCompletionsModel("https://fixture.test/chat", "fixture", key, transport=httpx.MockTransport(provider))
    app = create_app(database_path, model=model)
    with TestClient(app) as client:
        origin = "记住先汇总，测试凭据 " + key
        assert send(client, "origin", origin).status_code == 200
        records = request(client, "/api/workbench/records").json()
        message_id = records["source_messages"][0]["message_id"]
        memory = request(client, "/api/memories", "POST", {"content": "旧偏好先汇总", "source_message_id": message_id}).json()
        path = f"/api/memories/{memory['memory_id']}"
        assert request(client, "/api/memories").json()[0]["content"] == "旧偏好先汇总"
        corrected = request(client, path, "PATCH", {"content": "新偏好先明细", "expected_revision": 1}).json()
        assert corrected["invalidated_message_ids"] == [message_id]
        assert send(client, "corrected", "查发票用途规则", session="new").status_code == 200
        assert any("新偏好先明细" in item["content"] for item in captured[-1])
        assert all("旧偏好先汇总" not in item["content"] and "记住先汇总" not in item["content"] for item in captured[-1])
        assert request(client, path, "DELETE", expected_revision=2, user="U002").status_code == 404
        assert request(client, "/api/memories", user="U002").json() == []
        assert request(client, path, "DELETE", expected_revision=2).status_code == 200
        assert send(client, "deleted", "再查询", session="new").status_code == 200
        assert all("新偏好先明细" not in item["content"] for item in captured[-1])
        with app.state.database.connect() as connection:
            events = connection.execute("SELECT kind,payload_json FROM events ORDER BY event_id").fetchall()
            assert all(key not in row["payload_json"] for row in events)
            frames = [json.loads(row["payload_json"]) for row in events if row["kind"] == "model_input"]
            assert [frame["messages"] for frame in frames] == captured
            assert message_id in frames[-1]["omitted_message_ids"]
            assert any({source["kind"] for source in frame["sources"]} == {"rule", "memory"} for frame in frames)
            assert any(row["kind"] == "tool_result" for row in events)
        body = save_body("audit", invoice(client)["revision"], {"note": "权限核对"})
        assert request(client, "/api/invoices/F002", "PATCH", body).status_code == 200
        assert request(client, "/api/operations/a6-op-audit", user="U002").status_code == 404
        other = save_body("wrong-enterprise", invoice(client, enterprise_id="C002")["revision"], {"note": "错误任务"})
        assert request(client, "/api/invoices/F002", "PATCH", other, enterprise_id="C002").status_code == 404


def test_B13_B19_rule_change_invalidates_proposal(database_path):
    def updated_rule(context):
        assert tool_data(context)["data"]["rules"][0]["citation"]["version"] == "3.0"
        return AnswerDecision(kind="answer", reply="请查阅新版。")

    def unverified_policy(context):
        result = tool_data(context)
        assert result["status"] == "failed" and "核对企业和规则版本" in result["error"]["message"]
        return AnswerDecision(kind="answer", reply="请核对用途检查规则版本。")

    model = ScriptedModel(call_tool("propose_invoice_update", invoice_id="F002", patch={"purpose": "旧依据的提案"}),
                          call_tool("search_rules", query="R002"), updated_rule,
                          call_tool("check_invoice_purposes"), unverified_policy)
    app = create_app(database_path, model=model)
    with TestClient(app) as client:
        initial = invoice(client)
        assert "确认保存" in send(client, "propose", "依据发票用途规则修改 F002").json()["reply"]
        new_rule = {"name": "甲企业发票用途规则", "version": "3.0", "effective_from": "2026-09-02", "content": "A6 版本更新夹具"}
        assert request(client, "/api/rules/R002/versions", "POST", new_rule).status_code == 201
        assert request(client, "/api/rules/R002/deactivate", "POST", {"expected_revision": 1}, version="2.0").status_code == 200
        assert "依据已变化" in send(client, "confirm", "确认保存").json()["reply"]
        assert invoice(client) == initial
        assert "新版" in send(client, "updated", "查询新规则").json()["reply"]
        assert all(rule["citation"]["version"] != "2.0" for rule in request(client, "/api/rules", query="R002").json())
        refused = send(client, "unverified-policy", "按新规则检查缺用途发票").json()
        assert "核对" in refused["reply"] and not refused["citations"]


def test_B14_B23_bounded_context_and_timeout_keep_pending(database_path):
    model = ScriptedModel(AskDecision(kind="ask", reply="请补充 F002 用途。"),
                          *[call_tool("unknown_tool")] * 6, ModelUnavailable("模型超时，请重试。"))
    app = create_app(database_path, model=model)
    with TestClient(app) as client:
        first = send(client, "ask", "核对发票").json()
        over_limit = send(client, "loop", "继续核对").json()
        assert "步数上限" in over_limit["reply"] and over_limit["pending_items"] == first["pending_items"]
        timeout = send(client, "timeout", "办公用途").json()
        assert timeout["task_id"] == first["task_id"] and timeout["pending_items"] == first["pending_items"]
        assert len(model.inputs) == 8
        task = request(client, "/api/tasks/active").json()["task"]
        with app.state.database.write() as connection:
            connection.execute("INSERT INTO messages(message_id,task_id,session_id,role,content) VALUES ('oversized',?,'s1','user',?)",
                               (task["task_id"], "长历史" * 2500))
        context_request = ContextRequest(scope=Scope(user_id="U001", **PARAMS), task_id=task["task_id"],
            task_revision=task["revision"], task_state={"object_id": "F002", "question": "请补充 F002 用途。"},
            user_input="继续", input_token_budget=12000)
        builder = app.state.turn_executor.context
        base = builder.build(context_request)
        huge = ToolResult(status="succeeded", data={"invoice_cents": 962206, "records": [{"note": "长明细" * 40}] * 80})
        context = builder.build(context_request.model_copy(update={"input_token_budget": base.input_tokens + 1200,
                                                                 "tool_results": [huge]}))
        assert "oversized" in context.omitted_message_ids
        assert context.input_tokens <= base.input_tokens + 1200
        assert any('"object_id":"F002"' in message["content"] and "请补充 F002" in message["content"] for message in context.messages)
        assert any('"invoice_cents":962206' in message["content"] and '"omitted_records":78' in message["content"] for message in context.messages)


def test_B22_task_correction_clear_and_unresolved_guard(database_path):
    model = ScriptedModel(AskDecision(kind="ask", reply="请补充原待办。"),
                          call_tool("propose_invoice_update", invoice_id="F002", patch={"note": "旧提案"}))
    app = create_app(database_path, model=model)
    with TestClient(app) as client:
        first = send(client, "ask", "补充用途").json()
        path = f"/api/tasks/{first['task_id']}"
        original = request(client, path).json()
        assert original["question"] == "请补充原待办。"
        assert request(client, path, "PATCH", {"expected_revision": original["revision"], "object_id": "F011"}).status_code == 404
        revised = request(client, path, "PATCH", {"expected_revision": original["revision"], "object_id": "F002",
                                                "question": "请补充 F002 用途。"}).json()
        assert revised["object_id"] == "F002" and revised["question"] == "请补充 F002 用途。"
        assert "F002" in send(client, "resume", "继续任务", session="new").json()["reply"]
        initial = invoice(client)
        assert "确认保存" in send(client, "propose", "给 F002 增加备注", session="new").json()["reply"]
        proposed = request(client, path).json()
        assert proposed["has_proposal"]
        assert request(client, path, "PATCH", {"expected_revision": proposed["revision"],
                                                "question": "请重新说明待办。"}).status_code == 200
        assert not send(client, "stale-confirm", "确认保存", session="new").json()["operation_ids"]
        assert invoice(client) == initial
        current = request(client, path).json()
        cleared = request(client, path, "PATCH", {"expected_revision": current["revision"], "question": None}).json()
        assert cleared["question"] is None and cleared["status"] == "running"
        body = save_body("prior", invoice(client)["revision"], {"note": "历史已保存"}, task_id=first["task_id"])
        assert request(client, "/api/invoices/F002", "PATCH", body).status_code == 200
        with app.state.database.write() as connection:
            connection.execute("UPDATE operations SET status='unknown' WHERE operation_id='a6-op-prior'")
        change = {"expected_revision": cleared["revision"], "object_id": "F003"}
        assert request(client, path, "PATCH", change).status_code == 409
        assert request(client, "/api/operations/a6-op-prior/reconcile", "POST").json()["status"] == "succeeded"
        assert request(client, path, "PATCH", change).json()["object_id"] == "F003"
        assert request(client, "/api/operations/a6-op-prior").json()["data"]["record"]["note"] == "历史已保存"
