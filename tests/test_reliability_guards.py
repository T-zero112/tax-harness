"""Guards for model timeout, decision wrapping, amount coverage, and proposal targets."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tax_harness.answer_checks import (
    amount_coverage_issue, asserts_pending_proposal, invoice_mentioned, present_bank_result,
)
from tax_harness.app import create_app
from tax_harness.contracts import BankFilters, ContextBundle, ToolResult
from tax_harness.model_adapter import (
    TIMEOUT_MESSAGE, AnswerDecision, AskDecision, ChatCompletionsModel, InvalidDecision, ModelUnavailable,
    ToolDecision,
    configured_model,
)
from test_agent import HEADERS, PARAMS, ScriptedModel, proposal, read_invoice, seed, send


def context():
    return ContextBundle(messages=[], tools=[], sources=[], omitted_message_ids=[], input_tokens=0,
                         token_count_method="test")


def answer_body(reply):
    return {"choices": [{"message": {"content": json.dumps({"kind": "answer", "reply": reply}, ensure_ascii=False)}}]}


def test_fix1_read_timeout_retries_once_then_accepts_decision():
    calls = {"n": 0}

    def provider(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("fixture timeout", request=request)
        return httpx.Response(200, json=answer_body("已完成查询。"))

    model = ChatCompletionsModel("https://model.test/v1/chat/completions", "fixture", "fixture-key",
                                 transport=httpx.MockTransport(provider))
    decision = model.decide(context())
    assert calls["n"] == 2 and decision.reply == "已完成查询。"


def test_fix1_repeated_timeout_is_honest_and_not_a_completed_result(tmp_path):
    calls = {"n": 0}

    def provider(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("fixture timeout", request=request)

    model = ChatCompletionsModel("https://model.test/v1/chat/completions", "fixture", "fixture-key",
                                 timeout=0.01, transport=httpx.MockTransport(provider))
    app = create_app(tmp_path / "timeout.sqlite3", model=model)
    with TestClient(app) as client:
        response = send(client, "slow", "查询本月银行收入和支出")
        body = response.json()
        assert body["reply"] == TIMEOUT_MESSAGE
        assert "元" not in body["reply"] and body["task_status"] == "running"
        assert body["pending_items"] == [] and body["operation_ids"] == []
        assert calls["n"] == 2
        with app.state.database.connect() as connection:
            assert connection.execute("SELECT status FROM requests").fetchone()[0] == "failed"
            assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


def test_fix1_http_error_is_not_retried():
    calls = {"n": 0}

    def provider(request):
        calls["n"] += 1
        return httpx.Response(500, json={"error": "upstream"})

    model = ChatCompletionsModel("https://model.test/v1/chat/completions", "fixture", "fixture-key",
                                 transport=httpx.MockTransport(provider))
    with pytest.raises(ModelUnavailable) as caught:
        model.decide(context())
    assert calls["n"] == 1 and "超时" not in str(caught.value)


def test_fix1_confirmation_does_not_call_the_model_or_write_twice(tmp_path):
    calls = {"n": 0}

    def provider(request):
        calls["n"] += 1
        if calls["n"] > 1:
            raise AssertionError("confirmation must not send another model request")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "kind": "tool", "name": "propose_invoice_update",
            "arguments": {"invoice_id": "F001", "patch": {"purpose": "办公资料"}},
        })}}]})

    model = ChatCompletionsModel("https://model.test/v1/chat/completions", "fixture", "fixture-key",
                                 transport=httpx.MockTransport(provider))
    app = create_app(tmp_path / "confirm.sqlite3", model=model)
    with TestClient(app) as client:
        seed(app)
        proposed = send(client, "propose", "请将 F001 用途改为办公资料")
        assert "确认保存" in proposed.json()["reply"] and calls["n"] == 1
        saved = send(client, "confirm", "确认保存")
        assert "回查确认" in saved.json()["reply"] and calls["n"] == 1
        replay = send(client, "confirm", "确认保存")
        assert replay.json()["operation_ids"] == saved.json()["operation_ids"] and calls["n"] == 1
        with app.state.database.connect() as connection:
            assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
            assert connection.execute("SELECT purpose FROM invoices").fetchone()[0] == "办公资料"


def decide_from(content):
    model = ChatCompletionsModel(
        "https://model.test/v1/chat/completions", "fixture", "fixture-key",
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]},
        )),
    )
    return model.decide(context())


def test_fix2_unwraps_tool_call_and_json_fence_without_relaxing_schema():
    tool = '{"kind":"tool","name":"list_invoices","arguments":{}}'
    ask = '{"kind":"ask","reply":"请补充发票编号"}'
    answer = '{"kind":"answer","reply":"已核对"}'
    assert decide_from(tool).name == "list_invoices"
    assert decide_from(f"<tool_call>\n{tool}\n</tool_call>").name == "list_invoices"
    assert decide_from(f"```json\n{ask}\n```").reply == "请补充发票编号"
    assert decide_from(f"<tool_call>\n```json\n{answer}\n```\n</tool_call>").reply == "已核对"
    assert decide_from(f"先查询。\n<tool_call>{tool}</tool_call>").name == "list_invoices"
    with pytest.raises(InvalidDecision):
        decide_from(f"<tool_call>{tool[:-1]},\"extra\":1}}</tool_call>")
    with pytest.raises(InvalidDecision):
        decide_from('```json\n{"kind":"ask"}\n```')
    with pytest.raises(InvalidDecision):
        decide_from('请查询 {"kind":"answer","reply":"已核对"}')
    with pytest.raises(InvalidDecision):
        decide_from(f"<tool_call>{tool}</tool_call><tool_call>{ask}</tool_call>")


def test_fix2_wrapped_tool_call_runs_the_registered_tool(tmp_path):
    contents = iter([
        '<tool_call>{"kind":"tool","name":"list_invoices","arguments":{}}</tool_call>',
        '```json\n{"kind":"answer","reply":"发票共 501.00 元。"}\n```',
    ])
    model = ChatCompletionsModel(
        "https://model.test/v1/chat/completions", "fixture", "fixture-key",
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": next(contents)}}]},
        )),
    )
    app = create_app(tmp_path / "wrapped.sqlite3", model=model)
    with TestClient(app) as client:
        seed(app)
        response = send(client, "wrapped", "查询发票总额")
        assert response.status_code == 200 and response.json()["reply"] == "发票共 501.00 元。"
        with app.state.database.connect() as connection:
            assert connection.execute("SELECT count(*) FROM events WHERE kind='tool_result'").fetchone()[0] == 1


def test_fix1_timeout_keeps_the_existing_harness_settings(monkeypatch):
    for key in ("HARNESS_MODEL_ENDPOINT", "HARNESS_MODEL_NAME", "HARNESS_MODEL_KEY", "HARNESS_MODEL_TIMEOUT",
                "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "qwen-fixture")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-openai-key")
    assert configured_model() is None
    monkeypatch.setenv("HARNESS_MODEL_ENDPOINT", "https://explicit.test/v1/chat/completions")
    monkeypatch.setenv("HARNESS_MODEL_NAME", "explicit-model")
    monkeypatch.setenv("HARNESS_MODEL_KEY", "fixture-harness-key")
    model = configured_model()
    assert model.endpoint == "https://explicit.test/v1/chat/completions"
    assert model.model == "explicit-model" and model.key == "fixture-harness-key" and model.timeout == 90
    monkeypatch.setenv("HARNESS_MODEL_TIMEOUT", "75")
    assert configured_model().timeout == 75


def income_only_observation():
    result = ToolResult(status="succeeded", data={
        "records": [{"direction": "income", "amount_cents": 1300050}],
        "bank_count": 1, "income_cents": 1300050, "expense_cents": 0,
    })
    return [{"arguments": {"direction": "income", "transaction_id": None}, "result": result}]


def test_fix3_uncovered_expense_amounts_and_false_pending_claims_are_rejected():
    observed = income_only_observation()
    assert amount_coverage_issue("银行支出 0.00 元。", observed)
    assert amount_coverage_issue("银行支出 7,180.74 元。", observed)
    assert amount_coverage_issue("银行收入 13000.50 元。", observed) is None
    full = ToolResult(status="succeeded", data={
        "records": [{"direction": "income", "amount_cents": 1300050}, {"direction": "expense", "amount_cents": 718074}],
        "income_cents": 1300050, "expense_cents": 718074,
    })
    covered = [{"arguments": {"direction": None, "transaction_id": None}, "result": full}]
    assert amount_coverage_issue("银行收入 13000.50 元，银行支出 7180.74 元。", covered) is None
    assert amount_coverage_issue("银行支出 0.00 元。", covered)
    shown = present_bank_result(BankFilters(direction="income"), observed[0]["result"])
    assert shown.data["income_cents"] == 1300050 and "expense_cents" not in shown.data
    assert asserts_pending_proposal("旧提案仍待确认")
    assert not asserts_pending_proposal("无法确认已付款，实际用途需要你补充。")
    assert not asserts_pending_proposal("本轮没有待确认的修改。")


def seed_bank(app):
    with app.state.database.write() as connection:
        connection.executemany(
            "INSERT INTO bank_transactions(enterprise_id,transaction_id,transaction_date,direction,amount_cents,counterparty) "
            "VALUES (?,?,?,?,?,?)",
            [("C001", "T-IN", "2026-09-02", "income", 1300050, "客户"),
             ("C001", "T-OUT", "2026-09-03", "expense", 718074, "供应商")],
        )


def test_fix3_income_query_cannot_answer_monthly_expense_until_it_is_covered(tmp_path):
    model = ScriptedModel(
        ToolDecision(kind="tool", name="list_bank_transactions", arguments={"direction": "income"}),
        AnswerDecision(kind="answer", reply="银行收入 13000.50 元，银行支出 0.00 元。"),
        ToolDecision(kind="tool", name="list_bank_transactions", arguments={}),
        AnswerDecision(kind="answer", reply="银行收入 13000.50 元，银行支出 7180.74 元。"),
    )
    app = create_app(tmp_path / "coverage.sqlite3", model=model)
    with TestClient(app) as client:
        seed_bank(app)
        response = send(client, "sum", "查询本月银行收入和支出").json()
        assert response["reply"] == "银行收入 13000.50 元，银行支出 7180.74 元。"
        assert response["operation_ids"] == []
        shown = json.loads(next(item["content"] for item in model.inputs[1].messages if item["content"].startswith('{"kind":"工具结果"')))
        assert shown["data"]["data"]["income_cents"] == 1300050 and "expense_cents" not in shown["data"]["data"]
        assert any("尚未被查询覆盖" in item["content"] for item in model.inputs[2].messages)


def test_fix3_covered_income_amount_is_accepted(tmp_path):
    model = ScriptedModel(
        ToolDecision(kind="tool", name="list_bank_transactions", arguments={"direction": "income"}),
        AnswerDecision(kind="answer", reply="银行收入 13000.50 元。"),
    )
    app = create_app(tmp_path / "income.sqlite3", model=model)
    with TestClient(app) as client:
        seed_bank(app)
        response = send(client, "income", "这个月进账多少").json()
        assert response["reply"] == "银行收入 13000.50 元。" and len(model.inputs) == 2


def test_fix3_cleared_proposal_cannot_be_described_as_still_pending(tmp_path):
    model = ScriptedModel(
        proposal(),
        AnswerDecision(kind="answer", reply="旧提案仍待确认。"),
        AnswerDecision(kind="answer", reply="本轮没有待确认的修改。"),
    )
    app = create_app(tmp_path / "pending.sqlite3", model=model)
    with TestClient(app) as client:
        seed(app)
        assert "确认保存" in send(client, "propose", "请将 F001 用途改为办公资料").json()["reply"]
        answered = send(client, "later", "现在还有待确认的修改吗").json()
        assert answered["reply"] == "本轮没有待确认的修改。"
        assert answered["pending_items"] == [] and "旧提案" not in answered["reply"]
        assert read_invoice(client)["purpose"] is None


def seed_second_invoice(app):
    seed(app)
    with app.state.database.write() as connection:
        connection.execute(
            "INSERT INTO invoices(enterprise_id,invoice_id,invoice_date,seller,item_name,amount_cents) "
            "VALUES ('C001','F002','2026-09-02','供方','培训资料',20000)"
        )


def propose_f002(purpose="培训资料打印"):
    return ToolDecision(kind="tool", name="propose_invoice_update",
                        arguments={"invoice_id": "F002", "patch": {"purpose": purpose}})


def test_fix4_query_results_do_not_authorize_an_unnamed_invoice(tmp_path):
    assert invoice_mentioned("请改 F002。", "F002")
    assert invoice_mentioned("请改 f002", "F002")
    assert not invoice_mentioned("请改 F0020 的用途", "F002")
    model = ScriptedModel(
        ToolDecision(kind="tool", name="list_invoices", arguments={}),
        propose_f002(),
    )
    app = create_app(tmp_path / "unnamed.sqlite3", model=model)
    with TestClient(app) as client:
        seed_second_invoice(app)
        response = send(client, "b34", "帮我把其中一张的用途补成培训资料打印").json()
        assert "发票编号" in response["reply"] and "确认保存" not in response["reply"]
        assert response["task_status"] == "waiting_user" and response["pending_items"] == [response["reply"]]
        assert response["operation_ids"] == []
        invoice = client.get("/api/invoices/F002", params=PARAMS, headers=HEADERS).json()["data"]
        assert invoice["purpose"] is None and invoice["revision"] == 1
        with app.state.database.connect() as connection:
            kinds = {row[0] for row in connection.execute("SELECT kind FROM events")}
            assert "proposal_target_rejected" in kinds and "save_proposal" not in kinds


def test_fix4_user_history_or_confirmed_target_allows_the_proposal(tmp_path):
    model = ScriptedModel(
        AskDecision(kind="ask", reply="请说明用途。"),
        propose_f002(),
        AskDecision(kind="ask", reply="请补充备注内容。"),
        ToolDecision(kind="tool", name="propose_invoice_update",
                     arguments={"invoice_id": "F002", "patch": {"note": "票据已核对"}}),
    )
    app = create_app(tmp_path / "named.sqlite3", model=model)
    with TestClient(app) as client:
        seed_second_invoice(app)
        assert send(client, "name", "我想改 F002").json()["task_status"] == "waiting_user"
        proposed = send(client, "purpose", "用途写培训资料打印").json()
        assert "确认保存" in proposed["reply"] and "F002" in proposed["reply"]
        assert client.get("/api/invoices/F002", params=PARAMS, headers=HEADERS).json()["data"]["purpose"] is None
        assert send(client, "cancel", "取消保存").json()["reply"].startswith("已取消")
        asked = send(client, "again", "再补一张票", session="s2").json()
        detail = client.get(f"/api/tasks/{asked['task_id']}", params=PARAMS, headers=HEADERS).json()
        bound = client.patch(f"/api/tasks/{asked['task_id']}", params=PARAMS, headers=HEADERS,
                             json={"expected_revision": detail["revision"], "object_id": "F002"})
        assert bound.status_code == 200 and bound.json()["object_id"] == "F002"
        confirmed = send(client, "note", "备注写成票据已核对", session="s2").json()
        assert "确认保存" in confirmed["reply"] and "F002" in confirmed["reply"]
        assert "F0020" not in confirmed["reply"]


def test_fix4_longer_identifier_does_not_count_as_the_target(tmp_path):
    model = ScriptedModel(propose_f002())
    app = create_app(tmp_path / "boundary.sqlite3", model=model)
    with TestClient(app) as client:
        seed_second_invoice(app)
        response = send(client, "near", "请改 F0020 的用途为培训资料打印").json()
        assert "发票编号" in response["reply"] and "确认保存" not in response["reply"]
        assert client.get("/api/invoices/F002", params=PARAMS, headers=HEADERS).json()["data"]["purpose"] is None
