"""Money is integer paise end to end. Floats never touch prices."""

from __future__ import annotations

from pydantic import BaseModel, Field, computed_field


def format_inr(paise: int) -> str:
    """Indian digit grouping: 125000000 paise -> '₹12,50,000'. Paise shown only when non-zero."""
    sign = "-" if paise < 0 else ""
    rupees, rem = divmod(abs(paise), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])
    suffix = f".{rem:02d}" if rem else ""
    return f"{sign}₹{digits}{suffix}"


def rupees(amount: int) -> int:
    """Readability helper for literals: rupees(1500) == 150000 paise."""
    return amount * 100


class Price(BaseModel):
    paise: int = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display(self) -> str:
        return format_inr(self.paise)
