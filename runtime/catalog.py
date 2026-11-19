"""The closed list of this extension's steps: the generated ones (runtime/registry.json)
and the everyday ones (runtime/composites.py)."""

from __future__ import annotations

from . import composites
from .generic import registry

_REGISTRY = registry()

GENERATED_IDS: frozenset[str] = frozenset(_REGISTRY)
COMPOSITE_IDS: frozenset[str] = frozenset(composites.COMPOSITES)
OPERATION_IDS: frozenset[str] = GENERATED_IDS | COMPOSITE_IDS
#: Steps that change data in Allegro: every documented operation that is not a GET and
#: not one of the few the specification documents as changing nothing
#: (build_contracts.READ_ONLY_NON_GET), and the everyday batch update.
EFFECT_OPERATION_IDS: frozenset[str] = (
    frozenset(oid for oid, spec in _REGISTRY.items() if spec["kind"] == "write")
    | composites.COMPOSITE_WRITES
)
WRITE_OPERATION_IDS = EFFECT_OPERATION_IDS
#: Steps that may save a workflow file: their requests keep the file's time in reserve.
FILE_OUTPUT_IDS: frozenset[str] = frozenset(
    oid for oid, spec in _REGISTRY.items() if spec["answer"] == "file" or spec.get("formats")
) | frozenset({"list_all_my_offers", "list_orders"})

__all__ = [
    "COMPOSITE_IDS",
    "EFFECT_OPERATION_IDS",
    "FILE_OUTPUT_IDS",
    "GENERATED_IDS",
    "OPERATION_IDS",
    "WRITE_OPERATION_IDS",
]
