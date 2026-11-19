"""Final round of 0.1.1: the table of write outcomes (T1-T6) decided in one function,
lists measured as whole results (N1), exact continuation (N2), an empty page that is
not the end (N3), a page larger than the transport reads (Al2) and command results
read one by one (Al3)."""

from __future__ import annotations

import importlib
from email.message import Message
from typing import Any

import pytest
from support import PUBLIC, FakeAllegro, Response, errors_holder, ok, refuse, run

from runtime import budget, generic

# -- T: the outcome of a write, one table ------------------------------------------------------

HTML = "text/html"
NONE = "none"


class CutBody:
    """A success status whose body is cut off by the connection."""

    def __init__(self, status: int, content_type: str) -> None:
        self.status = status
        self.headers = Message()
        if content_type != NONE:
            self.headers["Content-Type"] = content_type
        else:
            self.headers["X-Request-Id"] = "1"

    def read(self, size: int = -1) -> bytes:
        raise ConnectionResetError

    def __enter__(self) -> CutBody:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def nested(levels: int) -> str:
    return "[" * levels + "1" + "]" * levels


BODIES = {
    "empty": b"",
    "parsed": b'{"id": "1"}',
    "not_json": b"done",
    "broken_json": b'{"id": ',
    "deep": ('{"a": ' + nested(49) + "}").encode(),
    "too_large": b'{"id": "1", "pad": "' + b"x" * 5000 + b'"}',
    "cut_off": b"",
}


def answer_of(status: int, content_type: str, body: str) -> Any:
    if body == "cut_off":
        return CutBody(status, content_type)
    headers = {"X-Request-Id": "1"} if content_type == NONE else {"Content-Type": content_type}
    return Response(status, BODIES[body], headers)


def expected(promised: bool, content_type: str, body: str) -> str:
    """Table T, as the planner wrote it (T2 to T6; Allegro writes are class "other")."""
    if promised and content_type == HTML:
        return "unknown"  # T2: on the headers, whatever the body
    if body in {"too_large", "cut_off", "deep"}:
        return "kept_without_data"  # T4 d
    if body == "empty":
        return "success"  # T6
    if promised and body in {"not_json", "broken_json"}:
        return "unknown"  # T3
    if body == "parsed":
        return "success"  # T6
    return "text"  # T6: no JSON documented for the status


CELLS = [
    (step, content_type, body)
    for step in ("edit_offer", "assign_tags_to_offer", "update_stock_and_prices")
    for content_type in (PUBLIC, HTML, NONE)
    for body in BODIES
]


def _run_cell(step: str, content_type: str, body: str) -> dict[str, Any]:
    if step == "update_stock_and_prices":
        answer = answer_of(201, content_type, body)
        given: dict[str, Any] = {"rows": [{"offer_id": "11", "quantity": 5}], "batch_key": "k"}
    elif step == "edit_offer":
        answer = answer_of(200, content_type, body)
        given = {"offer_id": "1", "body": {"a": 1}}
    else:
        answer = answer_of(200, content_type, body)
        given = {"offer_id": "1", "body": {"tags": []}}
    return run(step, given, FakeAllegro(answer))


@pytest.mark.parametrize(("step", "content_type", "body"), CELLS)
def test_the_outcome_of_a_write_follows_table_t(
    step: str, content_type: str, body: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(generic, "MAX_ANSWER_BYTES", 1000)
    # edit_offer (200) and the stock command (201) document only JSON; assigning tags
    # documents 200 without a body.
    promised = step != "assign_tags_to_offer"
    want = expected(promised, content_type, body)
    result = _run_cell(step, content_type, body)
    if want == "unknown":
        assert result["ok"] is False, result
        assert result["error_code"] == "invalid_response"
        assert result["external_effect_status"] == "timeout_unknown"
        assert result["definitely_no_external_effect"] is False
        return
    assert result["ok"] is True, result
    assert result["external_effect_status"] == "succeeded"
    out = result["result"]
    if step == "update_stock_and_prices":
        assert out["commands"][0]["status"] == "registered"
        if want == "kept_without_data":
            assert any("not returned" in w for w in out["warnings"])
        return
    if want == "kept_without_data":
        assert out["data"] is None and out["status"] == 200
        assert "not returned" in out["warnings"][0]
    elif want == "text":
        assert isinstance(out["data"], str) and "not JSON" in out["warnings"][0]
    elif body == "parsed":
        assert out["data"] == {"id": "1"}
    else:
        assert out["data"] is None


def test_an_undocumented_success_status_never_reaches_the_table() -> None:
    # T1: edit_offer does not document 204; the transport decides (unknown).
    result = run(
        "edit_offer", {"offer_id": "1", "body": {"a": 1}}, FakeAllegro(Response(204, b"", None))
    )
    assert result["error_code"] == "invalid_response"
    assert result["external_effect_status"] == "timeout_unknown"


def test_every_write_path_goes_through_the_one_function(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = importlib.import_module("runtime.outcome")
    seen: list[str] = []
    original = outcome.write_outcome

    def spy(context: Any, entry: Any, answer: Any, shown: str, warnings: list[str]) -> Any:
        seen.append(shown)
        return original(context, entry, answer, shown, warnings)

    monkeypatch.setattr(outcome, "write_outcome", spy)
    run("edit_offer", {"offer_id": "1", "body": {"a": 1}}, FakeAllegro(ok({"id": "1"})))
    rows = [{"offer_id": "11", "quantity": 5}]
    fake = FakeAllegro(lambda call: ok({"id": call.path.rsplit("/", 1)[1]}, 201))
    run("update_stock_and_prices", {"rows": rows, "batch_key": "k"}, fake)
    assert seen == [
        "PATCH /sale/product-offers/{offerId}",
        "PUT /sale/offer-quantity-change-commands/{commandId}",
    ]


# -- N1: a list is measured as the whole result ---------------------------------------------------


def offers_answer(width: int, total: int = 100_000) -> Any:
    def answer(call: Any) -> Any:
        query = dict(call.query)
        start, limit = int(query.get("offset", 0)), int(query["limit"])
        page = [
            {"id": f"o{i}", "name": "n" * width} for i in range(start, min(total, start + limit))
        ]
        return ok({"offers": page, "totalCount": total})

    return answer


@pytest.mark.parametrize("width", range(40, 1300, 61))
def test_what_the_list_takes_always_passes_the_final_check(
    width: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(budget, "MAX_SERIALIZED_BYTES", 30_000)
    fake = FakeAllegro(routes={"GET /sale/offers": offers_answer(width)})
    result = run("list_all_my_offers", {}, fake)
    assert result["ok"] is True, result.get("error")
    out = result["result"]
    assert out["complete"] is False and out["next_offset"] == out["count"]
    assert budget.fits(out)
    events = [{"id": f"e{i}", "type": "BOUGHT", "pad": "p" * width} for i in range(5000)]

    def events_page(call: Any) -> Any:
        query = dict(call.query)
        start = int(query["from"][1:]) + 1 if "from" in query else 0
        return ok({"events": events[start : start + int(query["limit"])]})

    fake = FakeAllegro(routes={"GET /order/events": events_page})
    result = run("list_order_events", {}, fake)
    assert result["ok"] is True, result.get("error")
    assert budget.fits(result["result"])


@pytest.mark.parametrize("pad", range(29_700, 29_960, 20))
def test_a_list_filled_to_the_brim_is_not_refused_by_the_final_check(
    pad: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The records alone fit; with the result's envelope and warnings they may not. What
    # the list takes must pass the final check: it returns what fits, never fails.
    monkeypatch.setattr(budget, "MAX_SERIALIZED_BYTES", 30_000)
    records = [{"id": "o1", "name": "small"}, {"id": "o2", "name": "n" * pad}]

    def answer(call: Any) -> Any:
        query = dict(call.query)
        start, limit = int(query["offset"]), int(query["limit"])
        return ok({"offers": records[start : start + limit], "totalCount": 2})

    result = run("list_all_my_offers", {}, FakeAllegro(routes={"GET /sale/offers": answer}))
    assert result["ok"] is True, result.get("error")
    assert budget.fits(result["result"])


# -- N2: exact continuation ---------------------------------------------------------------------


def test_after_a_smaller_page_the_full_size_is_asked_again() -> None:
    fake = FakeAllegro(routes={"GET /sale/offers": offers_answer(2000, total=5000)})
    result = run("list_all_my_offers", {}, fake)
    limits = [int(dict(c.query)["limit"]) for c in fake.calls]
    assert limits[:4] == [1000, 1000, 500, 1000]
    out = result["result"]
    assert out["next_offset"] == out["count"]


def test_the_asked_maximum_ends_on_whole_pages() -> None:
    fake = FakeAllegro(routes={"GET /sale/offers": offers_answer(10, total=5000)})
    result = run("list_all_my_offers", {"max_offers": 1500}, fake)
    out = result["result"]
    assert out["count"] == 1500 and out["next_offset"] == 1500
    assert [int(dict(c.query)["limit"]) for c in fake.calls] == [1000, 500]


# -- N3: an empty page is not the end while Allegro counts more ------------------------------------


def test_an_empty_page_before_the_counted_end_is_an_incomplete_read() -> None:
    first = ok({"offers": [{"id": f"o{i}"} for i in range(1000)], "totalCount": 3000})
    fake = FakeAllegro(first, ok({"offers": [], "totalCount": 3000}))
    result = run("list_all_my_offers", {}, fake)
    assert result["ok"] is False
    assert result["error_code"] == "invalid_response"
    assert "not complete" in result["error"]
    assert result["definitely_no_external_effect"] is True


# -- Al2: a page larger than the transport reads --------------------------------------------------


def test_a_first_page_larger_than_the_transport_reads_is_asked_with_half_the_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generic, "MAX_ANSWER_BYTES", 60_000)
    fake = FakeAllegro(routes={"GET /sale/offers": offers_answer(1000, total=300)})
    result = run("list_all_my_offers", {}, fake)
    assert result["ok"] is True, result.get("error")
    limits = [int(dict(c.query)["limit"]) for c in fake.calls]
    assert limits[:6] == [1000, 500, 250, 125, 62, 31]
    out = result["result"]
    # The next page (asked at the full size again) is too large too: on a page after
    # the first, the step returns what it read with where to continue.
    assert out["count"] == 31 and out["complete"] is False and out["next_offset"] == 31


def test_a_later_page_larger_than_the_transport_reads_returns_what_was_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generic, "MAX_ANSWER_BYTES", 2_000_000)

    def answer(call: Any) -> Any:
        start = int(dict(call.query)["offset"])
        width = 10 if start == 0 else 10_000
        page = [{"id": f"o{i}", "name": "n" * width} for i in range(start, start + 1000)]
        return ok({"offers": page, "totalCount": 5000})

    result = run("list_all_my_offers", {}, FakeAllegro(routes={"GET /sale/offers": answer}))
    assert result["ok"] is True, result.get("error")
    out = result["result"]
    assert out["count"] == 1000 and out["complete"] is False and out["next_offset"] == 1000
    assert "response_too_large" in out["warnings"][-1]


def test_halving_stops_when_the_time_runs_out_and_says_requests_were_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generic, "MAX_ANSWER_BYTES", 100)
    now = [0.0]

    def answer(call: Any) -> Any:
        now[0] += 150
        return offers_answer(1000)(call)

    fake = FakeAllegro(routes={"GET /sale/offers": answer})
    result = run("list_all_my_offers", {}, fake, clock=lambda: now[0])
    assert result["error_code"] == "timeout"
    assert result["definitely_no_external_effect"] is True
    assert f"after {len(fake.calls)} requests" in result["error"]


# -- Al3: command results one by one -----------------------------------------------------------


def summary(command_id: str) -> Any:
    return ok({"id": command_id, "taskCount": {"total": 2, "success": 2, "failed": 0}})


def test_a_command_that_cannot_be_read_is_unread_and_the_others_are_returned() -> None:
    fake = FakeAllegro(refuse(404, errors_holder("NOT_FOUND", "No command")), summary("b"))
    commands = [{"command_id": "a", "kind": "price"}, {"command_id": "b", "kind": "quantity"}]
    result = run("get_stock_and_price_update_results", {"commands": commands}, fake)
    assert result["ok"] is True, result
    out = result["result"]
    assert [(r["command_id"], r["status"]) for r in out["commands"]] == [
        ("a", "unread"),
        ("b", "read"),
    ]
    assert "not_found" in out["commands"][0]["reason"]
    assert out["unread_command_ids"] == ["a"]
    assert out["all_completed"] is False


def test_when_no_command_can_be_read_the_step_fails() -> None:
    fake = FakeAllegro(refuse(429, b""))
    result = run(
        "get_stock_and_price_update_results",
        {"commands": [{"command_id": "a", "kind": "price"}]},
        fake,
    )
    assert result["error_code"] == "rate_limited"


def test_a_full_output_returns_the_read_commands_and_the_rest_to_continue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget, "MAX_SERIALIZED_BYTES", 6_000)
    commands = [{"command_id": f"c{i:03d}", "kind": "price"} for i in range(60)]
    fake = FakeAllegro(routes={})
    fake.answers = [summary(c["command_id"]) for c in commands]
    result = run("get_stock_and_price_update_results", {"commands": commands}, fake)
    assert result["ok"] is True, result
    out = result["result"]
    read = [r["command_id"] for r in out["commands"]]
    assert 0 < len(read) < 60
    assert out["unread_command_ids"] == [c["command_id"] for c in commands[len(read) :]]
    assert "output is full" in out["warnings"][-1]
    assert budget.fits(out)
