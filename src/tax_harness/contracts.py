"""A/B shared contract v1. All changes require coordinated integration."""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Month = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskStatus(StrEnum):
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class OperationStatus(StrEnum):
    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Scope(Contract):
    user_id: Identifier
    enterprise_id: Identifier
    month: Month


class MessageRequest(Contract):
    request_id: Identifier
    session_id: Identifier
    enterprise_id: Identifier
    month: Month | None = None
    text: Annotated[str, Field(min_length=1, max_length=20000)]


class Error(Contract):
    code: Literal["invalid_argument", "forbidden", "not_found", "conflict",
                  "tool_failed", "unknown_result", "busy", "not_implemented"]
    message: str
    retryable: bool = False


class Citation(Contract):
    doc_id: Identifier
    name: str
    version: Identifier
    entry: str


class TurnResponse(Contract):
    request_id: Identifier
    task_id: Identifier
    task_status: TaskStatus
    reply: str
    pending_items: list[str] = Field(default_factory=list)
    operation_ids: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


class ToolResult(Contract):
    status: Literal["succeeded", "pending", "failed", "unknown"]
    object_id: Identifier | None = None
    data: dict[str, Any] | None = None
    error: Error | None = None
    operation_id: Identifier | None = None

    @model_validator(mode="after")
    def check_outcome(self):
        if self.status in {"failed", "unknown"} and self.error is None:
            raise ValueError("failed/unknown results require error details")
        if self.status in {"succeeded", "pending"} and self.error is not None:
            raise ValueError("successful/pending results cannot carry an error")
        return self


class InvoiceFilters(Contract):
    invoice_id: Identifier | None = None
    seller: Identifier | None = None
    item_name: Identifier | None = None


class BankFilters(Contract):
    transaction_id: Identifier | None = None
    direction: Literal["income", "expense"] | None = None


class InvoicePatch(Contract):
    purpose: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def require_field(self):
        if not self.model_fields_set:
            raise ValueError("at least one editable field must be supplied")
        return self


class InvoiceSaveRequest(Contract):
    task_id: Identifier
    session_id: Identifier
    request_id: Identifier
    operation_id: Identifier
    expected_revision: int = Field(ge=1, strict=True)
    patch: InvoicePatch


class TaskCloseRequest(Contract):
    status: Literal["completed", "cancelled"]
    expected_revision: int = Field(ge=1, strict=True)


class TaskCorrectionRequest(Contract):
    expected_revision: int = Field(ge=1, strict=True)
    question: str | None = Field(default=None, max_length=2000)
    object_id: Identifier | None = None

    @model_validator(mode="after")
    def require_change(self):
        if not (self.model_fields_set & {"question", "object_id"}):
            raise ValueError("请指定要更正或清除的待办或发票")
        if self.question is not None and not self.question.strip():
            raise ValueError("清除待办请使用 null")
        return self


class Memory(Contract):
    memory_id: Identifier
    user_id: Identifier
    enterprise_id: Identifier | None
    content: str
    source_message_id: Identifier
    revision: int = Field(ge=1)


class RuleHit(Contract):
    citation: Citation
    enterprise_id: Identifier
    effective_from: str
    content: str
    revision: int = Field(ge=1)


class RevisionRef(Contract):
    kind: Literal["memory", "rule"]
    identifier: Identifier
    revision: int = Field(ge=1)


class MutationReceipt(Contract):
    reference: RevisionRef
    invalidated_message_ids: list[str] = Field(default_factory=list)


class HistoryMessage(Contract):
    message_id: Identifier
    role: Literal["user", "assistant", "tool"]
    content: str


class ContextRequest(Contract):
    scope: Scope
    task_id: Identifier
    task_revision: int = Field(ge=1)
    task_state: dict[str, Any]
    user_input: str
    tool_results: list[ToolResult] = Field(default_factory=list)
    input_token_budget: int = Field(gt=0)


class ContextBundle(Contract):
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    sources: list[RevisionRef]
    omitted_message_ids: list[str]
    input_tokens: int = Field(ge=0)
    token_count_method: str
