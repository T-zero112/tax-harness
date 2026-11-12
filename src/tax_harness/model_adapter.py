import os
import re
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


_TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def unwrap_decision_content(content: str) -> str:
    """Strip one <tool_call> or fenced JSON wrapper. Field validation stays strict."""
    text = content.strip()
    for _ in range(3):
        tool_calls = _TOOL_CALL.findall(text)
        fences = _FENCE.findall(text)
        if len(tool_calls) == 1 and not fences:
            text = tool_calls[0].strip()
            continue
        if len(fences) == 1 and not tool_calls:
            text = fences[0].strip()
            continue
        if len(tool_calls) == 1 and len(fences) == 1:
            inner_tool = tool_calls[0].strip()
            inner_fence = fences[0].strip()
            if inner_fence in inner_tool or inner_tool in inner_fence:
                text = inner_tool if inner_fence in inner_tool else inner_fence
                continue
        break
    return text


TIMEOUT_MESSAGE = "模型请求超时，本次没有得到可用结果。请稍后使用新的请求编号重试。"
REQUEST_FAILED_MESSAGE = "模型请求失败，请稍后重试。"
# One extra attempt. Model decisions do not write business data; confirmation does not use this client.
MAX_DECISION_RETRIES = 1


def chat_completions_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


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

    def _post(self, context: ContextBundle) -> str:
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            response = client.post(self.endpoint, headers={"Authorization": f"Bearer {self.key}"},
                                   json={"model": self.model, "messages": context.messages})
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise InvalidDecision("模型返回的动作格式无效。") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise InvalidDecision("模型返回的动作格式无效。") from exc
        if not isinstance(content, str):
            raise InvalidDecision("模型返回的动作格式无效。")
        return content

    def decide(self, context: ContextBundle) -> Decision:
        content = None
        last_timeout: httpx.TimeoutException | None = None
        for _ in range(self.retries + 1):
            try:
                content = self._post(context)
                break
            except httpx.TimeoutException as exc:
                last_timeout = exc
            except InvalidDecision:
                raise
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                raise ModelUnavailable(REQUEST_FAILED_MESSAGE) from exc
        else:
            raise ModelUnavailable(TIMEOUT_MESSAGE) from last_timeout
        text = content.strip()
        try:
            return DECISION.validate_json(text)
        except (TypeError, ValueError) as first_error:
            unwrapped = unwrap_decision_content(text)
            if unwrapped == text:
                raise InvalidDecision("模型返回的动作格式无效。") from first_error
            try:
                return DECISION.validate_json(unwrapped)
            except (TypeError, ValueError) as exc:
                raise InvalidDecision("模型返回的动作格式无效。") from exc


def configured_model() -> ChatCompletionsModel | None:
    endpoint = os.getenv("HARNESS_MODEL_ENDPOINT", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if not endpoint and base_url:
        endpoint = chat_completions_url(base_url)
    model = os.getenv("HARNESS_MODEL_NAME", "").strip() or os.getenv("OPENAI_MODEL", "").strip()
    key = os.getenv("HARNESS_MODEL_KEY", "").strip() or os.getenv("OPENAI_API_KEY", "").strip()
    if not all((endpoint, model, key)):
        return None
    timeout = float(os.getenv("HARNESS_MODEL_TIMEOUT", "90"))
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("HARNESS_MODEL_TIMEOUT must be a positive number of seconds")
    return ChatCompletionsModel(endpoint, model, key, timeout=timeout)
