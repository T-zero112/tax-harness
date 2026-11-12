"""Guards for model timeout, decision wrapping, amount coverage, and proposal targets."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tax_harness.app import create_app
from tax_harness.contracts import ContextBundle
from tax_harness.model_adapter import (
    TIMEOUT_MESSAGE, ChatCompletionsModel, ModelUnavailable, configured_model,
)
from test_agent import HEADERS, PARAMS, seed, send


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


def test_fix1_timeout_and_openai_compatible_settings(monkeypatch):
    for key in ("HARNESS_MODEL_ENDPOINT", "HARNESS_MODEL_NAME", "HARNESS_MODEL_KEY",
                "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY", "HARNESS_MODEL_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    assert configured_model() is None
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("OPENAI_MODEL", "qwen-fixture")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-openai-key")
    model = configured_model()
    assert model.endpoint == "https://example.test/v1/chat/completions"
    assert model.model == "qwen-fixture" and model.timeout == 90
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1/chat/completions")
    monkeypatch.setenv("HARNESS_MODEL_ENDPOINT", "https://explicit.test/v1/chat/completions")
    monkeypatch.setenv("HARNESS_MODEL_NAME", "explicit-model")
    monkeypatch.setenv("HARNESS_MODEL_TIMEOUT", "75")
    model = configured_model()
    assert model.endpoint == "https://explicit.test/v1/chat/completions"
    assert model.model == "explicit-model" and model.key == "fixture-openai-key" and model.timeout == 75
