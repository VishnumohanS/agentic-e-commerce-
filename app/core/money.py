"""Money helpers.

All amounts inside the system are integers in **minor units** (paise for INR).
Floating point is never used for money. Razorpay also expects minor units,
so no conversion happens at the payment boundary.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MINOR_UNITS_PER_MAJOR = 100
SUPPORTED_CURRENCIES = {"INR"}
CURRENCY_SYMBOLS = {"INR": "\u20b9"}


def to_minor(amount: str | int | float | Decimal) -> int:
    """Convert a major-unit amount (rupees) to minor units (paise)."""
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid monetary amount: {amount!r}") from exc
    minor = (value * MINOR_UNITS_PER_MAJOR).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(minor)


def to_major(minor: int) -> Decimal:
    """Convert minor units to a Decimal in major units."""
    return (Decimal(int(minor)) / Decimal(MINOR_UNITS_PER_MAJOR)).quantize(Decimal("0.01"))


def format_money(minor: int, currency: str = "INR") -> str:
    symbol = CURRENCY_SYMBOLS.get(currency.upper(), f"{currency.upper()} ")
    return f"{symbol}{to_major(minor):,.2f}"


def normalize_currency(currency: str) -> str:
    normalized = (currency or "").strip().upper()
    if normalized not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Unsupported currency: {currency!r}")
    return normalized
