"""Current-turn evidence and proposal validation, adapted from the competition guards."""
import re
import unicodedata
from types import SimpleNamespace
from decimal import Decimal

from amount_guards import amounts_supported, proposal_targets
from model_adapter import Requirement

KINDS = {
    "invoice_total": ("list_invoices", "invoice_cents", "发票合计"),
    "income_total": ("list_bank_transactions", "income_cents", "银行收入"),
    "expense_total": ("list_bank_transactions", "expense_cents", "银行支出"),
    "rules": ("search_rules", "rules", "适用规则"),
    "invoices": ("list_invoices", "records", "发票明细"),
    "review": ("check_purpose", "review", "核验结果"),
    "operation": ("get_operation", "operation", "保存状态"),
}


def required_by_text(text):
    specs = [("invoice_total", r"发票.{0,8}(合计|总额|汇总)"),
             ("income_total", r"(银行)?收入"), ("expense_total", r"(银行)?支出"),
             ("rules", r"规则|依据")]
    return [Requirement(requirement_id="required_" + kind, kind=kind) for kind, pattern in specs if re.search(pattern, text)]


def meaningful(text):
    return any(unicodedata.category(c)[0] in {"L", "N"} for c in text)


def target(text, pending):
    identifiers = re.findall(r"(?<![A-Za-z0-9_-])[A-Za-z]+[0-9]+(?:-[A-Za-z0-9_-]+)?(?![A-Za-z0-9_-])", text)
    identifiers = [v for v in identifiers if not re.fullmatch(r"C\d+", v, re.I)]
    mentioned, targets = proposal_targets(text, identifiers)
    if mentioned:
        return next(iter(targets)) if len(targets) == 1 else None
    return pending


def scope_conflict(text, scope, enterprises):
    if any(value != scope["enterprise_id"] for value in re.findall(r"\bC[0-9]{3}\b", text, re.I)):
        return True
    if any(row["enterprise_id"] != scope["enterprise_id"] and row["name"] in text for row in enterprises):
        return True
    # The demo uses Chinese ordering names. Ambiguous short names require a scope choice.
    for prefix in re.findall(r"([甲乙丙丁])(?:企业|公司|企)", text):
        matches = [row["enterprise_id"] for row in enterprises if row["name"].startswith(prefix)]
        if matches != [scope["enterprise_id"]]:
            return True
    return bool(re.search(r"(?:另一家|别的|其他)(?:企业|公司)", text))


def safe_reply(text, evidence):
    reads = []
    for item in evidence:
        data = dict(item["result"].get("data") or {})
        data.update(data.get("summary", {}))
        for key in ("invoice_cents", "income_cents", "expense_cents"):
            if key in data:
                data[key] = int(data[key])
        for record in data.get("records", []):
            if record.get("amount_cents") is not None:
                record["amount_cents"] = int(record["amount_cents"])
        if isinstance(data.get("record"), dict) and data["record"].get("amount_cents") is not None:
            data["record"]["amount_cents"] = int(data["record"]["amount_cents"])
        args = SimpleNamespace(direction=item["arguments"].get("direction"))
        reads.append((item["tool"], args, SimpleNamespace(data=data)))
    if not meaningful(text):
        return "empty_reply"
    if re.search(r"已(?:经)?(?:保存|写入|修改|补充|提出|生成)|建议已", text):
        if not any(item["tool"] == "get_operation" and item["result"]["data"]["operation"]["state"] == "confirmed"
                   for item in evidence):
            return "proposal_evidence_missing"
    if not amounts_supported(text, reads):
        return "amount_unverified"
    return None


def render(requirements, items, evidence):
    wanted = {r.requirement_id for r in requirements}
    ids = [item.requirement_id for item in items]
    if len(ids) != len(set(ids)) or set(ids) != wanted:
        raise ValueError("answer_items_incomplete")
    by_id = {r["evidence_id"]: r for r in evidence}
    lines = []
    for req in requirements:
        item = next(i for i in items if i.requirement_id == req.requirement_id)
        tool, key, label = KINDS[req.kind]
        if item.value_key != key or not item.evidence_ids:
            raise ValueError("evidence_mismatch")
        values = []
        for identifier in item.evidence_ids:
            proof = by_id.get(identifier)
            if proof is None or proof["tool"] != tool:
                raise ValueError("evidence_mismatch")
            expected = dict(req.filters)
            actual = dict(proof["arguments"])
            if req.kind == "rules" and not expected:
                actual.pop("query", None)
            if req.kind in ("income_total", "expense_total"):
                direction = "income" if req.kind == "income_total" else "expense"
                if actual.get("direction") not in (None, direction):
                    raise ValueError("direction_mismatch")
                actual.pop("direction", None)
                expected.pop("direction", None)
            if actual != expected:
                raise ValueError("filter_mismatch")
            data = proof["result"]["data"]
            if req.kind == "invoice_total" and int(data["summary"]["unknown_amounts"]) > 0:
                raise ValueError("invoice_amount_unknown")
            value = data.get("summary", {}).get(key, data.get(key))
            if key == "review":
                value = data
            if value is None:
                raise ValueError("evidence_mismatch")
            if key == "records" and data.get("has_more"):
                raise ValueError("list_incomplete")
            values.append(value)
        if any(value != values[0] for value in values):
            raise ValueError("evidence_conflict")
        value = values[0]
        if key.endswith("_cents"):
            lines.append(f"{label} {Decimal(value)/100:,.2f} 元")
        elif key == "rules":
            if not value:
                lines.append("当前范围没有适用规则，请提供材料供人工复核。")
            for hit in value:
                rule = hit["rule"]
                cite = rule["citation"]
                lines.append(f'{cite["source"]} · 版本 {cite["version"]} · 条款 {cite["clause"]}：{rule["content"]}')
        else:
            import json
            lines.append(label + " " + json.dumps(value, ensure_ascii=False))
    return "\n".join(lines)
