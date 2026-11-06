"""Synchronous interfaces. Protocols specify contracts, not runtime implementations."""

from typing import Protocol

from .contracts import (
    BankFilters, Citation, ContextBundle, ContextRequest, HistoryMessage,
    InvoiceFilters, InvoiceSaveRequest, Memory, MutationReceipt, RevisionRef, RuleHit, Scope, ToolResult,
)


class RevisionConflict(ValueError):
    """The caller used an obsolete revision; re-read before changing data."""


class MemoryStore(Protocol):
    def list_active(self, scope: Scope) -> list[Memory]: ...

    def create(self, scope: Scope, content: str, source_message_id: str,
               *, user_wide: bool = False) -> Memory: ...

    def correct(self, scope: Scope, memory_id: str, content: str,
                expected_revision: int) -> MutationReceipt: ...

    def delete(self, scope: Scope, memory_id: str,
               expected_revision: int) -> MutationReceipt: ...

    def filter_history(self, scope: Scope,
                       messages: list[HistoryMessage]) -> list[HistoryMessage]: ...

    def is_current(self, scope: Scope, reference: RevisionRef) -> bool: ...


class RuleStore(Protocol):
    def search(self, scope: Scope, query: str) -> list[RuleHit]: ...

    def add_version(self, scope: Scope, rule: RuleHit) -> MutationReceipt: ...

    def deactivate(self, scope: Scope, doc_id: str, version: str,
                   expected_revision: int) -> MutationReceipt: ...

    def verify_citation(self, scope: Scope, citation: Citation) -> bool: ...

    def is_current(self, scope: Scope, reference: RevisionRef) -> bool: ...


class ContextBuilder(Protocol):
    def build(self, request: ContextRequest) -> ContextBundle: ...


class BusinessReader(Protocol):
    def list_invoices(self, scope: Scope, filters: InvoiceFilters | None = None) -> ToolResult: ...

    def list_bank_transactions(self, scope: Scope, filters: BankFilters | None = None) -> ToolResult: ...

    def get_record(self, scope: Scope, object_id: str) -> ToolResult: ...


class BusinessWriter(Protocol):
    def update_fields(self, scope: Scope, object_id: str, request: InvoiceSaveRequest) -> ToolResult: ...

    def get_operation(self, scope: Scope, operation_id: str) -> ToolResult: ...

    def reconcile_operation(self, scope: Scope, operation_id: str) -> ToolResult: ...
