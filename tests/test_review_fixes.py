"""Review fixes of 0.1.1: nesting depth, the output budget and its last guard, a write
that sent nothing, headers without the token, and a batch that is only partly
registered."""

from __future__ import annotations

import importlib
import io
import json
import sys
from decimal import Decimal
from typing import Any

import pytest
from support import (
    PUBLIC,
    TOKEN,
    FakeAllegro,
    errors_holder,
    ok,
    payload,
    raw,
    refuse,
    run,
    text_of,
)

import main
from runtime import budget, composites


def nesting() -> Any:
    # Imported per test, so each test fails on its own where the module is missing.
    return importlib.import_module("runtime.nesting")


def nested(levels: int, inner: str = "1") -> str:
    return "[" * levels + inner + "]" * levels


def invoke(raw_request: bytes, monkeypatch: pytest.MonkeyPatch) -> tuple[int, dict[str, Any]]:
    """main.main() in this process, stdin and stdout replaced."""
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw_request)))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out))
    code = main.main()
    return code, json.loads(out.getvalue())


def request_with_body(levels: int) -> bytes:
    # Depth: request {, action {, input { and then the body's own levels.
    text = json.dumps(payload("edit_offer", {"offer_id": "1"}))
    return text.replace('"offer_id": "1"', '"offer_id": "1", "body": ' + nested(levels)).encode()


# -- O1: nesting depth ---------------------------------------------------------------------


def test_depth_is_counted_outside_strings() -> None:
    assert nesting().depth(b"") == 0
    assert nesting().depth(b'{"a": [1, {"b": []}]}') == 4
    assert nesting().depth(b'["[[[[", "\\"[[[", {"k\\\\": "]]]]"}]') == 2
    # An unterminated string runs to the end, as for the JSON reader.
    assert nesting().depth(b'["abc [[[[[[') == 1


@pytest.mark.parametrize("levels", [30, 100_000])
def test_a_request_nested_too_deeply_is_refused_before_it_is_read(
    levels: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, response = invoke(request_with_body(levels), monkeypatch)
    assert code == 2
    assert response["error_code"] == "invalid_payload"
    assert "nested deeper than 32" in response["error"]
    assert response["definitely_no_external_effect"] is True
    assert response["external_effect_status"] == "failed"


def test_a_request_at_the_depth_limit_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    _, response = invoke(request_with_body(29), monkeypatch)
    assert "nested deeper" not in str(response.get("error"))
    # Brackets inside a string do not count.
    text = json.dumps(payload("get_my_account", {"x": "[" * 1000}))
    _, response = invoke(text.encode(), monkeypatch)
    assert "nested deeper" not in str(response.get("error"))


def test_a_recursion_error_while_reading_the_request_is_the_same_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nesting(), "too_deep", lambda raw, limit: False)
    code, response = invoke(request_with_body(200_000), monkeypatch)
    assert code == 2
    assert response["error_code"] == "invalid_payload"
    assert "nested deeper than 32" in response["error"]
    assert response["definitely_no_external_effect"] is True


@pytest.mark.parametrize("levels", [49, 100_000])
def test_a_confirmed_write_with_a_too_deep_answer_keeps_its_facts(levels: int) -> None:
    location = "https://api.allegro.pl/sale/product-offers/1"
    body = ('{"id": "1", "deep": ' + nested(levels - 1) + "}").encode()
    fake = FakeAllegro(raw(200, body, Location=location))
    result = run("edit_offer", {"offer_id": "1", "body": {"a": 1}}, fake)
    assert result["ok"] is True, result
    assert result["external_effect_status"] == "succeeded"
    assert result["definitely_no_external_effect"] is False
    out = result["result"]
    assert out["data"] is None
    assert out["status"] == 200 and out["location"] == location
    assert out["processing"] == "completed"
    assert "nested deeper than 48" in out["warnings"][0]


def test_an_answer_at_the_depth_limit_is_read() -> None:
    body = ('{"id": "1", "deep": ' + nested(47) + "}").encode()
    result = run("edit_offer", {"offer_id": "1", "body": {"a": 1}}, FakeAllegro(raw(200, body)))
    assert result["result"]["data"]["id"] == "1"


@pytest.mark.parametrize("levels", [49, 100_000])
def test_a_read_with_a_too_deep_answer_is_an_invalid_answer(levels: int) -> None:
    fake = FakeAllegro(raw(200, nested(levels).encode()))
    result = run("get_my_account", {}, fake)
    assert result["error_code"] == "invalid_response"
    assert "nested deeper than 48" in result["error"]
    assert result["definitely_no_external_effect"] is True
    assert result["external_effect_status"] == "failed"
    result = run(
        "get_stock_and_price_update_results",
        {"commands": [{"command_id": "c", "kind": "price"}]},
        FakeAllegro(raw(200, nested(levels).encode())),
    )
    assert result["error_code"] == "invalid_response"
    assert result["definitely_no_external_effect"] is True


def test_a_refusal_with_a_too_deep_body_is_still_a_refusal() -> None:
    result = run("get_my_account", {}, FakeAllegro(refuse(400, nested(100_000).encode())))
    assert result["error_code"] == "upstream_validation_failed"
    assert result["definitely_no_external_effect"] is True


def test_a_registered_command_with_a_too_deep_answer_is_registered_without_its_id() -> None:
    def deep_echo(call: Any) -> Any:
        command_id = call.path.rsplit("/", 1)[1]
        return raw(201, ('{"id": "' + command_id + '", "x": ' + nested(60) + "}").encode())

    rows = [{"offer_id": "11", "quantity": 5}]
    result = run(
        "update_stock_and_prices", {"rows": rows, "batch_key": "k"}, FakeAllegro(deep_echo)
    )
    assert result["ok"] is True and result["external_effect_status"] == "succeeded"
    assert result["result"]["commands"][0]["status"] == "registered"
    assert any("does not repeat the command ID" in w for w in result["result"]["warnings"])


# -- O2: the budget measures what is written; the last guard keeps a write a success ------


def test_the_budget_measures_decimals_as_they_are_written() -> None:
    value = {"n": Decimal("1E+30")}
    written = json.dumps({"n": "1" + "0" * 30}, ensure_ascii=False, separators=(",", ":"))
    assert budget.measure(value).size == len(written.encode())
    # A million amounts of 31 digits are written as 33+ MB, far over the budget.
    assert not budget.fits([Decimal("1E+30")] * 120_000)


def test_the_last_guard_keeps_a_confirmed_write_a_success() -> None:
    huge = {
        "ok": True,
        "result": {
            "id": "1",
            "status": 201,
            "processing": "accepted",
            "check": {"operation_id": "x", "location": None},
            "blob": "x" * 6_000_000,
            "warnings": ["first"],
        },
        "error_code": None,
        "external_effect_status": "succeeded",
        "definitely_no_external_effect": False,
    }
    final, serialized = main._bounded_serialized_response(huge)
    assert final["ok"] is True and final["error_code"] is None
    assert final["external_effect_status"] == "succeeded"
    assert final["definitely_no_external_effect"] is False
    out = final["result"]
    assert out["id"] == "1" and out["status"] == 201 and out["processing"] == "accepted"
    assert out["check"] == {"operation_id": "x", "location": None}
    assert "blob" not in out
    assert out["warnings"][0] == "first" and "too large" in out["warnings"][-1]
    assert len(serialized) < 10_000


def test_the_last_guard_of_an_unknown_write_stays_an_error() -> None:
    huge = {
        "ok": False,
        "result": {"blob": "x" * 6_000_000},
        "error_code": "timeout_unknown",
        "external_effect_status": "timeout_unknown",
        "definitely_no_external_effect": False,
    }
    final, _ = main._bounded_serialized_response(huge)
    assert final["ok"] is False and final["error_code"] == "response_too_large"
    assert final["external_effect_status"] == "timeout_unknown"


# -- O3: a write that sent nothing ---------------------------------------------------------


def test_a_write_step_that_sent_nothing_reports_no_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    def found_already(context: Any) -> dict[str, Any]:
        return {
            "commands": [],
            "offers_count": 0,
            "processing": "accepted",
            "applied": True,
            "check": None,
            "warnings": [],
        }

    monkeypatch.setitem(composites.COMPOSITES, "update_stock_and_prices", found_already)
    fake = FakeAllegro()
    result = run(
        "update_stock_and_prices",
        {"rows": [{"offer_id": "1", "quantity": 2}], "batch_key": "k"},
        fake,
    )
    assert fake.calls == []
    assert result["ok"] is True
    assert result["external_effect_status"] == "failed"
    assert result["definitely_no_external_effect"] is True
    assert result["result"]["processing"] is None and result["result"]["applied"] is False


# -- O4: headers pass through the token removal ----------------------------------------------


def test_the_location_header_never_carries_the_token() -> None:
    location = f"https://api.allegro.pl/sale/product-offers/1?t={TOKEN}"
    fake = FakeAllegro(ok({"id": "1"}, 202, {"Content-Type": PUBLIC, "Location": location}))
    result = run("edit_offer", {"offer_id": "1", "body": {"a": 1}}, fake)
    assert TOKEN not in text_of(result)
    assert (
        result["result"]["location"] == "https://api.allegro.pl/sale/product-offers/1?t=[redacted]"
    )
    assert result["result"]["check"]["location"] == result["result"]["location"]
    fake = FakeAllegro(refuse(303, b"", Location=location, ETag=TOKEN))
    result = run("check_offer_processing_status", {"offer_id": "1", "operation_id": "op"}, fake)
    assert result["ok"] is True
    assert TOKEN not in text_of(result)
    assert result["result"]["etag"] == "[redacted]"


def test_a_token_cut_by_the_header_bound_is_still_removed() -> None:
    # The bound (2000 characters) falls inside the token: removed first, nothing of it is left.
    location = "https://api.allegro.pl/" + "a" * 1960 + TOKEN
    fake = FakeAllegro(ok({"id": "1"}, 200, {"Content-Type": PUBLIC, "Location": location}))
    result = run("edit_offer", {"offer_id": "1", "body": {"a": 1}}, fake)
    assert TOKEN[:12] not in text_of(result)


# -- O7: a batch that is only partly registered ----------------------------------------------

ROWS = [
    {"offer_id": "11", "quantity": 5, "price": "49.90"},
    {"offer_id": "12", "quantity": 5},
    {"offer_id": "13", "price": "49.9", "currency": "PLN"},
    {"offer_id": "14", "price": "10.00", "currency": "CZK"},
]


def echo(call: Any) -> Any:
    return ok({"id": call.path.rsplit("/", 1)[1]}, 201)


def test_a_definite_refusal_after_registered_commands_is_a_partial_success() -> None:
    fake = FakeAllegro(echo, refuse(422, errors_holder("PRICE", "Too low")))
    result = run("update_stock_and_prices", {"rows": ROWS, "batch_key": "k"}, fake)
    assert result["ok"] is True, result
    assert result["external_effect_status"] == "succeeded"
    assert result["definitely_no_external_effect"] is False
    out = result["result"]
    assert [c["status"] for c in out["commands"]] == ["registered", "refused", "not_sent"]
    assert out["offers_count"] == 2
    assert out["processing"] == "accepted" and out["applied"] is None
    assert "Only 1 of 3 commands are registered" in out["warnings"][-1]
    assert "Too low" in out["warnings"][-1]
    assert len(fake.calls) == 2


def test_running_out_of_time_after_registered_commands_is_a_partial_success() -> None:
    now = [0.0]

    def slow_echo(call: Any) -> Any:
        now[0] += 560
        return echo(call)

    fake = FakeAllegro(slow_echo)
    result = run(
        "update_stock_and_prices", {"rows": ROWS, "batch_key": "k"}, fake, clock=lambda: now[0]
    )
    assert result["ok"] is True, result
    assert result["external_effect_status"] == "succeeded"
    out = result["result"]
    assert [c["status"] for c in out["commands"]] == ["registered", "not_sent", "not_sent"]
    assert out["applied"] is None
    assert "was not sent" in out["warnings"][-1]
    assert len(fake.calls) == 1


def test_an_unclear_command_after_many_registered_names_them_within_the_host_limit() -> None:
    rows = [{"offer_id": str(i), "quantity": i} for i in range(1, 10)]
    fake = FakeAllegro(*([echo] * 8), TimeoutError())
    result = run("update_stock_and_prices", {"rows": rows, "batch_key": "k"}, fake)
    planned = composites.plan_update({"rows": rows, "batch_key": "k"})
    assert result["ok"] is False
    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False
    message = result["error"]
    assert len(message) <= 512
    assert result["errors"][0]["message"] == message
    assert "8 of 9 commands are registered" in message
    assert all(c.command_id in message for c in planned[:5])
    assert "and 3 more" in message
    assert "may or may not be registered" in message
