"""The outcome of a write after it was sent: one table, decided in one place.

Every write of this extension (each generated step and Update stock and prices) goes
through ``write_outcome`` once Allegro answered it. The inputs are the status, the
Content-Type, the state of the body (empty / read and parsed / read and not parsed /
not fully read / nested deeper than 48 levels) and the class of the step. The first
row that applies decides:

- T1. A status the operation does not document as success never reaches this table:
  the transport refuses it (a documented refusal, or the outcome is unknown).
- T2. The specification documents only JSON for the status and the Content-Type is
  present and not JSON: the outcome is unknown (``invalid_response``,
  ``timeout_unknown``). Decided on the headers, whether or not the body was read.
- T3. The body was read, is not empty, JSON is documented, and it does not start with
  ``{`` or ``[`` or does not parse: the outcome is unknown. A confirmed write is kept
  despite size, time, depth and the output budget, never despite a body that is not
  what the specification documents.
- T4. The body was not fully read (limit, deadline, cut off) or nests deeper than 48
  levels, and T2 did not apply: by the class of the step. Every Allegro write is of
  the class "other" (T4 d): the change is confirmed by the status, so the step
  succeeds with ``data`` empty and a warning. The other classes do not occur here:
  Allegro confirms a write by the status, not inside the body (a); its batch writes
  register one command and answer one object, not a list of items (b); and no step
  needs an identifier from the body: a generated step returns ``data`` as it comes,
  and Update stock and prices sends its own command ID (c).
- T5. A parsed body of the wrong shape for the step: no Allegro step needs a shape
  (see T4), so nothing applies.
- T6. Otherwise: success; where the specification documents no JSON for the status
  a body that is not JSON is returned as text with a warning.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import errors, nesting
from .context import Context
from .errors import ExtensionError
from .transport import CHECK, Answer, TooDeep, unknown


def json_media(content_type: str) -> bool:
    return "json" in content_type.lower()


def _documented_json(entry: Mapping[str, Any], answer: Answer) -> bool:
    return str(answer.status) in entry.get("json_success", ())


def _starts_as_json(raw: bytes) -> bool:
    return raw[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")[:1] in {b"{", b"["}


def _not_documented(shown: str, answer: Answer, what: str) -> ExtensionError:
    return unknown(
        f"Allegro answered {shown} with HTTP {answer.status}, but {what}, not the JSON the "
        "specification documents for it, so the outcome is not known" + CHECK,
        errors.INVALID_RESPONSE,
        answer.status,
    )


def write_outcome(
    context: Context,
    entry: Mapping[str, Any],
    answer: Answer,
    shown: str,
    warnings: list[str],
) -> Any:
    """The ``data`` of a write Allegro answered with a documented success status (T2-T6
    above); raises when the outcome is unknown. Warnings are added to ``warnings``."""
    promised = _documented_json(entry, answer)
    # T2: on the headers, before and whatever the body.
    if promised and answer.content_type and not json_media(answer.content_type):
        raise _not_documented(shown, answer, f"its Content-Type is {answer.content_type}")
    # T4: not fully read.
    if answer.unread:
        warnings.append(
            f"Allegro confirmed {shown} (HTTP {answer.status}) but its answer {answer.unread}, "
            "so it is not returned (data is empty): see the record in Allegro"
        )
        return None
    if answer.empty:
        return None
    # T3: read, not empty, JSON documented, not JSON.
    if promised and not _starts_as_json(answer.raw):
        raise _not_documented(shown, answer, "its body does not start as JSON")
    try:
        return context.transport.json(answer)
    except TooDeep:
        # T4: deeper than the depth this extension reads.
        warnings.append(
            f"Allegro confirmed {shown} (HTTP {answer.status}) but its answer is nested "
            f"deeper than {nesting.MAX_ANSWER_DEPTH} levels, so it is not returned "
            "(data is empty); see the record in Allegro"
        )
        return None
    except ValueError:
        if promised:
            raise _not_documented(shown, answer, "its body is not valid JSON") from None
        # T6: no JSON documented for this status.
        warnings.append(
            f"Allegro confirmed {shown} (HTTP {answer.status}) but its answer is not "
            "JSON; it is returned as text"
        )
        return context.transport.text(answer, 20_000)


__all__ = ["json_media", "write_outcome"]
