"""Read-only Chat Completions transport and decision-text unwrapping.

Business writes do not use this module. A read timeout may be retried once.
"""

import json
import re

import httpx
from pydantic import ValidationError

TIMEOUT_MESSAGE = "模型请求超时，本次没有得到可用结果。请稍后使用新的请求编号重试。"
REQUEST_FAILED_MESSAGE = "模型请求失败，请稍后重试。"
MAX_DECISION_RETRIES = 1

_TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def unwrap_decision_content(content: str) -> str:
    """Strip one tool_call or fenced JSON wrapper. Field validation stays strict."""
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


def read_model_content(model, context) -> str:
    from .model_adapter import InvalidDecision, ModelUnavailable

    content = None
    last_timeout = None
    for _ in range(model.retries + 1):
        try:
            content = _post(model, context)
            break
        except httpx.TimeoutException as exc:
            last_timeout = exc
        except InvalidDecision:
            raise
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ModelUnavailable(REQUEST_FAILED_MESSAGE) from exc
    else:
        raise ModelUnavailable(TIMEOUT_MESSAGE) from last_timeout
    return content


def _post(model, context) -> str:
    from .model_adapter import InvalidDecision

    with httpx.Client(timeout=model.timeout, transport=model.transport) as client:
        response = client.post(model.endpoint, headers={"Authorization": f"Bearer {model.key}"},
                               json={"model": model.model, "messages": context.messages})
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


_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,40}")


def _safe_field(name: str) -> str:
    return name if _FIELD.fullmatch(name) else "字段"


def describe_decision_error(exc: ValidationError) -> str:
    """Name the failing field. Do not echo values, URLs, or exception text."""
    parts = []
    for err in exc.errors()[:4]:
        if err.get("type") in {"json_invalid", "json_type"}:
            return "动作不是合法 JSON。"
        if err.get("type") == "union_tag_invalid":
            parts.append("kind 取值无效")
            continue
        if err.get("type") == "union_tag_not_found":
            parts.append("缺少字段 kind")
            continue
        loc = [str(part) for part in err.get("loc", ()) if part not in {"", "__root__"}]
        kind = loc[0] if loc and loc[0] in {"ask", "answer", "tool"} else ""
        field = _safe_field(".".join(loc[1:] if kind else loc) or "内容")
        prefix = f"{kind} " if kind else ""
        if err.get("type") == "extra_forbidden":
            parts.append(f"{prefix}不允许字段 {field}".strip())
        elif err.get("type") == "missing":
            parts.append(f"{prefix}缺少字段 {field}".strip())
        else:
            parts.append(f"{prefix}{field} 不符合要求".strip())
    return "；".join(dict.fromkeys(parts)) or "模型返回的动作格式无效。"


def without_ask_citations(text: str) -> str:
    try:
        payload = json.loads(text)
    except ValueError:
        return text
    if isinstance(payload, dict) and payload.get("kind") == "ask" and "citations" in payload:
        payload = {key: value for key, value in payload.items() if key != "citations"}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text


def parse_decision_content(content: str):
    from .model_adapter import DECISION, InvalidDecision

    def load(text: str):
        try:
            return DECISION.validate_json(without_ask_citations(text))
        except ValidationError as exc:
            raise InvalidDecision(describe_decision_error(exc)) from exc

    text = content.strip()
    try:
        return load(text)
    except InvalidDecision:
        unwrapped = unwrap_decision_content(text)
        if unwrapped == text:
            raise
        return load(unwrapped)
