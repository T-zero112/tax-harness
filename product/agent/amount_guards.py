"""Check model claims and proposed targets against current-turn evidence."""

import re
from decimal import Decimal
from unicodedata import normalize


def mentioned_identifiers(text, identifiers):
    return {value for value in identifiers
            if re.search(rf"(?<![\w-]){re.escape(value)}(?![A-Za-z0-9_-])", text, re.IGNORECASE | re.ASCII)}


def proposal_targets(text, identifiers):
    mentioned = mentioned_identifiers(text, identifiers)
    # An unknown competition invoice number must override any pending target hint.
    for match in re.finditer(r"(?<![A-Za-z0-9_-])F[0-9]+(?![A-Za-z0-9_-])", text, re.IGNORECASE):
        if not any(value.casefold() == match[0].casefold() for value in mentioned):
            mentioned.add(match[0].upper())
    # Exclude identifiers explicitly withdrawn during a correction.
    targets = {value for value in mentioned if not re.search(
        rf"(?<![A-Za-z0-9_-]){re.escape(value)}(?![A-Za-z0-9_-])"
        r"\s*(?:的提案\s*)?(?:不改|不用|取消|不要|别改|作废)", text, re.IGNORECASE)}
    return mentioned, targets


_NUMBER = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_CURRENCY = re.compile(rf"(?<![\d.,])(?P<number>{_NUMBER})\s*(?P<unit>万元|元|分)(?!钟)")
_CURRENCY_PREFIX = re.compile(rf"[¥￥]\s*(?P<number>{_NUMBER})")
_LABEL = re.compile(r"(?P<label>银行收入|银行支出|收入|支出|进账|出账|发票(?:价税)?(?:合计|总额))")
_UNITLESS = re.compile(rf"{_LABEL.pattern}\s*(?:总额|合计)?\s*[:：为是]\s*[¥￥]?(?P<number>{_NUMBER})")
_CLAUSE = re.compile(r"[。；;，、\n]")
_THRESHOLD = re.compile(r"(?:及以上|及以下|以上|以下|起填|门槛|阈值)")


def _category(label):
    if "收入" in label or label == "进账":
        return "income_cents"
    if "支出" in label or label == "出账":
        return "expense_cents"
    return "invoice_cents"


def _cents(match, unit):
    return Decimal(match["number"].replace(",", "")) * {"分": 1, "元": 100, "万元": 1000000}[unit]


def amounts_supported(reply, reads, rule_texts=()):
    reply = normalize("NFKC", reply).replace("*", "").replace("`", "")
    amounts = {"income_cents": set(), "expense_cents": set(), "invoice_cents": set()}
    generic = set()
    for name, arguments, result in reads:
        data = result.data or {}
        if name == "list_bank_transactions":
            if arguments.direction in (None, "income"):
                amounts["income_cents"].add(data["income_cents"])
            if arguments.direction in (None, "expense"):
                amounts["expense_cents"].add(data["expense_cents"])
        if name == "list_invoices":
            amounts["invoice_cents"].add(data["invoice_cents"])
        generic.update(record["amount_cents"] for record in data.get("records", ()) if "amount_cents" in record)
        if "amount_cents" in data:
            generic.add(data["amount_cents"])
        if isinstance(data.get("record"), dict) and "amount_cents" in data["record"]:
            generic.add(data["record"]["amount_cents"])
    for values in amounts.values():
        generic.update(values)

    rule_amounts = {_cents(match, match["unit"]) for text in rule_texts for match in _CURRENCY.finditer(text)}
    currency_spans = set()
    matches = [(match, match["unit"]) for match in _CURRENCY.finditer(reply)]
    explicit = {match.span("number") for match, _ in matches}
    matches.extend((match, "元") for match in _CURRENCY_PREFIX.finditer(reply) if match.span("number") not in explicit)
    for match, unit in matches:
        currency_spans.add(match.span("number"))
        value = _cents(match, unit)
        before = _CLAUSE.split(reply[:match.start()])[-1]
        after = _CLAUSE.split(reply[match.end():], maxsplit=1)[0]
        if value in rule_amounts and _THRESHOLD.search(before + after):
            continue
        labels = list(_LABEL.finditer(before))
        allowed = amounts[_category(labels[-1]["label"])] if labels else generic
        if value not in allowed:
            return False
    return all(
        match.span("number") in currency_spans or _cents(match, "元") in amounts[_category(match["label"])]
        for match in _UNITLESS.finditer(reply)
    )
