import os
from math import isfinite
from typing import Annotated, Literal, Protocol

import httpx
from pydantic import Field, TypeAdapter

from .contracts import Citation, ContextBundle, Contract, Identifier


class ToolDecision(Contract):
    kind: Literal["tool"]
    name: Identifier
    arguments: dict


class AskDecision(Contract):
    kind: Literal["ask"]
    reply: str = Field(min_length=1)


class AnswerDecision(Contract):
    kind: Literal["answer"]
    reply: str = Field(min_length=1)
    citations: list[Citation] = Field(default_factory=list)
    resolved_pending_question: bool = Field(default=False, strict=True)


Decision = Annotated[ToolDecision | AskDecision | AnswerDecision, Field(discriminator="kind")]
DECISION = TypeAdapter(Decision)


class ModelAdapter(Protocol):
    def decide(self, context: ContextBundle) -> Decision: ...


class ModelUnavailable(RuntimeError):
    pass


class InvalidDecision(ValueError):
    pass


class ChatCompletionsModel:
    def __init__(self, endpoint: str, model: str, key: str, *, timeout: float = 25,
                 transport: httpx.BaseTransport | None = None):
        self.endpoint = endpoint
        self.model = model
        self.key = key
        self.timeout = timeout
        self.transport = transport

    def decide(self, context: ContextBundle) -> Decision:
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                response = client.post(self.endpoint, headers={"Authorization": f"Bearer {self.key}"},
                                       json={"model": self.model, "messages": context.messages})
                response.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ModelUnavailable("模型请求失败或超时，请稍后重试。") from exc
        try:
            content = response.json()["choices"][0]["message"]["content"]
            return DECISION.validate_json(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise InvalidDecision("模型返回的动作格式无效。") from exc


def configured_model() -> ChatCompletionsModel | None:
    endpoint = os.getenv("HARNESS_MODEL_ENDPOINT", "").strip()
    model = os.getenv("HARNESS_MODEL_NAME", "").strip()
    key = os.getenv("HARNESS_MODEL_KEY", "").strip()
    if not all((endpoint, model, key)):
        return None
    timeout = float(os.getenv("HARNESS_MODEL_TIMEOUT", "25"))
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("HARNESS_MODEL_TIMEOUT must be a positive number of seconds")
    return ChatCompletionsModel(endpoint, model, key, timeout=timeout)
