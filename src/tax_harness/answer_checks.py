"""Reject final replies that cite uncovered amounts or a pending proposal that will not remain."""

import re

from .contracts import ToolResult

_AMOUNT = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?\s*元")
_CLAUSE = re.compile(r"[。！？!?；;\n，、]")
_LABELS = {"income": "银行收入", "expense": "银行支出"}
_NEGATIONS = ("没有", "无", "不是", "并不", "不存在", "未形成", "不能")


def present_bank_result(arguments, result: ToolResult) -> ToolResult:
    data = result.data
    if result.status != "succeeded" or not data or "income_cents" not in data:
        return result
    shown = dict(data)
    direction = arguments.direction
    if direction is None and arguments.transaction_id is None:
        shown["covered_directions"] = ["income", "expense"]
        shown["totals_scope"] = "month"
    elif direction is None:
        shown["covered_directions"] = sorted({
            row["direction"] for row in shown.get("records") or [] if row.get("direction") in _LABELS
        })
        shown["totals_scope"] = "filtered"
    else:
        other = "expense" if direction == "income" else "income"
        shown.pop(f"{other}_cents", None)
        shown["covered_directions"] = [direction]
        shown["uncovered_directions"] = [other]
        shown["totals_scope"] = "direction"
    return result.model_copy(update={"data": shown})


def covered_bank_amounts(observations) -> tuple[dict[str, bool], dict[str, set[int]]]:
    covered = {"income": False, "expense": False}
    allowed = {"income": set(), "expense": set()}
    for item in observations:
        result = item["result"]
        if result.status != "succeeded" or not result.data:
            continue
        arguments = item["arguments"]
        records = result.data.get("records") or []
        direction = arguments.get("direction")
        if direction in _LABELS:
            directions = [direction]
        elif arguments.get("transaction_id"):
            directions = [row["direction"] for row in records if row.get("direction") in _LABELS]
        else:
            directions = ["income", "expense"]
        whole_month = arguments.get("transaction_id") is None
        for name in directions:
            covered[name] = True
            for record in records:
                if record.get("direction") == name and isinstance(record.get("amount_cents"), int):
                    allowed[name].add(record["amount_cents"])
            if whole_month:
                total = result.data.get(f"{name}_cents")
                if isinstance(total, int):
                    allowed[name].add(total)
    return covered, allowed


def _cents(whole: str, fraction: str | None) -> int:
    scale = (fraction or "").ljust(2, "0")
    return int(whole.replace(",", "")) * 100 + int(scale)


def direction_claims(reply: str) -> list[tuple[str, int]]:
    claims = []
    for clause in _CLAUSE.split(reply):
        amounts = [(match.start(), _cents(match.group(1), match.group(2))) for match in _AMOUNT.finditer(clause)]
        directions = []
        for name, token in (("income", "收入"), ("expense", "支出")):
            start = 0
            while True:
                index = clause.find(token, start)
                if index < 0:
                    break
                directions.append((index, name))
                start = index + len(token)
        if not amounts or not directions:
            continue
        labels = {name for _, name in directions}
        if len(labels) == 1:
            label = next(iter(labels))
            claims.extend((label, cents) for _, cents in amounts)
            continue
        for position, cents in amounts:
            claims.append((min(directions, key=lambda item: abs(item[0] - position))[1], cents))
    return claims


def amount_coverage_issue(reply: str, observations) -> str | None:
    covered, allowed = covered_bank_amounts(observations)
    problems = []
    for direction, cents in direction_claims(reply):
        label = _LABELS[direction]
        if not covered[direction]:
            problems.append(f"{label}尚未被查询覆盖，不能把未查询方向的金额写进回答。请先查询该方向或查询全部流水。")
        elif cents not in allowed[direction]:
            problems.append(f"{label}金额与已覆盖的查询结果不一致。请按工具返回的汇总重新回答，不要改写数字。")
    if not problems:
        return None
    return " ".join(dict.fromkeys(problems))


UNNAMED_INVOICE_QUESTION = "请提供要修改的发票编号。未在你的说明或已确认目标中出现的发票不能生成修改提案。"


def record_bank_observation(observations, name, arguments, result):
    if name != "list_bank_transactions":
        return result
    observations.append({"arguments": arguments.model_dump(), "result": result})
    return present_bank_result(arguments, result)


def inconsistent_reply(decision, observations) -> str | None:
    if getattr(decision, "kind", None) not in {"answer", "ask"}:
        return None
    return reply_consistency_issue(decision.reply, observations)


def invoice_mentioned(text: str, invoice_id: str) -> bool:
    if not text or not invoice_id:
        return False
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(invoice_id)}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None


def user_named_invoice(object_id: str | None, text: str, invoice_id: str, history: list[str]) -> bool:
    if object_id and object_id.casefold() == invoice_id.casefold():
        return True
    if invoice_mentioned(text, invoice_id):
        return True
    return any(invoice_mentioned(item, invoice_id) for item in history)


def user_invoice_history(database, scope) -> list[str]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT m.content FROM messages m JOIN tasks t ON t.task_id=m.task_id "
            "WHERE t.user_id=? AND t.enterprise_id=? AND t.month=? AND m.role='user'",
            (scope.user_id, scope.enterprise_id, scope.month),
        ).fetchall()
    return [row["content"] for row in rows]


def asserts_pending_proposal(reply: str) -> bool:
    if "旧提案" in reply and not any(phrase in reply for phrase in ("没有旧提案", "无旧提案", "不是旧提案")):
        return True
    for match in re.finditer("待确认", reply):
        window = reply[max(0, match.start() - 8):match.start()]
        if any(token in window for token in _NEGATIONS):
            continue
        return True
    return False


def reply_consistency_issue(reply: str, observations) -> str | None:
    parts = []
    coverage = amount_coverage_issue(reply, observations)
    if coverage:
        parts.append(coverage)
    if asserts_pending_proposal(reply):
        parts.append("当前回答声称仍有待确认提案，但这一步不会保留待确认修改。请按实际状态回答，不要声称旧提案仍待确认。")
    return " ".join(parts) or None
