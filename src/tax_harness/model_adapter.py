import os
from math import isfinite
from typing import Annotated, Literal, Protocol

import httpx
from pydantic import Field, TypeAdapter

from .contracts import Citation, ContextBundle, Contract, Identifier
from .model_io import MAX_DECISION_RETRIES, TIMEOUT_MESSAGE, parse_decision_content, read_model_content


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
    def __init__(self, endpoint: str, model: str, key: str, *, timeout: float = 90,
                 transport: httpx.BaseTransport | None = None, retries: int = MAX_DECISION_RETRIES):
        if retries < 0 or retries > MAX_DECISION_RETRIES:
            raise ValueError("model decision retries must be 0 or 1")
        self.endpoint = endpoint
        self.model = model
        self.key = key
        self.timeout = timeout
        self.transport = transport
        self.retries = retries

    def decide(self, context: ContextBundle) -> Decision:
        return parse_decision_content(read_model_content(self, context))


def configured_model() -> ChatCompletionsModel | None:
    endpoint = os.getenv("HARNESS_MODEL_ENDPOINT", "").strip()
    model = os.getenv("HARNESS_MODEL_NAME", "").strip()
    key = os.getenv("HARNESS_MODEL_KEY", "").strip()
    if not all((endpoint, model, key)):
        return None
    timeout = float(os.getenv("HARNESS_MODEL_TIMEOUT", "90"))
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("HARNESS_MODEL_TIMEOUT must be a positive number of seconds")
    return ChatCompletionsModel(endpoint, model, key, timeout=timeout)
