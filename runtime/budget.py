"""Keep one step's result inside what the host accepts.

The host scans every string of an extension response for reflected secrets and
fails closed past fixed budgets (core ``secret_reflection``: 3 Mi traversed nodes,
nesting depth 64, 64 Mi compared code points of distinct strings), and ``main.py``
caps the serialized response at 4.5 MiB (the host's default output cap is 5 MiB).
This module measures a result with tighter budgets, a margin for the envelope.

It measures exactly what is written: every ``Decimal`` as the text ``results.plain``
writes for it (``numbers.plain``: plain notation inside the bounds), counted as a
string, after the token was removed from the answer (``transport.json``), never as
``str(Decimal)``, whose exponential form is shorter than what is written.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from . import errors, numbers
from .errors import ExtensionError

MAX_CANDIDATE_STRINGS = 12_000
MAX_NODES = 48_000
MAX_CODEPOINTS = 5_000_000
MAX_SERIALIZED_BYTES = 3_500_000


def measure(value: Any) -> tuple[int, int, int, int]:
    """(candidate strings, nodes, string code points, serialized bytes) of ``value``."""
    candidates = nodes = codepoints = 0
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        nodes += 1
        if isinstance(node, Decimal):
            node = numbers.plain(node)
        if isinstance(node, str):
            candidates += 1
            codepoints += len(node)
        elif isinstance(node, dict):
            for key, nested in node.items():
                nodes += 1
                if isinstance(key, str):
                    candidates += 1
                    codepoints += len(key)
                stack.append(nested)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    size = len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_written).encode(
            "utf-8"
        )
    )
    return candidates, nodes, codepoints, size


def _written(item: Any) -> str:
    """What ``results.plain`` writes for a value JSON cannot hold (a ``Decimal``)."""
    return numbers.plain(item) if isinstance(item, Decimal) else str(item)


def _within(totals: tuple[int, int, int, int], scale: float = 1.0) -> bool:
    candidates, nodes, codepoints, size = totals
    return (
        candidates <= MAX_CANDIDATE_STRINGS * scale
        and nodes <= MAX_NODES * scale
        and codepoints <= MAX_CODEPOINTS * scale
        and size <= MAX_SERIALIZED_BYTES * scale
    )


def too_large() -> ExtensionError:
    return ExtensionError(
        errors.RESPONSE_TOO_LARGE,
        "This Allegro result is too large for one step output. Narrow it with the step's "
        "filters or read fewer records per page.",
        definitely_no_external_effect=True,
        external_effect_status="failed",
    )


def fits(result: Any) -> bool:
    return _within(measure(result))


def ensure_fits(result: Any) -> None:
    if not fits(result):
        raise too_large()


__all__ = ["ensure_fits", "fits", "measure", "too_large"]
