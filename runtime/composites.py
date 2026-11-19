"""The everyday steps, built on the documented operations of runtime/registry.json.

Each one goes through the same checks and transport as the generated step of the
operation it calls (generic.prepare / generic.to_call), so media types, documented
refusals and the token handling are the same.

Pagination reads page after page until the list is complete, the step output (or
the workflow file) is full, the asked maximum is reached or the time left is too
short; then it returns what it read with ``complete`` false and where to continue.
A read never fails because of that.

Update stock and prices writes with Allegro's batch commands. The command ID is the
idempotency key Allegro documents (a second PUT with a used ID answers 409 "Command
id was already used"), and here it is derived from the batch key and the command's
whole content, so a second run of the same batch registers nothing new.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from . import budget, errors, files, generic, nesting, outcome
from .context import Context
from .errors import ExtensionError, invalid
from .params import date_time, decimal, integer, text
from .transport import MIN_CALL_WINDOW_SECONDS, DeadlineReached, TooDeep, dumps_json

#: Fixed namespace of the command IDs (never change it: ids must stay reproducible).
COMMAND_NAMESPACE = uuid.UUID("5b7c2f0e-8a54-4c1b-9b8e-0f3d7a1c6e42")
MAX_OFFERS_PER_COMMAND = 1000
MAX_COMMANDS = 400
MAX_ROWS = 20000
MAX_FAILED_SHOWN = 1000
_OFFER_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,30}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
FILE_CONTENT_TYPE = "application/json"
MAX_FILE_BYTES = files.MAX_DOWNLOAD_BYTES - 1024


# -- helpers -----------------------------------------------------------------------------


def _read(context: Context, operation_id: str, given: Mapping[str, Any]) -> Any:
    """One documented read; its JSON value (an error when it is not JSON)."""
    entry = generic.spec(operation_id)
    plan = generic.prepare(operation_id, given)
    answer = context.transport.read(generic.to_call(context, entry, plan))
    try:
        return context.transport.json(answer)
    except ValueError as exc:
        what = (
            f"JSON nested deeper than {nesting.MAX_ANSWER_DEPTH} levels"
            if isinstance(exc, TooDeep)
            else "something that is not JSON"
        )
        raise errors.no_effect(
            errors.INVALID_RESPONSE,
            f"Allegro answered {plan.shown} with {what}",
            failure_class="provider",
            retryable=False,
        ) from None


def _bad(shown: str, what: str) -> ExtensionError:
    return errors.no_effect(
        errors.INVALID_RESPONSE,
        f"Allegro's answer to {shown} {what}",
        failure_class="provider",
        retryable=False,
    )


def _room(context: Context) -> bool:
    """Whether another page can still be started and read in time."""
    return context.transport.remaining() >= MIN_CALL_WINDOW_SECONDS + 15


def _opt_int(given: Mapping[str, Any], name: str, *, default: int, low: int, high: int) -> int:
    value = given.get(name)
    if value is None or value == "":
        return default
    return integer({"minimum": low, "maximum": high}, value, name)


def _list_of_text(
    given: Mapping[str, Any],
    name: str,
    *,
    max_items: int,
    max_length: int,
    allowed: list[str] | None = None,
) -> list[str]:
    value = given.get(name)
    if value is None or value == [] or value == "":
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise invalid(f"{name} must be a list of at most {max_items} values")
    out = [text(v, f"{name} item {i}", max_length=max_length) for i, v in enumerate(value, 1)]
    if allowed and any(v not in allowed for v in out):
        raise invalid(f"{name} values must be among: {', '.join(allowed)}")
    return out


def _closed(given: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
    rest = {k: v for k, v in given.items() if k != "connection_ref"}
    unknown = sorted(set(rest) - allowed)
    if unknown:
        raise invalid(f"The step input has unknown fields: {', '.join(unknown[:10])}")
    return rest


# -- test connection ---------------------------------------------------------------------


def _me(context: Context) -> dict[str, Any]:
    data = _read(context, "get_my_account", {})
    if (
        not isinstance(data, Mapping)
        or not isinstance(data.get("id"), str)
        or not data["id"].strip()
    ):
        raise _bad("GET /me", "carries no user id")
    return dict(data)


def test_connection(context: Context) -> dict[str, Any]:
    _closed(context.operation_input, set())
    me = _me(context)
    login = me.get("login")
    return {
        "connected": True,
        "user_id": errors.safe_text(me["id"], 64),
        "login": errors.safe_text(login, 100) if isinstance(login, str) else None,
        "environment": context.connection.environment,
        "warnings": [],
    }


def verify_identity(context: Context) -> dict[str, Any]:
    """The post-connect action: which Allegro account the authorization is for."""
    me = _me(context)
    login = me.get("login")
    return {
        "subject": errors.safe_text(me["id"], 64),
        "verified_display_name": errors.safe_text(login, 100) if isinstance(login, str) else None,
        "health": "ok",
    }


# -- pages -------------------------------------------------------------------------------


#: The longest warning a list step adds when it stops; the budget keeps room for it.
MAX_STOP_WARNING = 600
_STOP_RESERVE = "x" * MAX_STOP_WARNING


class Pages:
    """Collects the records of a paginated read into the step output or a workflow file.

    The whole result is measured, not only the records (N1): its envelope (``envelope``,
    with the widest values the step can put there, the warnings so far, the pending
    ones and room for the closing warning) plus each record as it is added. So the
    final budget check never refuses what was taken here. Each record is walked once."""

    def __init__(self, to_file: bool, key: str, envelope: dict[str, Any]) -> None:
        self.to_file = to_file
        self.key = key
        self.envelope = envelope
        self.items: list[Any] = []
        self.size = 2
        self.records = budget.Totals()
        self.warnings: list[str] = []
        self.complete = False

    def take(self, page: list[Any], pending: list[str]) -> bool:
        """Add a page and its pending warnings; False (nothing added) when they do not fit."""
        if self.to_file:
            size = self.size + sum(len(dumps_json(item).encode("utf-8")) + 1 for item in page)
            if size > MAX_FILE_BYTES:
                return False
            self.size = size
        else:
            added = self.records
            for item in page:
                cost = budget.measure(item)
                # One level for the list, one for the result object.
                added = added + budget.Totals(cost.nodes, cost.depth + 2, cost.size + 1)
            frame = budget.measure(
                {
                    **self.envelope,
                    self.key: [],
                    "warnings": [*self.warnings, *pending, _STOP_RESERVE],
                }
            )
            if not (added + frame).within():
                return False
            self.records = added
        self.items += page
        self.warnings += pending
        return True


def _stop_warning(text: str) -> str:
    return errors.safe_text(text, MAX_STOP_WARNING)


#: Read failures after which the pages already read are returned (HTTP 429, 408 and
#: 5xx, network failures and time-outs of a read): the read can be continued later.
def _transient(exc: ExtensionError) -> bool:
    return exc.retryable is True


def _too_large_answer(exc: ExtensionError) -> bool:
    """Allegro's answer was larger than the transport reads (a read: nothing changed)."""
    return exc.code == errors.RESPONSE_TOO_LARGE and exc.definitely_no_external_effect is True


def _out_of_time(context: Context) -> ExtensionError:
    """No page fitted before the time ran out, after requests were already made."""
    return errors.no_effect(
        errors.TIMEOUT,
        f"The step ran out of time after {context.transport.calls} requests to Allegro while "
        "asking for a page small enough to return; nothing was changed. Run it again with "
        "narrower filters",
    )


def _finish(context: Context, pages: Pages, key: str, extra: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"count": len(pages.items), **extra}
    if pages.to_file:
        handle = None
        if pages.items:
            handle = files.save(
                context,
                dumps_json(pages.items).encode("utf-8"),
                content_type=FILE_CONTENT_TYPE,
                what="The list",
            )
        result[key] = []
        result["file"] = handle
    else:
        result[key] = pages.items
        result["file"] = None
    result["warnings"] = pages.warnings
    return result


def _offset_pages(
    context: Context,
    *,
    operation_id: str,
    query: dict[str, Any],
    list_key: str,
    page_size: int,
    start: int,
    most: int,
    to_file: bool,
    out_key: str,
) -> dict[str, Any]:
    """Pages read by offset until the list ends.

    A page that does not fit the output (or that Allegro answers larger than the
    transport reads, on the first page) is asked again with half as many records,
    down to one; after a page fits, the next one is asked at the full size again (N2).
    ``next_offset`` is always the first record not returned. An empty page while
    Allegro counts more records than were read is an error, never the end (N3)."""
    shown = f"{generic.spec(operation_id)['method']} {generic.spec(operation_id)['path']}"
    envelope = {
        "count": 10**9,
        "total_count": 10**12,
        "complete": False,
        "next_offset": 10**12,
        "file": "x" * 100,
    }
    pages = Pages(to_file, out_key, envelope)
    offset = start
    total: int | None = None
    seen: set[str] = set()
    first = True
    stopped = ""
    size = page_size
    while True:
        if len(pages.items) >= most:
            stopped = f"the {most} asked for were read"
            break
        if context.transport.calls and not _room(context):
            if first:
                raise _out_of_time(context)
            stopped = "time ran out"
            break
        limit = min(size, most - len(pages.items))
        try:
            data = _read(context, operation_id, {**query, "limit": limit, "offset": offset})
        except DeadlineReached:
            if first and context.transport.calls:
                raise _out_of_time(context) from None
            if first:
                raise
            stopped = "time ran out"
            break
        except ExtensionError as exc:
            if _too_large_answer(exc) and first and limit > 1:
                size = max(1, limit // 2)
                continue
            if first or not (_transient(exc) or _too_large_answer(exc)):
                raise
            stopped = f"Allegro could not give the next page ({exc.code}: {exc.message})"
            break
        if not isinstance(data, Mapping) or not isinstance(data.get(list_key), list):
            raise _bad(shown, f"has no {list_key} list")
        page = data[list_key]
        count_total = data.get("totalCount")
        if isinstance(count_total, (int, Decimal)) and not isinstance(count_total, bool):
            total = int(count_total)
        if not page and total is not None and offset < total:
            raise _bad(
                shown,
                f"is an empty page at offset {offset} while it counts {total} records; the read "
                "is not complete",
            )
        fresh = []
        for item in page:
            ident = item.get("id") if isinstance(item, Mapping) else None
            if not isinstance(ident, str):
                raise _bad(shown, "lists a record without an id")
            if ident in seen:
                continue
            fresh.append(item)
        pending = []
        if len(fresh) != len(page):
            pending.append(
                f"{len(page) - len(fresh)} records appeared twice because the list changed while "
                "it was read; each is returned once"
            )
        if not pages.take(fresh, pending):
            if limit > 1:
                # The page does not fit: ask again for half as many records.
                size = max(1, limit // 2)
                continue
            if first:
                raise budget.too_large()
            stopped = "the output is full" if not to_file else "the file is full"
            break
        seen.update(item["id"] for item in fresh)
        first = False
        size = page_size
        offset += len(page)
        if len(page) < limit or (total is not None and offset >= total) or not page:
            pages.complete = True
            break
    if not pages.complete:
        pages.warnings.append(
            _stop_warning(
                f"Stopped early because {stopped}; run the step again from next_offset "
                f"{offset} for the rest"
            )
        )
    return _finish(
        context,
        pages,
        out_key,
        {
            "total_count": total,
            "complete": pages.complete,
            "next_offset": None if pages.complete else offset,
        },
    )


def _output_choice(given: Mapping[str, Any]) -> bool:
    value = given.get("output")
    if value in (None, "", "step"):
        return False
    if value == "file":
        return True
    raise invalid("output must be step or file")


def list_all_my_offers(context: Context) -> dict[str, Any]:
    given = _closed(
        context.operation_input,
        {
            "name",
            "offer_ids",
            "external_ids",
            "publication_status",
            "category_id",
            "start_offset",
            "max_offers",
            "output",
        },
    )
    query: dict[str, Any] = {}
    if given.get("name") not in (None, ""):
        query["name"] = text(given["name"], "name", max_length=200)
    if given.get("category_id") not in (None, ""):
        query["category_id"] = text(given["category_id"], "category_id", max_length=30)
    offer_ids = _list_of_text(given, "offer_ids", max_items=1000, max_length=30)
    external = _list_of_text(given, "external_ids", max_items=1000, max_length=100)
    statuses = _list_of_text(
        given,
        "publication_status",
        max_items=4,
        max_length=20,
        allowed=["INACTIVE", "ACTIVATING", "ACTIVE", "ENDED"],
    )
    if offer_ids:
        query["offer_id"] = offer_ids
    if external:
        query["external_id"] = external
    if statuses:
        query["publication_status"] = statuses
    return _offset_pages(
        context,
        operation_id="get_offers_page",
        query=query,
        list_key="offers",
        page_size=1000,
        start=_opt_int(given, "start_offset", default=0, low=0, high=9_999_999),
        most=_opt_int(given, "max_offers", default=100_000, low=1, high=100_000),
        to_file=_output_choice(given),
        out_key="offers",
    )


_ORDER_FILTERS = {
    "status": "status",
    "fulfillment_status": "fulfillment_status",
    "updated_from": "updated_at_gte",
    "updated_to": "updated_at_lte",
    "bought_from": "line_items_bought_at_gte",
    "bought_to": "line_items_bought_at_lte",
    "buyer_login": "buyer_login",
    "sort": "sort",
}
_DATE_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$")
_ORDER_CHOICES = {
    "status": ["BOUGHT", "FILLED_IN", "READY_FOR_PROCESSING", "CANCELLED"],
    "fulfillment_status": [
        "NEW",
        "PROCESSING",
        "READY_FOR_SHIPMENT",
        "READY_FOR_PICKUP",
        "SENT",
        "PICKED_UP",
        "CANCELLED",
        "SUSPENDED",
        "RETURNED",
    ],
    "sort": ["lineItems.boughtAt", "-lineItems.boughtAt", "updatedAt", "-updatedAt"],
}


def list_orders(context: Context) -> dict[str, Any]:
    given = _closed(
        context.operation_input, set(_ORDER_FILTERS) | {"start_offset", "max_orders", "output"}
    )
    query: dict[str, Any] = {}
    for name, wire_input in _ORDER_FILTERS.items():
        value = given.get(name)
        if value in (None, ""):
            continue
        checked = text(value, name, max_length=100)
        if name in _ORDER_CHOICES and checked not in _ORDER_CHOICES[name]:
            raise invalid(f"{name} must be one of: {', '.join(_ORDER_CHOICES[name])}")
        if name.endswith(("_from", "_to")):
            if not _DATE_TIME_RE.fullmatch(checked):
                raise invalid(f"{name} must be a date and time such as 2026-11-01T00:00:00Z")
            date_time(checked, name)
        query[wire_input] = checked
    return _offset_pages(
        context,
        operation_id="get_orders_page",
        query=query,
        list_key="checkoutForms",
        page_size=100,
        start=_opt_int(given, "start_offset", default=0, low=0, high=9_999_999),
        most=_opt_int(given, "max_orders", default=5000, low=1, high=20_000),
        to_file=_output_choice(given),
        out_key="orders",
    )


EVENT_TYPES = [
    "BOUGHT",
    "FILLED_IN",
    "READY_FOR_PROCESSING",
    "BUYER_CANCELLED",
    "FULFILLMENT_STATUS_CHANGED",
    "BUYER_MODIFIED",
    "AUTO_CANCELLED",
]


def list_order_events(context: Context) -> dict[str, Any]:
    given = _closed(context.operation_input, {"after_event_id", "types", "max_events"})
    after = given.get("after_event_id")
    after = text(after, "after_event_id", max_length=64) if after not in (None, "") else None
    types = _list_of_text(given, "types", max_items=7, max_length=40, allowed=EVENT_TYPES)
    most = _opt_int(given, "max_events", default=10_000, low=1, high=50_000)
    envelope = {"count": 10**9, "last_event_id": "x" * 64, "complete": False}
    pages = Pages(False, "events", envelope)
    last = after
    first = True
    stopped = ""
    size = 1000
    while True:
        if len(pages.items) >= most:
            stopped = f"the {most} asked for were read"
            break
        if context.transport.calls and not _room(context):
            if first:
                raise _out_of_time(context)
            stopped = "time ran out"
            break
        limit = min(size, most - len(pages.items))
        query: dict[str, Any] = {"limit": limit}
        if last:
            query["from"] = last
        if types:
            query["type"] = types
        try:
            data = _read(context, "get_order_events_page", query)
        except DeadlineReached:
            if first and context.transport.calls:
                raise _out_of_time(context) from None
            if first:
                raise
            stopped = "time ran out"
            break
        except ExtensionError as exc:
            if _too_large_answer(exc) and first and limit > 1:
                size = max(1, limit // 2)
                continue
            if first or not (_transient(exc) or _too_large_answer(exc)):
                raise
            stopped = f"Allegro could not give the next page ({exc.code}: {exc.message})"
            break
        if not isinstance(data, Mapping) or not isinstance(data.get("events"), list):
            raise _bad("GET /order/events", "has no events list")
        page = data["events"]
        ids = [e.get("id") if isinstance(e, Mapping) else None for e in page]
        if any(not isinstance(i, str) or not i for i in ids):
            raise _bad("GET /order/events", "lists an event without an id")
        if last is not None and last in ids:
            raise _bad("GET /order/events", f"repeats the event {last} it was asked to read after")
        if not pages.take(page, []):
            if limit > 1:
                # The page does not fit: ask again for half as many events.
                size = max(1, limit // 2)
                continue
            if first:
                raise budget.too_large()
            stopped = "the output is full"
            break
        first = False
        size = 1000
        if page:
            last = ids[-1]
        if len(page) < limit:
            pages.complete = True
            break
    if not pages.complete:
        pages.warnings.append(
            _stop_warning(
                f"Stopped early because {stopped}; run the step again with after_event_id "
                f"{last} for the rest"
            )
        )
    result = _finish(context, pages, "events", {"last_event_id": last, "complete": pages.complete})
    result.pop("file", None)
    return result


# -- update stock and prices -------------------------------------------------------------


class Command:
    def __init__(
        self, kind: str, value: str, currency: str | None, offers: list[str], batch_key: str
    ) -> None:
        self.kind = kind
        self.value = value
        self.currency = currency
        self.offers = offers
        content = json.dumps([batch_key, kind, value, currency, offers], separators=(",", ":"))
        self.command_id = str(uuid.uuid5(COMMAND_NAMESPACE, content))

    def body(self) -> dict[str, Any]:
        criteria = [{"type": "CONTAINS_OFFERS", "offers": [{"id": o} for o in self.offers]}]
        if self.kind == "quantity":
            modification: dict[str, Any] = {"changeType": "FIXED", "value": int(self.value)}
        else:
            modification = {
                "type": "FIXED_PRICE",
                "price": {"amount": self.value, "currency": self.currency},
            }
        return {"modification": modification, "offerCriteria": criteria}

    def summary(self, status: str) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "kind": self.kind,
            "value": self.value,
            "currency": self.currency,
            "offers_count": len(self.offers),
            "status": status,
            "outcome": None,
        }


def _price(value: Any, what: str) -> str:
    number = decimal(value, what)
    if number <= 0 or number >= Decimal(1000000000):
        raise invalid(f"{what} must be more than 0 and less than 1000000000")
    if number != number.quantize(Decimal("0.01")):
        raise invalid(f"{what} may have at most two decimal places")
    return format(number.quantize(Decimal("0.01")), "f")


def plan_update(operation_input: Mapping[str, Any]) -> list[Command]:
    """The commands for the rows, checked in full before anything is sent."""
    given = _closed(operation_input, {"rows", "batch_key"})
    batch_key = text(given.get("batch_key") or "", "batch_key", max_length=100)
    rows = given.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        raise invalid(f"rows must be a list of 1 to {MAX_ROWS} offers")
    quantities: dict[str, list[str]] = {}
    prices: dict[tuple[str, str], list[str]] = {}
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        what = f"rows item {index}"
        if not isinstance(row, Mapping):
            raise invalid(f"{what} must be an object")
        extra = sorted(set(row) - {"offer_id", "quantity", "price", "currency"})
        if extra:
            raise invalid(f"{what} has unknown fields: {', '.join(extra)}")
        offer = text(row.get("offer_id") or "", f"{what}.offer_id", max_length=30)
        if not _OFFER_ID_RE.fullmatch(offer):
            raise invalid(f"{what}.offer_id is not an Allegro offer ID")
        if offer in seen:
            raise invalid(f"{what}: offer {offer} appears twice; give each offer once")
        seen.add(offer)
        quantity, price = row.get("quantity"), row.get("price")
        if quantity in (None, "") and price in (None, ""):
            raise invalid(f"{what}: give quantity, price or both")
        if quantity not in (None, ""):
            amount = integer({"minimum": 0, "maximum": 1_000_000}, quantity, f"{what}.quantity")
            if amount == 0:
                raise invalid(
                    f"{what}.quantity is 0: Allegro's stock command sets 1 or more. End the "
                    "offer instead (Publish or end offers)"
                )
            quantities.setdefault(str(amount), []).append(offer)
        currency = row.get("currency")
        if price in (None, ""):
            if currency not in (None, ""):
                raise invalid(f"{what}: currency is given without a price")
            continue
        currency = "PLN" if currency in (None, "") else currency
        if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
            raise invalid(f"{what}.currency must be three capital letters such as PLN")
        prices.setdefault((_price(price, f"{what}.price"), currency), []).append(offer)
    commands: list[Command] = []
    for value, offers in sorted(quantities.items(), key=lambda kv: int(kv[0])):
        commands += _chunks("quantity", value, None, offers, batch_key)
    for (value, currency), offers in sorted(
        prices.items(), key=lambda kv: (kv[0][1], Decimal(kv[0][0]))
    ):
        commands += _chunks("price", value, currency, offers, batch_key)
    if len(commands) > MAX_COMMANDS:
        raise invalid(
            f"The rows need {len(commands)} commands (one per distinct stock or price, up to "
            f"{MAX_OFFERS_PER_COMMAND} offers each); split them into batches of at most "
            f"{MAX_COMMANDS} commands"
        )
    return commands


def _chunks(
    kind: str, value: str, currency: str | None, offers: list[str], key: str
) -> list[Command]:
    ordered = sorted(offers)
    return [
        Command(kind, value, currency, ordered[i : i + MAX_OFFERS_PER_COMMAND], key)
        for i in range(0, len(ordered), MAX_OFFERS_PER_COMMAND)
    ]


_COMMAND_STEP = {"quantity": "change_stock_of_many_offers", "price": "change_price_of_many_offers"}


def update_stock_and_prices(context: Context) -> dict[str, Any]:
    commands = plan_update(context.operation_input)
    done: list[dict[str, Any]] = []
    warnings: list[str] = []
    for command in commands:
        operation_id = _COMMAND_STEP[command.kind]
        entry = generic.spec(operation_id)
        plan = generic.prepare(
            operation_id, {"command_id": command.command_id, "body": command.body()}
        )
        try:
            answer = context.transport.write(generic.to_call(context, entry, plan))
        except DeadlineReached:
            # Raised before the request left: this command was certainly not sent.
            if not done:
                raise _midway(
                    done, commands, command, "the step ran out of time", unsure=False
                ) from None
            return _partial(done, commands, command, "the step ran out of time", warnings)
        except ExtensionError as exc:
            if exc.http_status == 409 and exc.definitely_no_external_effect:
                # Allegro already has this command ID: nothing was sent again. Its
                # outcome is read back for this row.
                row = command.summary("already_registered")
                row["outcome"] = _outcome_of(context, command)
                done.append(row)
                continue
            if exc.definitely_no_external_effect and done:
                # A definite refusal after confirmed commands: those stay registered.
                return _partial(done, commands, command, exc.message, warnings, refused=True)
            raise _midway(
                done,
                commands,
                command,
                exc.message,
                unsure=exc.definitely_no_external_effect is False,
                base=exc,
            ) from None
        try:
            echoed = outcome.write_outcome(context, entry, answer, plan.shown, warnings)
        except ExtensionError as exc:
            # A documented success with a body that is not the documented JSON.
            raise _midway(done, commands, command, exc.message, unsure=True, base=exc) from None
        if not isinstance(echoed, Mapping) or echoed.get("id") != command.command_id:
            warnings.append(
                f"Allegro registered command {command.command_id} (HTTP {answer.status}) but its "
                "answer does not repeat the command ID; check it with Get stock and price "
                "update results"
            )
        done.append(command.summary("registered"))
    _say_again(done, warnings)
    # The commands are registered; Allegro applies them afterwards, and Get stock and
    # price update results reads the outcome per offer.
    return {
        "commands": done,
        "offers_count": sum(c["offers_count"] for c in done),
        "processing": "accepted",
        "applied": False,
        "check": {"operation_id": "get_stock_and_price_update_results", "location": None},
        "retry_after_seconds": None,
        "warnings": warnings,
    }


def _outcome_of(context: Context, command: Command) -> dict[str, Any] | str:
    """How far Allegro got with a command registered by an earlier run (its summary);
    ``unknown`` when it cannot be read now."""
    if not _room(context):
        return "unknown"
    summary_step = _REPORT_STEPS[command.kind][0]
    try:
        summary = _read(context, summary_step, {"command_id": command.command_id})
        if not isinstance(summary, Mapping) or not isinstance(summary.get("taskCount"), Mapping):
            return "unknown"
        counts = summary["taskCount"]
        total, success, failed = (_count(counts.get(k, 0)) for k in ("total", "success", "failed"))
    except (ExtensionError, DeadlineReached):
        return "unknown"
    return {
        "completed": summary.get("completedAt") not in (None, ""),
        "total": total,
        "success": success,
        "failed": failed,
        "waiting": max(0, total - success - failed),
    }


def _say_again(done: list[dict[str, Any]], warnings: list[str]) -> None:
    again = sum(1 for c in done if c["status"] == "already_registered")
    if again:
        warnings.append(
            f"This batch key was already used: {again} of {len(done)} commands were registered "
            "by an earlier run and nothing was sent again for them (their outcome is in "
            "outcome; unknown when it could not be read). Use a new batch key for every "
            "update. "
            f"{again} of {len(done)} commands were already registered by an earlier run with the "
            "same batch key and rows; they were not applied again"
        )


def _partial(
    done: list[dict[str, Any]],
    commands: list[Command],
    failed: Command,
    reason: str,
    warnings: list[str],
    *,
    refused: bool = False,
) -> dict[str, Any]:
    """Some commands registered, then one certainly refused or certainly not sent.

    The registered ones are changes Allegro confirmed, so the step succeeds; each
    command has its status (registered, already_registered, refused, not_sent) and
    ``applied`` is unknown (null) because part of the batch is not registered."""
    rows = list(done)
    rows.append(failed.summary("refused" if refused else "not_sent"))
    rows += [c.summary("not_sent") for c in commands[len(done) + 1 :]]
    _say_again(done, warnings)
    registered = sum(1 for c in done if c["status"] in {"registered", "already_registered"})
    warnings.append(
        f"Only {registered} of {len(commands)} commands are registered: command "
        f"{failed.command_id} ({failed.kind} {failed.value}) "
        + ("was refused" if refused else "was not sent")
        + f": {errors.safe_text(reason, 300)}; {len(commands) - len(done) - 1} more were not "
        "sent. Fix the cause and run the step again with the same rows and batch key: "
        "registered commands are not applied twice"
    )
    return {
        "commands": rows,
        "offers_count": sum(c["offers_count"] for c in done),
        "processing": "accepted",
        "applied": None,
        "check": {"operation_id": "get_stock_and_price_update_results", "location": None},
        "retry_after_seconds": None,
        "warnings": warnings,
    }


#: The host keeps an error message of at most 512 characters (core step_failures).
MAX_ERROR_MESSAGE = 500
MAX_IDS_IN_MESSAGE = 5


def _midway(
    done: list[dict[str, Any]],
    commands: list[Command],
    failed: Command,
    reason: str,
    *,
    unsure: bool,
    base: ExtensionError | None = None,
) -> ExtensionError:
    """A failure part way: what was registered, what failed, what was not sent.

    The host's error envelope has no structured field for the registered command IDs
    (its error rows keep only code, message, path and selector), so they are in the
    text, bounded so the whole message stays under the 512 characters the host keeps."""
    registered = [c["command_id"] for c in done]
    left = len(commands) - len(done) - 1
    if not done and not unsure:
        if base is not None:
            return base
        return errors.no_effect(
            errors.TIMEOUT, "The step ran out of time before Allegro was called"
        )
    shown = ", ".join(registered[:MAX_IDS_IN_MESSAGE])
    if len(registered) > MAX_IDS_IN_MESSAGE:
        shown += f" and {len(registered) - MAX_IDS_IN_MESSAGE} more"
    head = f"{len(done)} of {len(commands)} commands are registered" + (
        f" ({shown})" if registered else ""
    )
    before = f"; command {failed.command_id} ({failed.kind}) " + (
        "may or may not be registered" if unsure else "was not registered"
    )
    after = (
        f"; {left} were not sent. Run the step again with the same rows and batch key: "
        "registered commands are not applied twice"
    )
    room = MAX_ERROR_MESSAGE - len(head) - len(before) - len(after) - 2
    said = f": {errors.safe_text(reason, room)}" if room >= 20 else ""
    message = head + before + said + after
    return ExtensionError(
        base.code
        if base is not None and base.code != errors.INVALID_PAYLOAD
        else errors.TIMEOUT_UNKNOWN,
        message[:MAX_ERROR_MESSAGE],
        failure_class="provider",
        retryable=False,
        definitely_no_external_effect=False,
        external_effect_status="timeout_unknown",
        http_status=base.http_status if base is not None else None,
    )


def validate_update(operation_input: Mapping[str, Any]) -> None:
    plan_update(operation_input)


# -- update results ----------------------------------------------------------------------

_REPORT_STEPS = {
    "quantity": ("get_stock_change_command_summary", "get_stock_change_command_report"),
    "price": ("get_price_change_command_summary", "get_price_change_command_report"),
}


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value < 0:
        raise _bad("the command summary", "has a count that is not a whole number")
    return int(value)


def get_stock_and_price_update_results(context: Context) -> dict[str, Any]:
    given = _closed(context.operation_input, {"commands"})
    commands = given.get("commands")
    if not isinstance(commands, list) or not commands or len(commands) > 500:
        raise invalid("commands must be a list of 1 to 500 commands")
    asked: list[tuple[str, str]] = []
    for index, item in enumerate(commands, 1):
        if not isinstance(item, Mapping):
            raise invalid(f"commands item {index} must be an object")
        command_id = text(
            item.get("command_id") or "", f"commands item {index}.command_id", max_length=64
        )
        kind = item.get("kind")
        if kind not in _REPORT_STEPS:
            raise invalid(f"commands item {index}.kind must be quantity or price")
        asked.append((command_id, str(kind)))
    out: list[dict[str, Any]] = []
    warnings: list[str] = []
    unread: list[str] = []
    first_failure: ExtensionError | None = None
    # The whole result is measured (N1): its envelope with every command ID listed as
    # unread and the closing warning, then each row and warning as it is added.
    totals = budget.measure(
        _results([], asked, [c for c, _ in asked], [_FULL_RESERVE, _FULL_RESERVE])
    )
    noted = 0
    for index, (command_id, kind) in enumerate(asked):
        if index and not _room(context):
            unread += [c for c, _ in asked[index:]]
            warnings.append(
                f"Time ran out: {len(asked) - index} commands were not read; run the step again "
                "with unread_command_ids"
            )
            break
        try:
            row = _command_result(context, command_id, kind, warnings)
        except (ExtensionError, DeadlineReached) as exc:
            # One command that cannot be read (404, 429, a time-out, an invalid answer)
            # does not hide the others: its row says why.
            reason = (
                "the step ran out of time"
                if isinstance(exc, DeadlineReached)
                else f"{exc.code}: {errors.safe_text(exc.message, 300)}"
            )
            if isinstance(exc, ExtensionError) and first_failure is None:
                first_failure = exc
            row = {"command_id": command_id, "kind": kind, "status": "unread", "reason": reason}
            unread.append(command_id)
        cost = budget.measure(row)
        added = totals + budget.Totals(cost.nodes, cost.depth + 2, cost.size + 1)
        for warning in warnings[noted:]:
            added = added + budget.Totals(1, 0, len(warning.encode("utf-8")) + 3)
        if not added.within():
            left = [c for c, _ in asked[index:]]
            unread = [c for c in unread if c not in left] + left
            warnings.append(
                f"The output is full: {len(left)} commands were not returned; run the step "
                "again with unread_command_ids"
            )
            break
        totals, noted = added, len(warnings)
        out.append(row)
    if not any(r["status"] == "read" for r in out):
        if first_failure is not None:
            raise first_failure
        if not out:
            raise budget.too_large()
    return _results(out, asked, unread, warnings)


#: Room kept for the warning that says the output is full.
_FULL_RESERVE = "x" * 200


def _results(
    out: list[dict[str, Any]],
    asked: list[tuple[str, str]],
    unread: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    read = [r for r in out if r["status"] == "read"]
    return {
        "commands": out,
        "all_completed": len(read) == len(asked) and all(r["completed"] for r in read),
        "failed_count": sum(r["failed"] for r in read),
        "unread_command_ids": unread,
        "warnings": warnings,
    }


def _command_result(
    context: Context, command_id: str, kind: str, warnings: list[str]
) -> dict[str, Any]:
    """One command's summary and, when offers failed, its report of failed offers."""
    summary_step, report_step = _REPORT_STEPS[kind]
    summary = _read(context, summary_step, {"command_id": command_id})
    if not isinstance(summary, Mapping) or not isinstance(summary.get("taskCount"), Mapping):
        raise _bad(f"the {kind} command {command_id}", "has no taskCount")
    counts = summary["taskCount"]
    total, success, failed = (_count(counts.get(k, 0)) for k in ("total", "success", "failed"))
    row: dict[str, Any] = {
        "command_id": command_id,
        "kind": kind,
        "status": "read",
        "reason": None,
        "completed": summary.get("completedAt") not in (None, ""),
        "total": total,
        "success": success,
        "failed": failed,
        "waiting": max(0, total - success - failed),
        "failed_offers": [],
    }
    if failed:
        report = _read(context, report_step, {"command_id": command_id, "limit": 1000, "offset": 0})
        tasks = report.get("tasks") if isinstance(report, Mapping) else None
        if not isinstance(tasks, list):
            raise _bad(f"the {kind} command report {command_id}", "has no tasks list")
        for task in tasks:
            if not isinstance(task, Mapping) or task.get("status") != "FAIL":
                continue
            offer = task.get("offer")
            offer_id = offer.get("id") if isinstance(offer, Mapping) else None
            row["failed_offers"].append(
                {
                    "offer_id": offer_id if isinstance(offer_id, str) else None,
                    "message": errors.safe_text(task.get("message"), 300),
                }
            )
            if len(row["failed_offers"]) >= MAX_FAILED_SHOWN:
                break
        if len(row["failed_offers"]) < failed:
            warnings.append(
                f"Command {command_id}: Allegro counts {failed} failed offers, the report "
                f"lists {len(row['failed_offers'])}"
            )
    return row


COMPOSITES: dict[str, Callable[[Context], dict[str, Any]]] = {
    "test_connection": test_connection,
    "list_all_my_offers": list_all_my_offers,
    "update_stock_and_prices": update_stock_and_prices,
    "get_stock_and_price_update_results": get_stock_and_price_update_results,
    "list_orders": list_orders,
    "list_order_events": list_order_events,
    "verify_identity": verify_identity,
}
COMPOSITE_WRITES: frozenset[str] = frozenset({"update_stock_and_prices"})
VALIDATORS: dict[str, Callable[[Mapping[str, Any]], None]] = {
    "update_stock_and_prices": validate_update,
}

__all__ = ["COMPOSITES", "COMPOSITE_WRITES", "VALIDATORS", "plan_update"]
