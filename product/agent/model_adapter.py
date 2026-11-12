"""OpenAI-compatible model boundary. Credentials never enter conversation state."""
import json
import os
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from typing import Annotated, Literal


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Requirement(StrictModel):
    requirement_id: str = Field(min_length=1, max_length=40)
    kind: Literal["invoice_total", "income_total", "expense_total", "rules", "invoices", "review", "operation"]
    filters: dict = Field(default_factory=dict)


class AnswerItem(StrictModel):
    requirement_id: str
    evidence_ids: list[str]
    value_key: str


class DecisionFields(StrictModel):
    claims_proposal: bool = False
    tool: str | None = None
    arguments: dict = Field(default_factory=dict)
    message: str = Field(default="", max_length=3000)
    requirements: list[Requirement] | None = None
    answer_items: list[AnswerItem] = Field(default_factory=list)


class ToolDecision(DecisionFields):
    kind: Literal["tool"]
    tool: str
    arguments: dict


class AskDecision(DecisionFields):
    kind: Literal["ask"]
    message: str = Field(min_length=1, max_length=3000)


class AnswerDecision(DecisionFields):
    kind: Literal["answer"]
    message: str = Field(min_length=1, max_length=3000)
    answer_items: list[AnswerItem]


class ProposeDecision(DecisionFields):
    kind: Literal["propose"]
    arguments: dict


DECISION = TypeAdapter(Annotated[ToolDecision | AskDecision | AnswerDecision | ProposeDecision,
                                Field(discriminator="kind")])


def configured():
    return bool(os.getenv("PRODUCT_MODEL_BASE_URL") and os.getenv("PRODUCT_MODEL_NAME"))


def redact(text):
    for name in ("PRODUCT_MODEL_API_KEY", "AGENT_TOKEN"):
        value = os.getenv(name, "")
        if value:
            text = text.replace(value, "[redacted]")
    return text


def decide(messages):
    base = os.environ["PRODUCT_MODEL_BASE_URL"].rstrip("/")
    body = {"model": os.environ["PRODUCT_MODEL_NAME"], "messages": messages, "temperature": 0, "max_tokens": 2048}
    if os.getenv("PRODUCT_MODEL_STRUCTURED", "none") == "schema":
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "decision", "schema": DECISION.json_schema()}}
    headers = {"Content-Type": "application/json"}
    key = os.getenv("PRODUCT_MODEL_API_KEY", "")
    if key:
        headers["Authorization"] = "Bearer " + key
    timeout = min(300, max(1, float(os.getenv("PRODUCT_MODEL_TIMEOUT", "120"))))
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.post(base + "/chat/completions", json=body, headers=headers)
        response.raise_for_status()
        raw = response.text
    evidence = os.getenv("PRODUCT_AGENT_EVIDENCE_DIR")
    if evidence:
        # Only opt-in isolated validation enables raw response retention.
        import uuid
        folder = Path(evidence)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (str(uuid.uuid4()) + ".json")).write_text(
            redact(json.dumps({"request": body, "response": raw}, ensure_ascii=False)), encoding="utf-8")
    if len(raw.encode()) > 262144:
        raise ValueError("model_response_too_large")
    content = json.loads(raw)["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("invalid_model_response")
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return DECISION.validate_json(text)
