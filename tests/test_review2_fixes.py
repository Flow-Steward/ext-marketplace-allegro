"""Second review round of 0.1.1: the result budget and real lists, a confirmed write
whose body is not read, the token forms computed once, impossible dates, warnings in
error envelopes, lists that stop on a transient failure, trimming that keeps the write's
check and row statuses, Retry-After and a batch key used twice."""

from __future__ import annotations

import io
import json
import sys
from email.message import Message
from typing import Any

import pytest
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
)

import main
from runtime import budget, connection, results

# -- plausible records ------------------------------------------------------------------------


def offer(i: int) -> dict[str, Any]:
    """An offer as GET /sale/offers lists it: 30+ fields with nested objects."""
    return {
        "id": f"{7700000000 + i}",
        "name": f"Wireless headphones model {i} with noise cancelling, black",
        "category": {"id": "257921"},
        "primaryImage": {"url": f"https://a.allegroimg.com/original/11/{i:08d}/image.jpg"},
        "sellingMode": {
            "format": "BUY_NOW",
            "price": {"amount": "199.99", "currency": "PLN"},
            "minimalPrice": None,
            "startingPrice": None,
        },
        "saleInfo": {"currentPrice": None, "biddersCount": 0},
        "stats": {"watchersCount": i % 17, "visitsCount": i * 3},
        "stock": {"available": 25, "sold": i % 40},
        "publication": {
            "status": "ACTIVE",
            "startingAt": "2026-10-01T10:00:00.000Z",
            "startedAt": "2026-10-01T10:00:02.000Z",
            "endingAt": None,
            "endedAt": None,
            "endedBy": None,
            "marketplaces": {"base": {"id": "allegro-pl"}, "additional": []},
        },
        "afterSalesServices": {
            "impliedWarranty": {"id": "a1b2c3d4-0000-4000-8000-000000000001"},
            "returnPolicy": {"id": "a1b2c3d4-0000-4000-8000-000000000002"},
            "warranty": {"id": "a1b2c3d4-0000-4000-8000-000000000003"},
        },
        "additionalServices": {"id": "a1b2c3d4-0000-4000-8000-000000000004"},
        "external": {"id": f"SKU-{i:06d}"},
        "delivery": {
            "shippingRates": {"id": "a1b2c3d4-0000-4000-8000-000000000005"},
            "handlingTime": "PT24H",
            "additionalInfo": None,
            "shipmentDate": None,
        },
        "b2b": {"buyableOnlyByBusiness": False},
        "fundraisingCampaign": None,
        "additionalMarketplaces": {"allegro-cz": {"publication": {"state": "APPROVED"}}},
        "createdAt": "2026-09-30T08:00:00.000Z",
        "updatedAt": "2026-11-18T12:30:00.000Z",
        "language": "pl-PL",
        "location": {"city": "Warszawa", "countryCode": "PL", "postCode": "00-001"},
        "taxSettings": {"rates": [{"rate": "23.00", "countryCode": "PL"}], "subject": "GOODS"},
        "contact": None,
        "sizeTable": None,
        "messageToSellerSettings": {"mode": "OPTIONAL", "hint": None},
        "discounts": {"wholesalePriceList": None},
        "parameters": [
            {"id": "11323", "name": "Stan", "values": ["Nowy"], "valuesIds": ["11323_1"]},
            {"id": "224017", "name": "Marka", "values": ["Brand"], "valuesIds": ["224017_8"]},
        ],
    }


def order(i: int) -> dict[str, Any]:
    """A checkout form as GET /order/checkout-forms lists it."""
    return {
        "id": f"29b5d5a0-0000-4000-8000-{i:012d}",
        "messageToSeller": "Please send before Friday",
        "buyer": {
            "id": f"{40000000 + i}",
            "email": f"buyer{i}@allegromail.pl",
            "login": f"buyer_{i}",
            "firstName": "Jan",
            "lastName": "Kowalski",
            "companyName": None,
            "guest": False,
            "personalIdentity": None,
            "phoneNumber": "+48 600 100 200",
            "address": {
                "street": "Marszałkowska 1",
                "city": "Warszawa",
                "postCode": "00-001",
                "countryCode": "PL",
            },
        },
        "payment": {
            "id": f"0f8f1f2e-0000-4000-8000-{i:012d}",
            "type": "ONLINE",
            "provider": "P24",
            "finishedAt": "2026-11-18T12:31:00.000Z",
            "paidAmount": {"amount": "219.98", "currency": "PLN"},
        },
        "status": "READY_FOR_PROCESSING",
        "fulfillment": {"status": "NEW", "shipmentSummary": {"lineItemsSent": "NONE"}},
        "delivery": {
            "address": {
                "firstName": "Jan",
                "lastName": "Kowalski",
                "street": "Marszałkowska 1",
                "city": "Warszawa",
                "zipCode": "00-001",
                "countryCode": "PL",
                "phoneNumber": "+48 600 100 200",
            },
            "method": {"id": "c3066682-97a3-42fe-9eb5-3beeccab840c", "name": "Allegro Paczkomaty"},
            "pickupPoint": {"id": "WAW01M", "name": "Paczkomat WAW01M"},
            "cost": {"amount": "19.99", "currency": "PLN"},
            "time": {"from": "2026-11-20T00:00:00Z", "to": "2026-11-21T00:00:00Z"},
            "smart": True,
            "calculatedNumberOfPackages": 1,
        },
        "invoice": {"required": False, "address": None},
        "lineItems": [
            {
                "id": f"line-{i}-{n}",
                "offer": {
                    "id": f"{7700000000 + n}",
                    "name": "Wireless headphones",
                    "external": None,
                },
                "quantity": 1,
                "originalPrice": {"amount": "199.99", "currency": "PLN"},
                "price": {"amount": "199.99", "currency": "PLN"},
                "boughtAt": "2026-11-18T12:30:00.000Z",
            }
            for n in range(2)
        ],
        "surcharges": [],
        "discounts": [],
        "summary": {"totalToPay": {"amount": "219.98", "currency": "PLN"}},
        "affiliate": None,
        "updatedAt": "2026-11-18T12:31:00.000Z",
        "revision": "a1b2c3d4",
        "marketplace": {"id": "allegro-pl"},
    }


def offers_page(call: Any, total: int = 1000, make: Any = offer) -> Any:
    query = dict(call.query)
    start, limit = int(query.get("offset", 0)), int(query["limit"])
    items = [make(i) for i in range(start, min(total, start + limit))]
    return ok({"offers": items, "count": len(items), "totalCount": total})


# -- G1: the budget lets real lists through ------------------------------------------------------


def test_the_budget_limits_are_the_decided_ones() -> None:
    assert budget.MAX_NODES == 2_000_000
    assert budget.MAX_DEPTH == 48
    assert budget.MAX_SERIALIZED_BYTES == 3_500_000
    assert not hasattr(budget, "MAX_CANDIDATE_STRINGS")
    deep: Any = 1
    for _ in range(48):
        deep = [deep]
    assert budget.fits({"data": deep})
    assert not budget.fits({"data": [deep]})


def test_a_page_of_1000_real_offers_fits() -> None:
    fake = FakeAllegro(offers_page)
    result = run("list_all_my_offers", {}, fake)
    assert result["ok"] is True, result.get("error")
    out = result["result"]
    assert out["count"] == 1000 and out["complete"] is True
    # The generated step of the same page fits too.
    result = run("get_offers_page", {"limit": 1000}, FakeAllegro(offers_page))
    assert result["ok"] is True, result.get("error")
    assert len(result["result"]["data"]["offers"]) == 1000


def test_100_real_orders_fit() -> None:
    forms = [order(i) for i in range(100)]
    fake = FakeAllegro(ok({"checkoutForms": forms, "count": 100, "totalCount": 100}))
    result = run("list_orders", {}, fake)
    assert result["ok"] is True, result.get("error")
    assert result["result"]["count"] == 100


def test_a_first_page_too_large_is_asked_again_with_half_the_records() -> None:
    def big(i: int) -> dict[str, Any]:
        return {**offer(i), "description": "x" * 10_000}

    fake = FakeAllegro(*([lambda call: offers_page(call, 3000, big)] * 6))
    result = run("list_all_my_offers", {}, fake)
    assert result["ok"] is True, result.get("error")
    limits = [int(dict(c.query)["limit"]) for c in fake.calls]
    assert limits[:3] == [1000, 500, 250]
    out = result["result"]
    # 250 records fit; the next 250 do not: the step returns what fits and stops.
    assert out["count"] == 250 and out["complete"] is False
    assert out["next_offset"] == 250
    assert "output is full" in out["warnings"][-1]


# -- G2: a confirmed write whose body is not read ----------------------------------------------


class CutBody:
    """A documented 200 whose body is cut off by the connection."""

    def __init__(self) -> None:
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = PUBLIC
        self.headers["Location"] = "https://api.allegro.pl/sale/product-offers/1"

    def read(self, size: int = -1) -> bytes:
        raise ConnectionResetError

    def __enter__(self) -> CutBody:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_a_write_whose_body_is_cut_off_keeps_its_facts() -> None:
    result = run("edit_offer", {"offer_id": "1", "body": {"a": 1}}, FakeAllegro(CutBody()))
    assert result["ok"] is True, result
    assert result["external_effect_status"] == "succeeded"
    out = result["result"]
    assert out["status"] == 200 and out["data"] is None
    assert out["location"] == "https://api.allegro.pl/sale/product-offers/1"
    assert out["processing"] == "completed"
    assert "cut off" in out["warnings"][0]


# -- G4: the token's forms are computed once; text files are scrubbed, binary ones are not ----


def test_the_token_forms_are_computed_once_per_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = [0]
    original = connection.Connection.secret_forms

    def counted(self: Any) -> tuple[str, ...]:
        calls[0] += 1
        return original(self)

    monkeypatch.setattr(connection.Connection, "secret_forms", counted)
    result = run("list_all_my_offers", {}, FakeAllegro(offers_page))
    assert result["ok"] is True
    assert calls[0] == 1


def test_a_text_file_answer_is_scrubbed_and_a_binary_one_is_not() -> None:
    csv = f"sku,available\nA-1,5\nnote,{TOKEN}\n".encode()
    artifacts = Artifacts()
    fake = FakeAllegro(raw(200, csv, content_type="text/csv"))
    result = run("get_fulfillment_stock", {"format": "csv"}, fake, artifacts=artifacts)
    assert result["ok"] is True, result
    saved = artifacts.saved[0]["data"]
    assert TOKEN.encode() not in saved and b"[redacted]" in saved
    pdf = b"%PDF-1.7 " + TOKEN.encode() + b" binary"
    artifacts = Artifacts()
    fake = FakeAllegro(raw(200, pdf, content_type="application/octet-stream"))
    result = run(
        "download_shipment_labels", {"body": {"shipmentIds": ["s1"]}}, fake, artifacts=artifacts
    )
    assert result["ok"] is True, result
    assert artifacts.saved[0]["data"] == pdf


# -- G5: impossible dates ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "moment",
    [
        "2026-02-31T00:00:00Z",
        "2026-13-01T00:00:00Z",
        "2026-11-01T25:00:00Z",
        "2026-11-01T10:61:00Z",
        "0001-01-01T00:00:00+23:59",
    ],
)
def test_an_impossible_date_is_refused_before_anything_is_sent(moment: str) -> None:
    fake = FakeAllegro()
    result = run("list_orders", {"updated_from": moment}, fake)
    assert result["error_code"] == "invalid_payload", result
    assert result["definitely_no_external_effect"] is True
    assert fake.calls == []
    result = run("get_billing_entries", {"occurred_at_gte": moment}, fake)
    assert result["error_code"] == "invalid_payload", result
    assert fake.calls == []


def test_a_real_date_is_sent_and_an_impossible_http_date_is_not() -> None:
    fake = FakeAllegro(ok({"billingEntries": []}))
    result = run("get_billing_entries", {"occurred_at_gte": "2026-02-28T23:59:59+01:00"}, fake)
    assert result["ok"] is True and len(fake.calls) == 1
    fake = FakeAllegro()
    result = run(
        "get_allegro_pickup_and_drop_off_points",
        {"if_modified_since": "Mon, 31 Feb 2026 07:28:00 GMT"},
        fake,
    )
    assert result["error_code"] == "invalid_payload" and fake.calls == []


# -- G6: error envelopes carry warnings ----------------------------------------------------------


def test_error_envelopes_carry_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run("get_my_account", {}, FakeAllegro(refuse(400, errors_holder())))
    assert result["ok"] is False
    assert result["warnings"] == [] and result["result"]["warnings"] == []
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{not json")))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out))
    main.main()
    response = json.loads(out.getvalue())
    assert response["warnings"] == [] and response["result"]["warnings"] == []


# -- Al2: a list stops on a transient failure after the first page ---------------------------------


@pytest.mark.parametrize("failure", [refuse(429, b""), refuse(503, errors_holder())])
def test_a_rate_limit_or_server_error_after_the_first_page_returns_what_was_read(
    failure: Any,
) -> None:
    fake = FakeAllegro(lambda call: offers_page(call, 3000), failure)
    result = run("list_all_my_offers", {}, fake)
    assert result["ok"] is True, result
    out = result["result"]
    assert out["count"] == 1000 and out["complete"] is False and out["next_offset"] == 1000
    assert "could not give the next page" in out["warnings"][-1]
    events = [{"id": f"e{i}", "type": "BOUGHT"} for i in range(1000)]
    fake = FakeAllegro(ok({"events": events}), failure)
    result = run("list_order_events", {}, fake)
    assert result["ok"] is True, result
    assert result["result"]["complete"] is False
    assert result["result"]["last_event_id"] == "e999"


def test_a_rate_limit_on_the_first_page_is_still_an_error() -> None:
    result = run("list_all_my_offers", {}, FakeAllegro(refuse(429, b"")))
    assert result["error_code"] == "rate_limited"


# -- Al3: trimming keeps the check and the row statuses --------------------------------------------


def test_trimming_a_write_result_keeps_its_check_and_row_statuses() -> None:
    location = "https://api.allegro.pl/sale/offer-price-change-commands/" + "c" * 400
    commands = [
        {"command_id": f"cmd-{i}", "kind": "price", "status": "registered", "note": "n" * 9000}
        for i in range(400)
    ]
    result = {
        "commands": commands,
        "check": {"operation_id": "get_stock_and_price_update_results", "location": location},
        "processing": "accepted",
        "warnings": [],
    }
    fitted = results.fit_write_result(json.loads(json.dumps(result)))
    assert fitted["check"] == result["check"]
    assert [(c["command_id"], c["status"]) for c in fitted["commands"]] == [
        (f"cmd-{i}", "registered") for i in range(400)
    ]
    huge = {
        "ok": True,
        "result": {**result, "blob": "x" * 6_000_000},
        "external_effect_status": "succeeded",
        "definitely_no_external_effect": False,
    }
    final, _ = main._bounded_serialized_response(huge)
    assert final["ok"] is True
    assert final["result"]["check"] == result["check"]
    assert [(c["command_id"], c["status"]) for c in final["result"]["commands"]] == [
        (f"cmd-{i}", "registered") for i in range(400)
    ]


# -- Al4: Retry-After of an accepted write --------------------------------------------------------


def test_retry_after_of_a_202_is_returned() -> None:
    headers = {"Content-Type": PUBLIC, "Location": "https://api.allegro.pl/x", "Retry-After": "30"}
    fake = FakeAllegro(ok({"id": "1"}, 202, headers))
    result = run("edit_offer", {"offer_id": "1", "body": {"a": 1}}, fake)
    assert result["result"]["retry_after_seconds"] == 30
    fake = FakeAllegro(ok({"id": "1"}, 200, headers))
    result = run("edit_offer", {"offer_id": "1", "body": {"a": 1}}, fake)
    assert result["result"]["retry_after_seconds"] is None
    test_run = run("edit_offer", {"offer_id": "1", "body": {"a": 1}}, FakeAllegro(), test_mode=True)
    assert test_run["result"]["retry_after_seconds"] is None


# -- Al5: a batch key used twice ----------------------------------------------------------------


def test_a_used_batch_key_reads_the_earlier_outcome_and_asks_for_a_new_key() -> None:
    rows = [{"offer_id": "11", "quantity": 5}]
    summary = ok(
        {
            "id": "c",
            "taskCount": {"total": 1, "success": 1, "failed": 0},
            "completedAt": "2026-11-19T10:00:00Z",
        }
    )
    fake = FakeAllegro(
        refuse(409, errors_holder("CONFLICT", "Command id was already used")), summary
    )
    result = run("update_stock_and_prices", {"rows": rows, "batch_key": "k"}, fake)
    assert result["ok"] is True, result
    row = result["result"]["commands"][0]
    assert row["status"] == "already_registered"
    assert row["outcome"] == {
        "completed": True,
        "total": 1,
        "success": 1,
        "failed": 0,
        "waiting": 0,
    }
    assert [c.verb for c in fake.calls] == ["PUT", "GET"]
    assert fake.calls[1].path.startswith("/sale/offer-quantity-change-commands/")
    warning = result["result"]["warnings"][-1]
    assert "batch key was already used" in warning and "nothing was sent again" in warning
    fake = FakeAllegro(refuse(409, errors_holder()), refuse(404, errors_holder()))
    result = run("update_stock_and_prices", {"rows": rows, "batch_key": "k"}, fake)
    assert result["result"]["commands"][0]["outcome"] == "unknown"


def test_the_batch_key_input_asks_for_a_new_key_every_time() -> None:
    import yaml
    from support import BUNDLE

    manifest = yaml.safe_load((BUNDLE / "contracts/operation_manifest.yaml").read_text())
    step = next(o for o in manifest["operations"] if o["operation_id"] == "update_stock_and_prices")
    batch_key = next(i for i in step["inputs"] if i["name"] == "batch_key")
    assert "new key for every update" in batch_key["schema"]["description"]
