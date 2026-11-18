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

from . import budget, errors, files, generic
from .context import Context
from .errors import ExtensionError, invalid
from .params import decimal, integer, text
from .transport import MIN_CALL_WINDOW_SECONDS, DeadlineReached, dumps_json

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
    except ValueError:
        raise errors.no_effect(
            errors.INVALID_RESPONSE,
            f"Allegro answered {plan.shown} with something that is not JSON",
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


class Pages:
    """Collects the records of a paginated read into the step output or a workflow file."""

    def __init__(self, to_file: bool, limit: int) -> None:
        self.to_file = to_file
        self.limit = limit
        self.items: list[Any] = []
        self.size = 2
        self.warnings: list[str] = []
        self.complete = False

    def take(self, page: list[Any], key: str) -> bool:
        """Add a page; False (nothing added) when it does not fit any more."""
        if self.to_file:
            size = self.size + sum(len(dumps_json(item).encode("utf-8")) + 1 for item in page)
            if size > MAX_FILE_BYTES:
                return False
            self.size = size
            self.items += page
            return True
        if not budget.fits({key: self.items + page, "warnings": self.warnings}):
            return False
        self.items += page
        return True


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
    shown = f"{generic.spec(operation_id)['method']} {generic.spec(operation_id)['path']}"
    pages = Pages(to_file, most)
    offset = start
    total: int | None = None
    seen: set[str] = set()
    first = True
    stopped = ""
    while True:
        if len(pages.items) >= most:
            stopped = f"the {most} asked for were read"
            break
        if not first and not _room(context):
            stopped = "time ran out"
            break
        limit = min(page_size, most - len(pages.items))
        try:
            data = _read(context, operation_id, {**query, "limit": limit, "offset": offset})
        except DeadlineReached:
            if first:
                raise
            stopped = "time ran out"
            break
        if not isinstance(data, Mapping) or not isinstance(data.get(list_key), list):
            raise _bad(shown, f"has no {list_key} list")
        page = data[list_key]
        count_total = data.get("totalCount")
        if isinstance(count_total, (int, Decimal)) and not isinstance(count_total, bool):
            total = int(count_total)
        fresh = []
        for item in page:
            ident = item.get("id") if isinstance(item, Mapping) else None
            if not isinstance(ident, str):
                raise _bad(shown, "lists a record without an id")
            if ident in seen:
                continue
            seen.add(ident)
            fresh.append(item)
        if len(fresh) != len(page):
            pages.warnings.append(
                f"{len(page) - len(fresh)} records appeared twice because the list changed while "
                "it was read; each is returned once"
            )
        if not pages.take(fresh, out_key):
            if first:
                raise budget.too_large()
            stopped = "the output is full" if not to_file else "the file is full"
            break
        first = False
        offset += len(page)
        if len(page) < limit or (total is not None and offset >= total) or not page:
            pages.complete = True
            break
    if not pages.complete:
        pages.warnings.append(
            f"Stopped early because {stopped}; run the step again from next_offset {offset} "
            "for the rest"
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
        if name.endswith(("_from", "_to")) and not _DATE_TIME_RE.fullmatch(checked):
            raise invalid(f"{name} must be a date and time such as 2026-11-01T00:00:00Z")
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
    pages = Pages(False, most)
    last = after
    first = True
    stopped = ""
    while True:
        if len(pages.items) >= most:
            stopped = f"the {most} asked for were read"
            break
        if not first and not _room(context):
            stopped = "time ran out"
            break
        limit = min(1000, most - len(pages.items))
        query: dict[str, Any] = {"limit": limit}
        if last:
            query["from"] = last
        if types:
            query["type"] = types
        try:
            data = _read(context, "get_order_events_page", query)
        except DeadlineReached:
            if first:
                raise
            stopped = "time ran out"
            break
        if not isinstance(data, Mapping) or not isinstance(data.get("events"), list):
            raise _bad("GET /order/events", "has no events list")
        page = data["events"]
        ids = [e.get("id") if isinstance(e, Mapping) else None for e in page]
        if any(not isinstance(i, str) or not i for i in ids):
            raise _bad("GET /order/events", "lists an event without an id")
        if last is not None and last in ids:
            raise _bad("GET /order/events", f"repeats the event {last} it was asked to read after")
        if not pages.take(page, "events"):
            if first:
                raise budget.too_large()
            stopped = "the output is full"
            break
        first = False
        if page:
            last = ids[-1]
        if len(page) < limit:
            pages.complete = True
            break
    if not pages.complete:
        pages.warnings.append(
            f"Stopped early because {stopped}; run the step again with after_event_id "
            f"{last} for the rest"
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
        }


def _price(value: Any, what: str) -> str:
    number = decimal(value, what)
    if number <= 0 or number >= Decimal("1000000000"):
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
            raise _midway(
                done, commands, command, "the step ran out of time", unsure=False
            ) from None
        except ExtensionError as exc:
            if exc.http_status == 409 and exc.definitely_no_external_effect:
                done.append(command.summary("already_registered"))
                continue
            raise _midway(
                done,
                commands,
                command,
                exc.message,
                unsure=exc.definitely_no_external_effect is False,
                base=exc,
            ) from None
        try:
            echoed = context.transport.json(answer)
        except ValueError:
            echoed = None
        if not isinstance(echoed, Mapping) or echoed.get("id") != command.command_id:
            warnings.append(
                f"Allegro registered command {command.command_id} (HTTP {answer.status}) but its "
                "answer does not repeat the command ID; check it with Get stock and price "
                "update results"
            )
        done.append(command.summary("registered"))
    again = sum(1 for c in done if c["status"] == "already_registered")
    if again:
        warnings.append(
            f"{again} of {len(done)} commands were already registered by an earlier run with the "
            "same batch key and rows; they were not applied again"
        )
    return {
        "commands": done,
        "offers_count": sum(c["offers_count"] for c in done),
        "warnings": warnings,
    }


def _midway(
    done: list[dict[str, Any]],
    commands: list[Command],
    failed: Command,
    reason: str,
    *,
    unsure: bool,
    base: ExtensionError | None = None,
) -> ExtensionError:
    """A failure part way: what was registered, what failed, what was not sent."""
    registered = [c["command_id"] for c in done]
    left = len(commands) - len(done) - 1
    if not done and not unsure:
        if base is not None:
            return base
        return errors.no_effect(
            errors.TIMEOUT, "The step ran out of time before Allegro was called"
        )
    message = (
        f"{len(done)} of {len(commands)} commands are registered"
        + (
            f" ({', '.join(registered[:20])}{'…' if len(registered) > 20 else ''})"
            if registered
            else ""
        )
        + f"; command {failed.command_id} ({failed.kind} {failed.value}) "
        + ("may or may not be registered" if unsure else "was not registered")
        + f": {reason}; {left} were not sent. Run the step again with the same rows and batch key: "
        "registered commands are not applied twice"
    )
    return ExtensionError(
        base.code
        if base is not None and base.code != errors.INVALID_PAYLOAD
        else errors.TIMEOUT_UNKNOWN,
        message,
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
    for done, (command_id, kind) in enumerate(asked):
        if done and not _room(context):
            warnings.append(
                f"Time ran out: {len(asked) - done} commands were not read; run the step again for them"
            )
            break
        summary_step, report_step = _REPORT_STEPS[kind]
        summary = _read(context, summary_step, {"command_id": command_id})
        if not isinstance(summary, Mapping) or not isinstance(summary.get("taskCount"), Mapping):
            raise _bad(f"the {kind} command {command_id}", "has no taskCount")
        counts = summary["taskCount"]
        total, success, failed = (_count(counts.get(k, 0)) for k in ("total", "success", "failed"))
        row: dict[str, Any] = {
            "command_id": command_id,
            "kind": kind,
            "completed": summary.get("completedAt") not in (None, ""),
            "total": total,
            "success": success,
            "failed": failed,
            "waiting": max(0, total - success - failed),
            "failed_offers": [],
        }
        if failed:
            report = _read(
                context, report_step, {"command_id": command_id, "limit": 1000, "offset": 0}
            )
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
        out.append(row)
    result = {
        "commands": out,
        "all_completed": len(out) == len(asked) and all(r["completed"] for r in out),
        "failed_count": sum(r["failed"] for r in out),
        "warnings": warnings,
    }
    budget.ensure_fits(result)
    return result


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
