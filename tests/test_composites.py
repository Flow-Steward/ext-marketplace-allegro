"""The everyday steps: connection test, all offers, batch stock and price update and its
results, orders and order events."""

from __future__ import annotations

from typing import Any

import pytest
from support import Artifacts, FakeAllegro, errors_holder, ok, raw, refuse, run, text_of

from runtime import composites

# -- test connection / verify identity -------------------------------------------------------


def test_test_connection_reads_the_account() -> None:
    fake = FakeAllegro(ok({"id": "42", "login": "shop", "email": "a@b.pl"}))
    result = run("test_connection", {}, fake)
    assert result["result"] == {
        "connected": True,
        "user_id": "42",
        "login": "shop",
        "environment": "production",
        "warnings": [],
    }
    result = run(
        "test_connection", {}, FakeAllegro(ok({"id": "1"})), connection_type="allegro_sandbox"
    )
    assert result["result"]["environment"] == "sandbox"


def test_test_connection_without_a_user_id_is_an_invalid_answer() -> None:
    result = run("test_connection", {}, FakeAllegro(ok({"login": "x"})))
    assert result["error_code"] == "invalid_response"


def test_verify_identity_returns_only_the_closed_post_connect_fields() -> None:
    result = run(
        "verify_identity", {}, FakeAllegro(ok({"id": "42", "login": "shop", "email": "e"}))
    )
    assert result["ok"] is True
    assert result["result"] == {"subject": "42", "verified_display_name": "shop", "health": "ok"}
    # Core's closed result contract (core/application/extensions/oauth_post_connect.py).
    assert set(result["result"]) <= {
        "subject",
        "verified_display_name",
        "verified_email",
        "safe_metadata",
        "health",
        "error_code",
    }


# -- get all my offers ---------------------------------------------------------------------


def offers(start: int, count: int) -> list[dict[str, Any]]:
    return [{"id": str(i), "name": f"Offer {i}"} for i in range(start, start + count)]


def test_all_pages_are_read_until_the_total() -> None:
    fake = FakeAllegro(
        ok({"offers": offers(0, 1000), "count": 1000, "totalCount": 1500}),
        ok({"offers": offers(1000, 500), "count": 500, "totalCount": 1500}),
    )
    result = run(
        "list_all_my_offers", {"publication_status": ["ACTIVE"], "external_ids": ["e1"]}, fake
    )
    assert result["ok"] is True
    out = result["result"]
    assert out["count"] == 1500 and out["complete"] is True and out["next_offset"] is None
    assert out["total_count"] == 1500
    assert [dict(c.query)["offset"] for c in fake.calls] == ["0", "1000"]
    assert ("publication.status", "ACTIVE") in fake.calls[0].query
    assert ("external.id", "e1") in fake.calls[0].query
    assert all(dict(c.query)["limit"] == "1000" for c in fake.calls)


def test_a_full_output_stops_with_where_to_continue() -> None:
    page = [{"id": str(i), "name": "x" * 2000} for i in range(1000)]
    page2 = [{"id": str(1000 + i), "name": "x" * 2000} for i in range(1000)]
    fake = FakeAllegro(
        ok({"offers": page, "totalCount": 5000}), ok({"offers": page2, "totalCount": 5000})
    )
    result = run("list_all_my_offers", {}, fake)
    out = result["result"]
    assert result["ok"] is True
    assert out["complete"] is False and out["next_offset"] == 1000 and out["count"] == 1000
    assert "next_offset 1000" in out["warnings"][-1]


def test_the_list_can_be_returned_as_a_file() -> None:
    artifacts = Artifacts()
    fake = FakeAllegro(ok({"offers": offers(0, 3), "totalCount": 3}))
    result = run("list_all_my_offers", {"output": "file"}, fake, artifacts=artifacts)
    out = result["result"]
    assert out["offers"] == [] and out["file"] == "artifact-1" and out["count"] == 3
    assert artifacts.saved[0]["content_type"] == "application/json"
    assert b'"Offer 2"' in artifacts.saved[0]["data"]


def test_a_record_seen_twice_is_returned_once_with_a_warning() -> None:
    fake = FakeAllegro(
        ok({"offers": offers(0, 2), "totalCount": 3}),
        ok({"offers": offers(1, 2), "totalCount": 3}),
    )
    result = run("list_all_my_offers", {"max_offers": 2}, fake)
    assert result["result"]["count"] == 2
    fake = FakeAllegro(
        ok({"offers": offers(0, 1000), "totalCount": 1002}),
        ok({"offers": offers(999, 3), "totalCount": 1002}),
    )
    result = run("list_all_my_offers", {}, fake)
    assert result["result"]["count"] == 1002
    assert "appeared twice" in result["result"]["warnings"][0]


def test_a_page_without_offers_or_ids_is_an_invalid_answer() -> None:
    assert (
        run("list_all_my_offers", {}, FakeAllegro(ok({"items": []})))["error_code"]
        == "invalid_response"
    )
    assert (
        run("list_all_my_offers", {}, FakeAllegro(ok({"offers": [{"name": "x"}]})))["error_code"]
        == "invalid_response"
    )


def test_running_out_of_time_between_pages_returns_what_was_read() -> None:
    now = [0.0]

    def answer(call: Any) -> Any:
        now[0] += 300
        return ok({"offers": offers(int(dict(call.query)["offset"]), 1000), "totalCount": 9000})

    fake = FakeAllegro(answer, answer, answer)
    result = run("list_all_my_offers", {}, fake, clock=lambda: now[0])
    out = result["result"]
    assert result["ok"] is True and out["complete"] is False
    assert out["next_offset"] == out["count"]
    assert "time ran out" in out["warnings"][-1]


# -- orders and events ---------------------------------------------------------------------


def test_orders_are_read_with_their_filters() -> None:
    forms = [{"id": f"o{i}"} for i in range(3)]
    fake = FakeAllegro(ok({"checkoutForms": forms, "count": 3, "totalCount": 3}))
    result = run(
        "list_orders",
        {
            "status": "READY_FOR_PROCESSING",
            "updated_from": "2026-11-01T00:00:00Z",
            "sort": "updatedAt",
        },
        fake,
    )
    assert result["result"]["orders"] == forms and result["result"]["complete"] is True
    query = dict(fake.calls[0].query)
    assert query["status"] == "READY_FOR_PROCESSING"
    assert query["updatedAt.gte"] == "2026-11-01T00:00:00Z"
    assert query["sort"] == "updatedAt" and query["limit"] == "100"


@pytest.mark.parametrize(
    "given", [{"status": "PAID"}, {"updated_from": "2026-11-01"}, {"sort": "price"}]
)
def test_bad_order_filters_are_refused(given: dict[str, Any]) -> None:
    fake = FakeAllegro()
    assert run("list_orders", given, fake)["error_code"] == "invalid_payload" and fake.calls == []


def test_order_events_continue_after_the_last_event() -> None:
    page = [{"id": str(i), "type": "BOUGHT"} for i in range(1000)]
    fake = FakeAllegro(ok({"events": page}), ok({"events": [{"id": "1000", "type": "BOUGHT"}]}))
    result = run("list_order_events", {"after_event_id": "start", "types": ["BOUGHT"]}, fake)
    out = result["result"]
    assert out["count"] == 1001 and out["last_event_id"] == "1000" and out["complete"] is True
    assert dict(fake.calls[0].query)["from"] == "start"
    assert dict(fake.calls[1].query)["from"] == "999"
    assert ("type", "BOUGHT") in fake.calls[0].query


def test_no_new_events_keeps_the_given_last_event() -> None:
    result = run("list_order_events", {"after_event_id": "77"}, FakeAllegro(ok({"events": []})))
    assert result["result"]["last_event_id"] == "77" and result["result"]["count"] == 0


def test_an_event_page_that_repeats_the_start_is_refused() -> None:
    result = run(
        "list_order_events", {"after_event_id": "5"}, FakeAllegro(ok({"events": [{"id": "5"}]}))
    )
    assert result["error_code"] == "invalid_response"


# -- update stock and prices ---------------------------------------------------------------

ROWS = [
    {"offer_id": "11", "quantity": 5, "price": "49.90"},
    {"offer_id": "12", "quantity": 5},
    {"offer_id": "13", "price": "49.9", "currency": "PLN"},
    {"offer_id": "14", "price": "10.00", "currency": "CZK"},
]


def test_rows_are_grouped_into_commands_with_ids_from_their_content() -> None:
    commands = composites.plan_update({"rows": ROWS, "batch_key": "run-1"})
    shape = [(c.kind, c.value, c.currency, c.offers) for c in commands]
    assert shape == [
        ("quantity", "5", None, ["11", "12"]),
        ("price", "10.00", "CZK", ["14"]),
        ("price", "49.90", "PLN", ["11", "13"]),
    ]
    again = composites.plan_update({"rows": list(reversed(ROWS)), "batch_key": "run-1"})
    assert [c.command_id for c in again] == [c.command_id for c in commands]
    other = composites.plan_update({"rows": ROWS, "batch_key": "run-2"})
    assert not {c.command_id for c in other} & {c.command_id for c in commands}


def test_a_group_larger_than_1000_offers_is_split() -> None:
    rows = [{"offer_id": str(i), "quantity": 1} for i in range(2500)]
    commands = composites.plan_update({"rows": rows, "batch_key": "k"})
    assert [len(c.offers) for c in commands] == [1000, 1000, 500]


@pytest.mark.parametrize(
    "rows",
    [
        [{"offer_id": "1", "quantity": 0}],
        [{"offer_id": "1", "quantity": -1}],
        [{"offer_id": "1", "price": "1.234"}],
        [{"offer_id": "1", "price": "0"}],
        [{"offer_id": "1", "price": 9.99}],
        [{"offer_id": "1", "price": "9.99", "currency": "zł"}],
        [{"offer_id": "1", "currency": "PLN", "quantity": 1}],
        [{"offer_id": "1"}],
        [{"offer_id": "1", "quantity": 1}, {"offer_id": "1", "price": "1"}],
        [{"offer_id": "1/2", "quantity": 1}],
        [{"offer_id": "1", "quantity": 1, "stock": 2}],
        [],
    ],
)
def test_bad_rows_are_refused_and_nothing_is_sent(rows: list[dict[str, Any]]) -> None:
    fake = FakeAllegro()
    result = run("update_stock_and_prices", {"rows": rows, "batch_key": "k"}, fake)
    assert result["error_code"] == "invalid_payload"
    assert result["definitely_no_external_effect"] is True and fake.calls == []


def test_too_many_distinct_values_are_refused() -> None:
    rows = [{"offer_id": str(i), "quantity": i + 1} for i in range(401)]
    result = run("update_stock_and_prices", {"rows": rows, "batch_key": "k"}, FakeAllegro())
    assert result["error_code"] == "invalid_payload" and "401 commands" in result["error"]


def echo(call: Any) -> Any:
    return ok({"id": call.path.rsplit("/", 1)[1], "taskCount": {"total": 1}}, 201)


def test_the_commands_are_sent_once_each_with_the_documented_bodies() -> None:
    fake = FakeAllegro(echo, echo, echo)
    result = run("update_stock_and_prices", {"rows": ROWS, "batch_key": "run-1"}, fake)
    assert result["ok"] is True, result
    assert result["external_effect_status"] == "succeeded"
    out = result["result"]
    assert [c["status"] for c in out["commands"]] == ["registered"] * 3
    assert out["offers_count"] == 5
    planned = composites.plan_update({"rows": ROWS, "batch_key": "run-1"})
    assert [c.path for c in fake.calls] == [
        f"/sale/offer-quantity-change-commands/{planned[0].command_id}",
        f"/sale/offer-price-change-commands/{planned[1].command_id}",
        f"/sale/offer-price-change-commands/{planned[2].command_id}",
    ]
    assert all(c.verb == "PUT" for c in fake.calls)
    assert fake.calls[0].raw_body == (
        b'{"modification":{"changeType":"FIXED","value":5},"offerCriteria":'
        b'[{"type":"CONTAINS_OFFERS","offers":[{"id":"11"},{"id":"12"}]}]}'
    )
    assert fake.calls[2].body["modification"] == {
        "type": "FIXED_PRICE",
        "price": {"amount": "49.90", "currency": "PLN"},
    }


def test_registered_commands_are_accepted_not_applied() -> None:
    result = run(
        "update_stock_and_prices",
        {"rows": ROWS, "batch_key": "run-1"},
        FakeAllegro(echo, echo, echo),
    )
    out = result["result"]
    assert [c["status"] for c in out["commands"]] == ["registered"] * 3
    assert out["processing"] == "accepted" and out["applied"] is False
    assert out["check"] == {
        "operation_id": "get_stock_and_price_update_results",
        "location": None,
    }


def test_an_undocumented_success_status_is_not_a_registered_command() -> None:
    # The batch price change documents 201; a 204 without a body is not taken as registered.
    rows = [{"offer_id": "13", "price": "49.90"}]
    fake = FakeAllegro(raw(204, b""))
    result = run("update_stock_and_prices", {"rows": rows, "batch_key": "k"}, fake)
    assert result["ok"] is False
    assert result["error_code"] == "invalid_response"
    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False
    assert "HTTP 204" in result["error"] and "201" in result["error"]
    assert "0 of 1 commands are registered" in result["error"]
    assert "may or may not be registered" in result["error"]
    assert len(fake.calls) == 1


def test_a_command_already_registered_is_not_applied_again() -> None:
    fake = FakeAllegro(refuse(409), echo, refuse(409))
    result = run("update_stock_and_prices", {"rows": ROWS, "batch_key": "run-1"}, fake)
    assert result["ok"] is True
    assert [c["status"] for c in result["result"]["commands"]] == [
        "already_registered",
        "registered",
        "already_registered",
    ]
    assert len(fake.calls) == 3
    assert "already registered" in result["result"]["warnings"][-1]


def test_a_refusal_of_the_first_command_changes_nothing_and_stops() -> None:
    fake = FakeAllegro(refuse(422, errors_holder("QUANTITY", "Too much")))
    result = run("update_stock_and_prices", {"rows": ROWS, "batch_key": "k"}, fake)
    assert result["definitely_no_external_effect"] is True
    assert len(fake.calls) == 1


def test_an_unclear_first_command_is_unknown_and_not_retried() -> None:
    fake = FakeAllegro(TimeoutError())
    result = run("update_stock_and_prices", {"rows": ROWS, "batch_key": "k"}, fake)
    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False
    assert len(fake.calls) == 1


def test_a_failure_part_way_says_what_was_registered_and_sends_no_more() -> None:
    fake = FakeAllegro(echo, refuse(500, errors_holder()))
    result = run("update_stock_and_prices", {"rows": ROWS, "batch_key": "k"}, fake)
    planned = composites.plan_update({"rows": ROWS, "batch_key": "k"})
    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False
    assert "1 of 3 commands are registered" in result["error"]
    assert planned[0].command_id in result["error"]
    assert "may or may not be registered" in result["error"]
    assert "1 were not sent" in result["error"]
    assert len(fake.calls) == 2


def test_a_clear_refusal_part_way_still_reports_the_registered_commands() -> None:
    fake = FakeAllegro(echo, refuse(422))
    result = run("update_stock_and_prices", {"rows": ROWS, "batch_key": "k"}, fake)
    assert result["external_effect_status"] == "timeout_unknown"
    assert "was not registered" in result["error"]


def test_a_test_run_plans_the_same_commands_and_sends_nothing() -> None:
    fake = FakeAllegro()
    result = run(
        "update_stock_and_prices", {"rows": ROWS, "batch_key": "run-1"}, fake, test_mode=True
    )
    assert fake.calls == []
    assert result["definitely_no_external_effect"] is True
    planned = composites.plan_update({"rows": ROWS, "batch_key": "run-1"})
    assert [c["command_id"] for c in result["result"]["commands"]] == [
        c.command_id for c in planned
    ]
    assert {c["status"] for c in result["result"]["commands"]} == {"not_sent"}


# -- update results ------------------------------------------------------------------------


def test_results_read_the_summary_and_the_failed_offers() -> None:
    fake = FakeAllegro(
        routes={
            "GET /sale/offer-quantity-change-commands/q1": ok(
                {
                    "id": "q1",
                    "completedAt": "2026-11-18T10:00:00Z",
                    "taskCount": {"total": 3, "success": 2, "failed": 1},
                }
            ),
            "GET /sale/offer-quantity-change-commands/q1/tasks": ok(
                {
                    "tasks": [
                        {"offer": {"id": "11"}, "status": "SUCCESS"},
                        {
                            "offer": {"id": "12"},
                            "status": "FAIL",
                            "message": "The offer does not exist.",
                        },
                    ]
                }
            ),
            "GET /sale/offer-price-change-commands/p1": ok(
                {
                    "id": "p1",
                    "completedAt": None,
                    "taskCount": {"total": 2, "success": 1, "failed": 0},
                }
            ),
        }
    )
    result = run(
        "get_stock_and_price_update_results",
        {
            "commands": [
                {"command_id": "q1", "kind": "quantity"},
                {"command_id": "p1", "kind": "price", "status": "registered"},
            ]
        },
        fake,
    )
    out = result["result"]
    assert out["all_completed"] is False and out["failed_count"] == 1
    assert out["commands"][0]["failed_offers"] == [
        {"offer_id": "12", "message": "The offer does not exist."}
    ]
    assert out["commands"][1]["waiting"] == 1 and out["commands"][1]["completed"] is False
    assert dict(fake.calls[1].query)["limit"] == "1000"


def test_results_refuse_an_unknown_kind() -> None:
    result = run(
        "get_stock_and_price_update_results",
        {"commands": [{"command_id": "x", "kind": "stock"}]},
        FakeAllegro(),
    )
    assert result["error_code"] == "invalid_payload"


def test_no_everyday_step_leaks_the_token() -> None:
    from support import TOKEN

    result = run(
        "get_stock_and_price_update_results",
        {"commands": [{"command_id": "x", "kind": "price"}]},
        FakeAllegro(refuse(404, {"errors": [{"message": TOKEN}]})),
    )
    assert TOKEN not in text_of(result)
