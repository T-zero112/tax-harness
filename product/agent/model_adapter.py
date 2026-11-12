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
    message: str = Field(default="", max_length=3000)
    requirements: list[Requirement] | None = None

class ToolDecision(DecisionFields):
    kind: Literal["tool"]
    tool: Literal["list_invoices","list_bank_transactions","get_invoice","search_rules","check_purpose","create_proposal","get_operation","list_recent_operations"]
    arguments: dict

class AskDecision(DecisionFields):
    kind: Literal["ask"]
    message: str = Field(min_length=1,max_length=3000)

class AnswerDecision(DecisionFields):
    kind: Literal["answer"]
    message: str = Field(min_length=1,max_length=3000)
    answer_items: list[AnswerItem]

class ProposeDecision(DecisionFields):
    kind: Literal["propose"]
    arguments: dict


DECISION = TypeAdapter(Annotated[ToolDecision | AskDecision | AnswerDecision | ProposeDecision,
                                Field(discriminator="kind")])


def decision_schema(registered=False):
    from copy import deepcopy
    from tools import TOOLS, Proposal
    schema=DECISION.json_schema()
    common=schema["$defs"]["ToolDecision"]
    variants=[]
    for name,arguments in TOOLS.items():
        branch=deepcopy(common)
        branch["properties"]["tool"]={"const":name,"type":"string"}
        branch["properties"]["arguments"]=arguments.model_json_schema()
        variants.append(branch)
    schema["$defs"]["ToolDecision"]={"oneOf":variants}
    schema["$defs"]["ProposeDecision"]["properties"]["arguments"]=Proposal.model_json_schema()
    if registered:
        def strip(node):
            if isinstance(node,dict):
                if node.get("properties",{}).get("kind"):
                    node["properties"].pop("requirements",None)
                for value in node.values():strip(value)
            elif isinstance(node,list):
                for value in node:strip(value)
        strip(schema)
    return schema


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
            "name": "decision", "schema": decision_schema(bool(json.loads(messages[-1]["content"]).get("requirements_registered")))}}
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
    parsed=json.loads(text)
    if isinstance(parsed,dict) and parsed.get("kind")=="tool" and parsed.get("tool") not in ToolDecision.model_fields["tool"].annotation.__args__:
        raise ValueError("unknown_tool")
    return DECISION.validate_python(parsed)
