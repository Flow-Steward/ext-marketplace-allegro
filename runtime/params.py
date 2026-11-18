"""Step inputs checked against the specification's parameters and turned into the request.

Each node comes from runtime/registry.json (built from the pinned specification by
tests/reference/build_contracts.py): its kind, whether it is required, its allowed
values and limits. Numbers stay Decimal to the wire; nothing goes through float.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import invalid

_PAIR_KEY_RE = re.compile(r"^[A-Za-z0-9._\-\[\]]{1,100}$")
MAX_PAIRS = 100


def text(value: Any, what: str, *, max_length: int, min_length: int = 0) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise invalid(f"{what} must be text")
    result = (
        value
        if isinstance(value, str)
        else (format(value, "f") if isinstance(value, Decimal) else str(value))
    )
    result = result.strip()
    if not result:
        raise invalid(f"{what} is empty")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in result):
        raise invalid(f"{what} may not contain control characters")
    if len(result) > max_length or len(result) < min_length:
        raise invalid(f"{what} must be {min_length or 1} to {max_length} characters")
    return result


def decimal(value: Any, what: str) -> Decimal:
    """An exact decimal from a JSON number (already Decimal) or text; never a float."""
    if isinstance(value, bool) or isinstance(value, float):
        raise invalid(f"{what} must be an exact decimal, written as text or a JSON number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"\s*-?\d{1,15}(\.\d{1,10})?\s*", value):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:  # pragma: no cover - the pattern rules it out
            raise invalid(f"{what} is not a number") from None
    else:
        raise invalid(f"{what} must be a number such as 49.99")
    if not number.is_finite():
        raise invalid(f"{what} must be a finite number")
    return number


def wire(value: Any) -> str:
    """A checked value as query text (Decimal plainly, booleans as JSON writes them)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def integer(node: Mapping[str, Any], value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise invalid(f"{what} must be a whole number")
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        value = int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,19}", value.strip()):
        value = int(value.strip())
    if not isinstance(value, int):
        raise invalid(f"{what} must be a whole number")
    low, high = node.get("minimum"), node.get("maximum")
    if (low is not None and value < low) or (high is not None and value > high):
        raise invalid(f"{what} must be a whole number from {low} to {high}")
    return value


def check(node: Mapping[str, Any], value: Any, what: str) -> Any:
    """One parameter value checked against its node: what to send (a list sends one
    query parameter per item, ``pairs`` one per key)."""
    kind = node["kind"]
    if kind == "text":
        result = text(
            value,
            what,
            max_length=int(node["max_length"]),
            min_length=int(node.get("min_length") or 0),
        )
        allowed = node.get("allowed")
        if allowed and result not in allowed:
            raise invalid(f"{what} must be one of: {', '.join(allowed)}")
        return result
    if kind == "integer":
        return integer(node, value, what)
    if kind == "decimal":
        number = decimal(value, what)
        low, high = node.get("minimum"), node.get("maximum")
        if low is not None and number < Decimal(str(low)):
            raise invalid(f"{what} may not be less than {low}")
        if high is not None and number > Decimal(str(high)):
            raise invalid(f"{what} may not be more than {high}")
        return number
    if kind == "boolean":
        if not isinstance(value, bool):
            raise invalid(f"{what} must be true or false")
        return value
    if kind == "text_list":
        items = value if isinstance(value, list) else [value]
        limit = int(node["max_items"])
        if not items or len(items) > limit:
            raise invalid(f"{what} must be a list of 1 to {limit} values")
        out = [
            text(item, f"{what} item {i}", max_length=int(node["item_max_length"]))
            for i, item in enumerate(items, 1)
        ]
        allowed = node.get("allowed")
        if allowed and any(item not in allowed for item in out):
            raise invalid(f"{what} values must be among: {', '.join(allowed)}")
        return out
    if kind == "pairs":
        if not isinstance(value, Mapping) or not value or len(value) > MAX_PAIRS:
            raise invalid(f"{what} must be an object of 1 to {MAX_PAIRS} name/value pairs")
        pairs: list[tuple[str, str]] = []
        for name, item in value.items():
            if not isinstance(name, str) or not _PAIR_KEY_RE.fullmatch(name):
                raise invalid(f"{what} has a name that is not usable as a query parameter")
            for one in item if isinstance(item, list) else [item]:
                pairs.append((name, wire(text(one, f"{what}.{name}", max_length=1000))))
        return pairs
    raise invalid(f"{what} has a kind this extension does not know")  # pragma: no cover


__all__ = ["check", "decimal", "integer", "text", "wire"]
