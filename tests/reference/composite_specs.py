"""The everyday steps: their inputs, outputs and descriptions.

They are built on top of the documented operations (runtime/composites.py) and come
first in the step list. Contract files are generated from this module by
build_contracts.py.
"""

from __future__ import annotations

from typing import Any

PUBLICATION_STATUSES = ["INACTIVE", "ACTIVATING", "ACTIVE", "ENDED"]
ORDER_STATUSES = ["BOUGHT", "FILLED_IN", "READY_FOR_PROCESSING", "CANCELLED"]
FULFILLMENT_STATUSES = [
    "NEW",
    "PROCESSING",
    "READY_FOR_SHIPMENT",
    "READY_FOR_PICKUP",
    "SENT",
    "PICKED_UP",
    "CANCELLED",
    "SUSPENDED",
    "RETURNED",
]
ORDER_SORTS = ["lineItems.boughtAt", "-lineItems.boughtAt", "updatedAt", "-updatedAt"]
ORDER_EVENT_TYPES = [
    "BOUGHT",
    "FILLED_IN",
    "READY_FOR_PROCESSING",
    "BUYER_CANCELLED",
    "FULFILLMENT_STATUS_CHANGED",
    "BUYER_MODIFIED",
    "AUTO_CANCELLED",
]
DATE_TIME = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$"

WARNINGS = {
    "name": "warnings",
    "type": "array",
    "items": {"type": "string"},
    "description": "What the step could not do fully or what to check.",
}

#: The outputs every write returns about what Allegro's answer confirms.
PROCESSING_OUTPUTS: list[dict[str, Any]] = [
    {
        "name": "processing",
        "type": ["string", "null"],
        "enum": ["completed", "accepted", None],
        "description": (
            "completed: Allegro answered that the change is made. accepted: Allegro accepted "
            "the request (HTTP 202) or registered the command, and applies it afterwards; "
            "check the outcome with the step named in check. None in a test run and when "
            "nothing was sent."
        ),
    },
    {
        "name": "applied",
        "type": ["boolean", "null"],
        "description": (
            "True with processing completed. False with processing accepted (the outcome is "
            "read with the check step), in a test run and when nothing was sent. Empty when "
            "only part of a batch was registered."
        ),
    },
    {
        "name": "check",
        "type": ["object", "null"],
        "additionalProperties": True,
        "description": (
            "With processing accepted: operation_id, the step that reads the outcome (none "
            "when Allegro documents no such step), and location, the address Allegro returned "
            "for it (none when it sent none)."
        ),
    },
    {
        "name": "retry_after_seconds",
        "type": ["integer", "null"],
        "description": (
            "With HTTP 202: the seconds Allegro asks to wait (Retry-After) before checking the "
            "outcome. Empty otherwise."
        ),
    },
]


def _text(name: str, title: str, description: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "title": title, "description": description, "type": "string", **extra}


COMPOSITES: list[dict[str, Any]] = [
    {
        "operation_id": "test_connection",
        "title": "Test connection",
        "kind": "read",
        "calls": ["GET /me"],
        "scopes": ["allegro:api:profile:read"],
        "description": (
            "Checks that Allegro accepts the connection's authorization by reading the "
            "account it belongs to (GET /me). Returns the user id and login. Needs the "
            "Allegro permission allegro:api:profile:read."
        ),
        "inputs": [],
        "outputs": [
            {
                "name": "connected",
                "type": "boolean",
                "description": "True when Allegro accepted the authorization.",
            },
            {
                "name": "user_id",
                "type": ["string", "null"],
                "description": "The Allegro user id of the connected account.",
            },
            {
                "name": "login",
                "type": ["string", "null"],
                "description": "The Allegro login of the connected account.",
            },
            {
                "name": "environment",
                "type": "string",
                "description": "production or sandbox, from the connection type.",
            },
            WARNINGS,
        ],
    },
    {
        "operation_id": "list_all_my_offers",
        "title": "Get all my offers",
        "kind": "read",
        "calls": ["GET /sale/offers"],
        "scopes": ["allegro:api:sale:offers:read"],
        "description": (
            "Reads the seller's offers page by page (GET /sale/offers, up to 1000 per page) "
            "until all are read or the step output (or, with output file, a JSON workflow file) is full, filtered by name, offer IDs, "
            "external IDs, publication status or category. When the output is full or time "
            "runs out the step returns what it read with complete false and next_offset to "
            "start the next step from. Needs allegro:api:sale:offers:read."
        ),
        "inputs": [
            _text("name", "Title contains", "Text to search in the offer title.", maxLength=200),
            {
                "name": "offer_ids",
                "title": "Offer IDs",
                "type": "array",
                "items": {"type": "string", "maxLength": 30},
                "maxItems": 1000,
                "description": "Only these offers.",
            },
            {
                "name": "external_ids",
                "title": "External IDs",
                "type": "array",
                "items": {"type": "string", "maxLength": 100},
                "maxItems": 1000,
                "description": "Only offers with these IDs from your own system (external.id).",
            },
            {
                "name": "publication_status",
                "title": "Publication status",
                "type": "array",
                "items": {"type": "string", "enum": PUBLICATION_STATUSES},
                "maxItems": 4,
                "description": "Only offers with any of these statuses. All statuses when empty.",
            },
            _text("category_id", "Category ID", "Only offers in this category.", maxLength=30),
            {
                "name": "start_offset",
                "title": "Start at",
                "type": "integer",
                "minimum": 0,
                "maximum": 9999999,
                "description": "The offset to start at: 0, or next_offset of an earlier run.",
            },
            {
                "name": "max_offers",
                "title": "At most",
                "type": "integer",
                "minimum": 1,
                "maximum": 100000,
                "description": "The most offers to read in this step. Default 100000.",
            },
            {
                "name": "output",
                "title": "Return as",
                "type": "string",
                "enum": ["step", "file"],
                "description": "step: in the step output (default). file: one JSON workflow file of up to 40 MB, for long lists.",
            },
        ],
        "outputs": [
            {
                "name": "offers",
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "description": "The offers, as Allegro lists them (empty with output file).",
            },
            {
                "name": "file",
                "type": ["string", "null"],
                "value_type": "artifact_handle",
                "description": "With output file: the list as a JSON workflow file.",
            },
            {"name": "count", "type": "integer", "description": "How many offers were read."},
            {
                "name": "total_count",
                "type": ["integer", "null"],
                "description": "How many offers match, as Allegro counted them on the last page.",
            },
            {
                "name": "complete",
                "type": "boolean",
                "description": "True when every matching offer from start_offset on was read.",
            },
            {
                "name": "next_offset",
                "type": ["integer", "null"],
                "description": "Where to continue when complete is false.",
            },
            WARNINGS,
        ],
    },
    {
        "operation_id": "update_stock_and_prices",
        "title": "Update stock and prices",
        "kind": "write",
        "calls": [
            "PUT /sale/offer-quantity-change-commands/{commandId}",
            "PUT /sale/offer-price-change-commands/{commandId}",
        ],
        "scopes": ["allegro:api:sale:offers:write"],
        "description": (
            "Sets the stock and the Buy Now price of many offers with Allegro's batch "
            "commands: offers with the same new stock or the same new price go into one "
            "command of up to 1000 offers. Each command ID is derived from batch_key and the "
            "command's content, so running the step again with the same rows and batch key "
            "never registers a change twice: Allegro answers that the command already exists. "
            "The step confirms that Allegro registered the commands (processing accepted, "
            "applied false); Allegro applies them afterwards, and Get stock and price update "
            "results reads the outcome per offer. When Allegro certainly refuses a command after "
            "earlier ones were registered, or time runs out before the next one is sent, the step "
            "still succeeds: each command has its status, applied is empty and a warning says "
            "what to fix before running it again. Stock must be at least 1 and a price is an exact "
            "decimal with at most two places. Needs allegro:api:sale:offers:write."
        ),
        "inputs": [
            {
                "name": "rows",
                "title": "Offers",
                "type": "array",
                "minItems": 1,
                "maxItems": 20000,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["offer_id"],
                    "properties": {
                        "offer_id": {
                            "type": "string",
                            "maxLength": 30,
                            "description": "The Allegro offer ID.",
                        },
                        "quantity": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000000,
                            "description": "The new stock.",
                        },
                        "price": {
                            "type": ["number", "string"],
                            "description": "The new Buy Now price, for example 49.99.",
                        },
                        "currency": {
                            "type": "string",
                            "pattern": "^[A-Z]{3}$",
                            "description": "The price's currency. Default PLN.",
                        },
                    },
                },
                "description": "One row per offer: offer_id and the new quantity, price or both.",
            },
            _text(
                "batch_key",
                "Batch key",
                "Your name for this batch: use a new key for every update, for example the export run ID. The same rows with a key already used are never applied again: Allegro answers that the command exists, nothing is sent again and the step reads that command's outcome.",
                minLength=1,
                maxLength=100,
            ),
        ],
        "outputs": [
            {
                "name": "commands",
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "description": "Each command: command_id, kind (quantity or price), value, currency, offers_count and status: registered, already_registered, refused (Allegro certainly refused it after earlier commands were registered) or not_sent; and outcome: for already_registered, how far Allegro got with the command registered earlier (completed, total, success, failed, waiting) or unknown when it could not be read; empty for the others.",
            },
            {
                "name": "offers_count",
                "type": "integer",
                "description": "How many offers the commands cover.",
            },
            *PROCESSING_OUTPUTS,
            WARNINGS,
            {"name": "external_effect_status", "type": "string"},
            {"name": "definitely_no_external_effect", "type": "boolean"},
        ],
    },
    {
        "operation_id": "get_stock_and_price_update_results",
        "title": "Get stock and price update results",
        "kind": "read",
        "calls": [
            "GET /sale/offer-quantity-change-commands/{commandId}",
            "GET /sale/offer-quantity-change-commands/{commandId}/tasks",
            "GET /sale/offer-price-change-commands/{commandId}",
            "GET /sale/offer-price-change-commands/{commandId}/tasks",
        ],
        "scopes": ["allegro:api:sale:offers:write"],
        "description": (
            "Reads how far Allegro got with the commands of Update stock and prices: per "
            "command the number of offers done, failed and waiting, and for failed offers the "
            "offer ID and Allegro's reason. Give the commands output of that step. Needs "
            "allegro:api:sale:offers:write (Allegro asks for it to read the reports)."
        ),
        "inputs": [
            {
                "name": "commands",
                "title": "Commands",
                "type": "array",
                "minItems": 1,
                "maxItems": 500,
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "required": ["command_id", "kind"],
                    "properties": {
                        "command_id": {"type": "string", "maxLength": 64},
                        "kind": {"type": "string", "enum": ["quantity", "price"]},
                    },
                },
                "description": "The commands output of Update stock and prices.",
            },
        ],
        "outputs": [
            {
                "name": "commands",
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "description": "Each command: command_id, kind, completed, total, success, failed, waiting and failed_offers (offer_id and message).",
            },
            {
                "name": "all_completed",
                "type": "boolean",
                "description": "True when Allegro finished every command.",
            },
            {
                "name": "failed_count",
                "type": "integer",
                "description": "How many offers failed in all.",
            },
            WARNINGS,
        ],
    },
    {
        "operation_id": "list_orders",
        "title": "Get orders",
        "kind": "read",
        "calls": ["GET /order/checkout-forms"],
        "scopes": ["allegro:api:orders:read"],
        "description": (
            "Reads the seller's orders (checkout forms) page by page (GET "
            "/order/checkout-forms, up to 100 per page) until all are read or the step output "
            "(or, with output file, a JSON workflow file) is full, filtered by status, seller status, buyer and dates. Returns complete "
            "false and next_offset when it stops early. Needs allegro:api:orders:read."
        ),
        "inputs": [
            {
                "name": "status",
                "title": "Status",
                "type": "string",
                "enum": ORDER_STATUSES,
                "description": "Only orders with this status (READY_FOR_PROCESSING: paid).",
            },
            {
                "name": "fulfillment_status",
                "title": "Seller status",
                "type": "string",
                "enum": FULFILLMENT_STATUSES,
                "description": "Only orders with this seller status.",
            },
            _text(
                "updated_from",
                "Changed from",
                "Only orders changed at or after this time, for example 2026-11-01T00:00:00Z.",
                pattern=DATE_TIME,
            ),
            _text(
                "updated_to",
                "Changed until",
                "Only orders changed at or before this time.",
                pattern=DATE_TIME,
            ),
            _text(
                "bought_from",
                "Bought from",
                "Only orders bought at or after this time.",
                pattern=DATE_TIME,
            ),
            _text(
                "bought_to",
                "Bought until",
                "Only orders bought at or before this time.",
                pattern=DATE_TIME,
            ),
            _text("buyer_login", "Buyer login", "Only orders of this buyer.", maxLength=100),
            {
                "name": "sort",
                "title": "Sort by",
                "type": "string",
                "enum": ORDER_SORTS,
                "description": "Order of the list; a leading - means newest first. Allegro's default: -lineItems.boughtAt.",
            },
            {
                "name": "start_offset",
                "title": "Start at",
                "type": "integer",
                "minimum": 0,
                "maximum": 9999999,
                "description": "The offset to start at: 0, or next_offset of an earlier run.",
            },
            {
                "name": "max_orders",
                "title": "At most",
                "type": "integer",
                "minimum": 1,
                "maximum": 20000,
                "description": "The most orders to read in this step. Default 5000.",
            },
            {
                "name": "output",
                "title": "Return as",
                "type": "string",
                "enum": ["step", "file"],
                "description": "step: in the step output (default). file: one JSON workflow file of up to 40 MB, for long lists.",
            },
        ],
        "outputs": [
            {
                "name": "orders",
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "description": "The orders, as Allegro returns them (empty with output file).",
            },
            {
                "name": "file",
                "type": ["string", "null"],
                "value_type": "artifact_handle",
                "description": "With output file: the list as a JSON workflow file.",
            },
            {"name": "count", "type": "integer", "description": "How many orders were read."},
            {
                "name": "total_count",
                "type": ["integer", "null"],
                "description": "How many orders match, as Allegro counted them on the last page.",
            },
            {
                "name": "complete",
                "type": "boolean",
                "description": "True when every matching order from start_offset on was read.",
            },
            {
                "name": "next_offset",
                "type": ["integer", "null"],
                "description": "Where to continue when complete is false.",
            },
            WARNINGS,
        ],
    },
    {
        "operation_id": "list_order_events",
        "title": "Get order events",
        "kind": "read",
        "calls": ["GET /order/events"],
        "scopes": ["allegro:api:orders:read"],
        "description": (
            "Reads order events (bought, filled in, paid, cancelled, seller status changed…) "
            "after the event ID given, page by page (GET /order/events, up to 1000 per page). "
            "Keep last_event_id and pass it as after_event_id next time to read only new "
            "events. Needs allegro:api:orders:read."
        ),
        "inputs": [
            _text(
                "after_event_id",
                "After event",
                "Read the events after this event ID (last_event_id of an earlier run). Empty: Allegro starts where its documentation for GET /order/events says.",
                maxLength=64,
            ),
            {
                "name": "types",
                "title": "Event types",
                "type": "array",
                "items": {"type": "string", "enum": ORDER_EVENT_TYPES},
                "maxItems": 7,
                "description": "Only these event types. All when empty.",
            },
            {
                "name": "max_events",
                "title": "At most",
                "type": "integer",
                "minimum": 1,
                "maximum": 50000,
                "description": "The most events to read in this step. Default 10000.",
            },
        ],
        "outputs": [
            {
                "name": "events",
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "description": "The events, oldest first.",
            },
            {"name": "count", "type": "integer", "description": "How many events were read."},
            {
                "name": "last_event_id",
                "type": ["string", "null"],
                "description": "The ID of the last event read; the after_event_id of the next run. The one given when there was nothing new.",
            },
            {
                "name": "complete",
                "type": "boolean",
                "description": "True when there were no more events to read.",
            },
            WARNINGS,
        ],
    },
]

#: The post-connect action Core runs after authorization: hidden, no parameters.
VERIFY_IDENTITY = {
    "action_id": "verify_identity",
    "title": "Verify Allegro account",
    "description": "Reads the authorized Allegro account (GET /me) so Flow Steward records which account the connection is for.",
}
