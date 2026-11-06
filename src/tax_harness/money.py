from decimal import Decimal, InvalidOperation


def to_cents(value: str | Decimal) -> int:
    """Accept exact yuan values. XLSX adapters must convert numeric cells via str."""
    if not isinstance(value, (str, Decimal)):
        raise ValueError("amount must be a decimal string or Decimal")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("amount must be finite and nonnegative")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError("amount has more than two decimal places")
    if cents > 9223372036854775807:
        raise ValueError("amount exceeds SQLite integer range")
    return int(cents)
