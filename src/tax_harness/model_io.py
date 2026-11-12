"""Read-only Chat Completions transport and decision-text unwrapping.

Business writes do not use this module. A read timeout may be retried once.
"""

import re

import httpx

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


def parse_decision_content(content: str):
    from .model_adapter import DECISION, InvalidDecision

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
