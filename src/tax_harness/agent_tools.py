from pydantic import Field

from .contracts import BankFilters, Contract, Identifier, InvoiceFilters, InvoicePatch


class InvoiceLookup(Contract):
    invoice_id: Identifier


class RuleQuery(Contract):
    query: str = Field(min_length=1, max_length=500)


class SaveProposal(InvoiceLookup):
    patch: InvoicePatch


class OperationLookup(Contract):
    operation_id: Identifier


TOOL_INPUTS = {
    "list_invoices": (InvoiceFilters, "查询当前范围发票和发票总额"),
    "list_bank_transactions": (BankFilters, "查询当前范围流水并分别汇总收入和支出"),
    "get_invoice": (InvoiceLookup, "按编号读取当前范围发票"),
    "check_invoice_purposes": (InvoiceFilters, "按材料包现行规则检查发票用途并返回实际缺项"),
    "search_rules": (RuleQuery, "检索当前企业及月份适用的规则"),
    "get_operation": (OperationLookup, "查询已记录的保存操作"),
    "propose_invoice_update": (SaveProposal, "提出当前用户明确要求的用途或备注修改，交由用户确认"),
}

TOOL_DEFINITIONS = [
    {"name": name, "description": description, "arguments": schema.model_json_schema()}
    for name, (schema, description) in TOOL_INPUTS.items()
]
