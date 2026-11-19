"""The transport: the token, the hosts, the outcome of refusals, timeouts and limits."""

from __future__ import annotations

import io
import socket
from decimal import Decimal
from typing import Any

import pytest
from flowsteward_extension_sdk import PinnedPeerError
from support import (
    PUBLIC,
    TOKEN,
    Artifacts,
    FakeAllegro,
    errors_holder,
    ok,
    raw,
    refuse,
    run,
    text_of,
)

from runtime import transport
from runtime.transport import dumps_json

WRITE = "change_stock_of_many_offers"  # PUT, documents 400/401/409/422 without bodies
EDIT = "edit_offer"  # PATCH, documents 400/401/403/404/409/422 with ErrorsHolder bodies
EDIT_INPUT = {"offer_id": "123", "body": {"name": "New"}}


def test_the_token_travels_only_in_the_authorization_header() -> None:
    fake = FakeAllegro(ok({"id": "42", "login": "seller"}))
    result = run("test_connection", {}, fake)
    assert result["ok"] is True
    call = fake.calls[0]
    assert call.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in call.url
    assert call.host == "api.allegro.pl" and call.path == "/me"
    assert call.headers["accept"] == PUBLIC
    assert TOKEN not in text_of(result)


def test_the_token_is_removed_from_answers_and_refusals() -> None:
    fake = FakeAllegro(ok({"id": "1", "echo": f"Bearer {TOKEN}", TOKEN: "key"}))
    result = run("get_my_account", {}, fake)
    assert TOKEN not in text_of(result) and "[redacted]" in text_of(result)
    fake = FakeAllegro(
        refuse(401, {"error": "invalid_token", "error_description": f"token {TOKEN} expired"})
    )
    result = run("get_my_account", {}, fake)
    assert result["error_code"] == "authentication_failed"
    assert TOKEN not in text_of(result)


def test_a_sandbox_connection_uses_the_sandbox_hosts() -> None:
    fake = FakeAllegro(ok({"id": "1"}))
    run("test_connection", {}, fake, connection_type="allegro_sandbox")
    assert fake.calls[0].host == "api.allegro.pl.allegrosandbox.pl"
    artifacts = Artifacts({"img": b"\xff\xd8jpeg"})
    fake = FakeAllegro(ok({"location": "https://x"}, 201))
    result = run(
        "upload_offer_image",
        {"file": "img", "content_type": "image/jpeg"},
        fake,
        artifacts=artifacts,
        connection_type="allegro_sandbox",
    )
    assert result["ok"] is True
    assert fake.calls[0].host == "upload.allegro.pl.allegrosandbox.pl"


@pytest.mark.parametrize(
    "connection",
    [
        {"connection_id": "conn-1", "connection_type": "agnum", "secrets": {"access_token": TOKEN}},
        {"connection_id": "conn-1", "connection_type": "allegro", "secrets": {}},
        {
            "connection_id": "conn-1",
            "connection_type": "allegro",
            "secrets": {"access_token": "short"},
        },
        {
            "connection_id": "conn-1",
            "connection_type": "allegro",
            "secrets": {"access_token": TOKEN, "refresh_token": "r" * 20},
        },
        {
            "connection_id": "other",
            "connection_type": "allegro",
            "secrets": {"access_token": TOKEN},
        },
    ],
)
def test_an_unusable_connection_is_refused_before_any_request(connection: dict[str, Any]) -> None:
    fake = FakeAllegro()
    result = run("test_connection", {}, fake, connection=connection)
    assert result["error_code"] == "invalid_connection"
    assert result["definitely_no_external_effect"] is True
    assert fake.calls == []
    assert TOKEN not in text_of(result)


# -- outcome of refusals -------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_a_documented_refusal_in_the_documented_form_changed_nothing(status: int) -> None:
    fake = FakeAllegro(refuse(status, errors_holder("E", "Bad offer")))
    result = run(EDIT, EDIT_INPUT, fake)
    assert result["ok"] is False
    assert result["definitely_no_external_effect"] is True
    assert result["external_effect_status"] == "failed"
    assert "nothing was changed" in result["error"] and "Bad offer" in result["error"]
    assert len(fake.calls) == 1


@pytest.mark.parametrize("body", [b"", b"<html>proxy</html>", b'{"message": "x"}', b"[]"])
def test_a_documented_status_without_the_documented_form_leaves_the_write_unknown(
    body: bytes,
) -> None:
    fake = FakeAllegro(refuse(422, body))
    result = run(EDIT, EDIT_INPUT, fake)
    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False
    assert "check in Allegro" in result["error"]


def test_a_status_documented_without_a_body_needs_no_body() -> None:
    fake = FakeAllegro(refuse(422))
    result = run(WRITE, {"command_id": "c1", "body": {"modification": {}}}, fake)
    assert result["definitely_no_external_effect"] is True


@pytest.mark.parametrize("status", [403, 404, 410, 500, 502, 503, 504])
def test_an_undocumented_refusal_or_a_server_error_leaves_the_write_unknown(status: int) -> None:
    fake = FakeAllegro(refuse(status, errors_holder()))
    result = run(WRITE, {"command_id": "c1", "body": {"modification": {}}}, fake)
    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False
    assert result["retryable"] is False
    assert len(fake.calls) == 1


def test_rate_limit_on_a_write_changed_nothing_and_is_not_retried() -> None:
    fake = FakeAllegro(refuse(429, b"", Retry_After="30"))
    result = run(EDIT, EDIT_INPUT, fake)
    assert result["error_code"] == "rate_limited"
    assert result["definitely_no_external_effect"] is True
    assert result["retryable"] is False
    assert "wait 30 s" in result["error"]
    assert len(fake.calls) == 1


def test_rate_limit_on_a_read_is_retryable_after_the_wait_allegro_asks_for() -> None:
    fake = FakeAllegro(refuse(429, b"", Retry_After="12"))
    result = run("get_my_account", {}, fake)
    assert result["error_code"] == "rate_limited"
    assert result["retryable"] is True and result["retry_after_seconds"] == 12
    assert len(fake.calls) == 1


def test_a_redirect_is_not_followed() -> None:
    fake = FakeAllegro(refuse(302, b"", Location="https://evil.example"))
    result = run("get_my_account", {}, fake)
    assert result["error_code"] == "redirect_rejected"
    assert len(fake.calls) == 1


def test_a_documented_redirect_is_an_answer() -> None:
    fake = FakeAllegro(refuse(303, b"", Location="https://api.allegro.pl/sale/product-offers/1"))
    result = run("check_offer_processing_status", {"offer_id": "1", "operation_id": "op"}, fake)
    assert result["ok"] is True
    assert result["result"]["status"] == 303
    assert result["result"]["location"] == "https://api.allegro.pl/sale/product-offers/1"


# -- transport failures and time -------------------------------------------------------------


def test_a_write_timeout_is_unknown_and_sent_once() -> None:
    fake = FakeAllegro(TimeoutError())
    result = run(EDIT, EDIT_INPUT, fake)
    assert result["error_code"] == "timeout_unknown"
    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False
    assert len(fake.calls) == 1


def test_a_broken_connection_after_a_write_is_unknown() -> None:
    fake = FakeAllegro(ConnectionResetError())
    result = run(EDIT, EDIT_INPUT, fake)
    assert result["external_effect_status"] == "timeout_unknown"


def test_a_read_timeout_changed_nothing() -> None:
    fake = FakeAllegro(TimeoutError())
    result = run("get_my_account", {}, fake)
    assert result["error_code"] == "timeout" and result["definitely_no_external_effect"] is True


@pytest.mark.parametrize(
    "message", ["cannot be resolved", "pinned address '10.0.0.1' is not allowed"]
)
def test_dns_and_private_addresses_fail_before_sending(message: str) -> None:
    fake = FakeAllegro(PinnedPeerError(f"Allegro: {message}"))
    result = run(EDIT, EDIT_INPUT, fake)
    assert result["definitely_no_external_effect"] is True


class SlowBody:
    """An answer whose body arrives slower than the step's time allows."""

    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.status = 200
        from email.message import Message

        self.headers = Message()
        self.headers["Content-Type"] = PUBLIC

    def read(self, size: int = -1) -> bytes:
        self.clock[0] += 400
        return b'{"a": 1}'

    def __enter__(self) -> SlowBody:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_a_write_confirmed_by_its_status_keeps_its_facts_when_its_body_is_late() -> None:
    now = [0.0]
    fake = FakeAllegro(SlowBody(now))
    result = run(EDIT, EDIT_INPUT, fake, clock=lambda: now[0])
    # HTTP 200 is documented: the change is confirmed before the body arrives.
    assert result["ok"] is True, result
    assert result["external_effect_status"] == "succeeded"
    out = result["result"]
    assert out["status"] == 200 and out["data"] is None
    assert out["processing"] == "completed"
    assert "did not arrive in time" in out["warnings"][0]


def test_the_step_limit_covers_reading_the_body_of_a_read() -> None:
    now = [0.0]
    fake = FakeAllegro(SlowBody(now))
    result = run("get_my_account", {}, fake, clock=lambda: now[0])
    assert result["error_code"] == "timeout"
    assert result["definitely_no_external_effect"] is True


def test_no_request_starts_without_time_left() -> None:
    ticks = iter([0.0] + [1000.0] * 10)
    fake = FakeAllegro()
    result = run("get_my_account", {}, fake, clock=lambda: next(ticks))
    assert result["error_code"] == "timeout" and fake.calls == []


def test_a_too_large_answer_to_a_write_is_unknown_and_to_a_read_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    big = raw(200, b'{"a":"' + b"x" * 5000 + b'"}')
    from runtime import generic

    monkeypatch.setattr(generic, "MAX_ANSWER_BYTES", 1000)
    result = run(EDIT, EDIT_INPUT, FakeAllegro(big))
    # The documented 200 confirmed the change: kept, without the body.
    assert result["ok"] is True and result["external_effect_status"] == "succeeded"
    assert result["result"]["data"] is None
    assert "is larger than" in result["result"]["warnings"][0]
    big = raw(200, b'{"a":"' + b"x" * 5000 + b'"}')
    result = run("get_my_account", {}, FakeAllegro(big))
    assert (
        result["error_code"] == "response_too_large"
        and result["definitely_no_external_effect"] is True
    )


def test_decimals_are_sent_exactly_never_through_float() -> None:
    value = Decimal("12345678901234567890.123456789")
    assert dumps_json({"amount": value, "n": [Decimal("0.10")]}) == (
        '{"amount":12345678901234567890.123456789,"n":[0.10]}'
    )
    fake = FakeAllegro(ok({"id": "1"}))
    run(EDIT, {"offer_id": "1", "body": {"x": value}}, fake)
    assert b"12345678901234567890.123456789" in fake.calls[0].raw_body


def test_a_socket_error_is_not_a_secret_leak() -> None:
    fake = FakeAllegro(socket.gaierror(f"name {TOKEN}"))
    result = run("get_my_account", {}, fake)
    assert TOKEN not in text_of(result)


def test_the_error_form_is_allegros() -> None:
    form = transport.AllegroTransport.error_form
    assert form(b'{"errors": [{"code": "X", "message": "m"}]}')
    assert form(b'{"error": "unauthorized"}')
    assert not form(b'{"errors": []}') and not form(b"{}") and not form(b"oops")
    assert not form(io.BytesIO(b"").read())


def test_a_token_written_with_json_escapes_is_still_removed() -> None:
    escaped = "".join(f"\\u{ord(ch):04x}" for ch in TOKEN)
    body = ('{"errors": [{"code": "X", "message": "' + escaped + '"}]}').encode()
    result = run("get_my_account", {}, FakeAllegro(refuse(400, body)))
    assert TOKEN not in text_of(result) and "[redacted]" in result["error"]


def test_an_internal_error_logs_its_place_and_never_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from runtime import generic

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"boom {TOKEN}")

    monkeypatch.setattr(generic, "answer_result", broken)
    result = run("get_my_account", {}, FakeAllegro(ok({"id": "1"})))
    assert result["error_code"] == "internal_error"
    assert result["definitely_no_external_effect"] is True
    captured = capsys.readouterr()
    assert TOKEN not in captured.err + captured.out + text_of(result)
    assert "RuntimeError" in captured.err


def test_an_internal_error_after_a_write_was_sent_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime import generic

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(generic, "answer_result", broken)
    result = run(EDIT, EDIT_INPUT, FakeAllegro(ok({"id": "1"})))
    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False


# -- success statuses the operation does not document -----------------------------------


def test_a_write_answered_with_an_undocumented_2xx_is_unknown() -> None:
    from test_generic import REGISTRY, minimal_input

    entry = REGISTRY["change_price_of_many_offers"]  # documents 201 only
    fake = FakeAllegro(raw(204, b""))
    result = run("change_price_of_many_offers", minimal_input(entry), fake)
    assert result["ok"] is False
    assert result["error_code"] == "invalid_response"
    assert result["definitely_no_external_effect"] is False
    assert result["external_effect_status"] == "timeout_unknown"
    assert "HTTP 204" in result["error"] and "documented: 201" in result["error"]
    assert len(fake.calls) == 1


def test_a_read_answered_with_an_undocumented_2xx_is_an_invalid_answer() -> None:
    fake = FakeAllegro(ok({"id": "1"}, 201))  # GET /me documents 200
    result = run("get_my_account", {}, fake)
    assert result["error_code"] == "invalid_response"
    assert result["definitely_no_external_effect"] is True
    assert "HTTP 201" in result["error"] and "documented: 200" in result["error"]


def test_documented_success_statuses_still_pass() -> None:
    assert run("get_my_account", {}, FakeAllegro(ok({"id": "1"})))["ok"] is True


# -- numbers outside the bounds ------------------------------------------------------------


def test_huge_numbers_in_an_answer_after_a_write_keep_the_structured_result() -> None:
    body = (
        b'{"id":"1","price":{"amount":12.50},"big":1e1000000,"small":1e-1000000,"n":'
        + b"9" * 5000
        + b"}"
    )
    fake = FakeAllegro(raw(200, body))
    result = run("edit_offer", EDIT_INPUT, fake)
    assert result["ok"] is True, result
    assert result["external_effect_status"] == "succeeded"
    data = result["result"]["data"]
    assert isinstance(data, dict) and data["id"] == "1"
    assert data["price"] == {"amount": "12.50"}
    assert data["big"] == "1e1000000" and data["small"] == "1e-1000000"
    assert data["n"] == "9" * 5000
    # Nothing was written out in full.
    assert len(text_of(result)) < 20_000


def test_a_huge_number_in_a_read_answer_is_text_not_expanded() -> None:
    fake = FakeAllegro(raw(200, b'{"id":"1","x":-2.5E+99999}'))
    result = run("get_my_account", {}, fake)
    assert result["ok"] is True and result["result"]["data"]["x"] == "-2.5E+99999"


@pytest.mark.parametrize(
    ("text", "inside"),
    [
        ("1" * 40, True),
        ("1" * 41, False),
        ("1e30", True),
        ("1e31", False),
        ("1e-30", True),
        ("1e-31", False),
        ("12345.678", True),
    ],
)
def test_the_number_bounds_come_from_digits_and_exponent(text: str, inside: bool) -> None:
    from runtime import numbers

    assert numbers.bounded(Decimal(text)) is inside
    integer = text.isdigit()
    assert (numbers.read(text, integer=integer) is not None) is inside


def test_a_decimal_outside_the_bounds_is_never_expanded_when_written() -> None:
    assert dumps_json({"x": Decimal("1e1000000")}) == '{"x":"1E+1000000"}'
    assert dumps_json({"x": Decimal("49.90")}) == '{"x":49.90}'
