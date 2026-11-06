from .contracts import Error, InvoiceFilters, Scope, ToolResult
from .ports import BusinessReader, RuleStore

PURPOSE_RULES = {"C001": ("R002", "2.0", 0), "C002": ("R003", "1.0", 100000)}


def check_invoice_purposes(scope: Scope, filters: InvoiceFilters, reader: BusinessReader, rules: RuleStore) -> ToolResult:
    invoices = reader.list_invoices(scope, filters).data
    if scope.month != "2026-09":
        return ToolResult(status="failed", error=Error(code="invalid_argument", message="初赛用途检查限于 2026 年 9 月，其他月份可查询金额。"))
    policy = PURPOSE_RULES.get(scope.enterprise_id)
    if policy is not None:
        doc_id, version, minimum_cents = policy
        selected = [rule for rule in rules.search(scope, "") if rule.citation.doc_id == doc_id]
        if len(selected) == 1 and selected[0].citation.version == version:
            records = [record for record in invoices["records"] if record["amount_cents"] >= minimum_cents
                       and not (record["purpose"] or "").strip()]
            return ToolResult(status="succeeded", data={"records": records, "missing_count": len(records),
                              "rules": [selected[0].model_dump()]})
    return ToolResult(status="failed", error=Error(code="not_found", message="当前缺少可执行的适用用途规则，请核对企业和规则版本。"))
