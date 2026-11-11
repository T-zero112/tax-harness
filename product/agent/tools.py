"""Typed tool arguments. Scope and authentication are never model-controlled."""
import os
from typing import Literal

import httpx
from pydantic import Field
from model_adapter import StrictModel


class InvoiceFilter(StrictModel):
    invoice_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    seller: str | None = Field(default=None, min_length=1, max_length=120)
    item_name: str | None = Field(default=None, min_length=1, max_length=120)


class BankFilter(StrictModel):
    transaction_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    direction: Literal["income", "expense"] | None = None


class InvoiceId(StrictModel):
    invoice_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class RuleQuery(StrictModel):
    query: str = Field(min_length=1, max_length=240)


class Proposal(InvoiceId):
    changes: dict[Literal["purpose", "note"], str]


class Operation(InvoiceId):
    proposal_id: str = Field(pattern=r"^[a-fA-F0-9-]{36}$")


TOOLS = {"list_invoices": InvoiceFilter, "list_bank_transactions": BankFilter, "get_invoice": InvoiceId,
         "search_rules": RuleQuery, "check_purpose": InvoiceId, "create_proposal": Proposal, "get_operation": Operation}


def definitions():
    return {name: model.model_json_schema() for name, model in TOOLS.items()}


def invoke(name, arguments, call_token):
    with httpx.Client(timeout=100, trust_env=False) as client:
        response = client.post("http://business:8080/internal/agent/" + name,
            headers={"Authorization": "Bearer " + os.environ["AGENT_TOKEN"]},
            json={"call_token": call_token, "arguments": arguments})
    if response.status_code != 200:
        return {"status": "failed", "data": {}, "error": {"code": "http_" + str(response.status_code),
                "message": "业务工具拒绝请求，请核对范围、权限或版本。"}}
    return response.json()
