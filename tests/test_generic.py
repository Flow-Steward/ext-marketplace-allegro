"""The generated steps: requests built from the registry, answers read as documented,
test runs that send nothing, and results kept inside the output budget."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
import yaml
from support import BETA, BUNDLE, PUBLIC, Artifacts, FakeAllegro, ok, raw, run, text_of

from runtime import budget, catalog, generic

REGISTRY = generic.registry()
WRITES = sorted(oid for oid, e in REGISTRY.items() if e["kind"] == "write")


def minimal_input(entry: dict[str, Any]) -> dict[str, Any]:
    """The smallest input the step accepts, built from its registry entry."""
    given: dict[str, Any] = {}
    for prm in entry["path_params"]:
        given[prm["input"]] = "1" * max(1, int(prm["node"].get("min_length") or 1))
    for prm in entry["query"] + entry["headers"]:
        n = prm["node"]
        if not n["required"]:
            continue
        sample: Any = {
            "integer": n.get("minimum", 1),
            "decimal": "1",
            "boolean": True,
            "pairs": {"a": "b"},
        }.get(n["kind"], (n.get("allowed") or ["x"])[0])
        if n["kind"] == "text_list":
            sample = [(n.get("allowed") or ["x"])[0]]
        given[prm["input"]] = sample
    if entry["accept_choices"]:
        given["accept"] = entry["accept_choices"][0]
    body = entry["body"]
    if body and body["required"]:
        if body["json"]:
            shape = body["json"]
            given["body"] = [] if shape["type"] == "array" else {k: "x" for k in shape["required"]}
            if shape["type"] == "array":
                given["body"] = [{"x": 1}]
            if not given["body"]:
                given["body"] = {"x": "y"}
        else:
            given["file"] = "upload-1"
            for part in body["multipart"] or []:
                if part["required"] and part["encoding"] != "binary":
                    given[generic.snake(part["name"])] = "x"
    return given


@pytest.mark.parametrize("operation_id", WRITES)
def test_a_test_run_checks_the_inputs_and_sends_nothing(operation_id: str) -> None:
    fake = FakeAllegro()
    result = run(operation_id, minimal_input(REGISTRY[operation_id]), fake, test_mode=True)
    assert result["ok"] is True, result
    assert fake.calls == []
    assert result["definitely_no_external_effect"] is True
    assert result["external_effect_status"] == "failed"
    assert result["result"]["warnings"]
    out = result["result"]
    assert out["processing"] is None and out["applied"] is False and out["check"] is None


@pytest.mark.parametrize("operation_id", WRITES)
def test_a_real_write_sends_exactly_one_request(operation_id: str) -> None:
    entry = REGISTRY[operation_id]
    status = int(next(s for s in entry["success"] if s.startswith("2")))
    if status == 204:
        answer = raw(204, b"")
    elif entry["answer"] == "file":
        answer = raw(status, b"%PDF-1.4", "application/octet-stream")
    else:
        answer = ok({"id": "1"}, status)
    fake = FakeAllegro(answer)
    artifacts = Artifacts({"upload-1": b"%PDF-1.4 file"})
    result = run(operation_id, minimal_input(entry), fake, artifacts=artifacts)
    assert result["ok"] is True, result
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.verb == entry["method"]
    assert result["external_effect_status"] == "succeeded"
    assert result["definitely_no_external_effect"] is False
    accepted = status == 202 or entry["processing"]["accepted_always"]
    assert result["result"]["processing"] == ("accepted" if accepted else "completed")
    assert result["result"]["applied"] is (not accepted)


def test_a_test_run_still_refuses_a_bad_input() -> None:
    fake = FakeAllegro()
    result = run("edit_offer", {"offer_id": "1"}, fake, test_mode=True)
    assert result["error_code"] == "invalid_payload" and fake.calls == []


@pytest.mark.parametrize("operation_id", sorted(REGISTRY))
def test_every_step_takes_its_minimal_input(operation_id: str) -> None:
    generic.prepare(operation_id, minimal_input(REGISTRY[operation_id]))


def test_path_values_are_one_encoded_segment() -> None:
    fake = FakeAllegro(ok({"id": "x"}))
    run("get_offer", {"offer_id": "A/1?x=#"}, fake)
    assert fake.calls[0].url.endswith("/sale/product-offers/A%2F1%3Fx%3D%23")


def test_query_lists_repeat_and_values_are_typed() -> None:
    fake = FakeAllegro(ok({"offers": []}))
    result = run(
        "get_offers_page",
        {
            "publication_status": ["ACTIVE", "ENDED"],
            "limit": 5,
            "offset": "10",
            "selling_mode_price_amount_gte": "9.90",
            "product_id_empty": True,
            "accept_language": "pl-PL",
        },
        fake,
    )
    assert result["ok"] is True
    call = fake.calls[0]
    assert ("publication.status", "ACTIVE") in call.query and (
        "publication.status",
        "ENDED",
    ) in call.query
    assert ("limit", "5") in call.query and ("offset", "10") in call.query
    assert ("sellingMode.price.amount.gte", "9.90") in call.query
    assert ("product.id.empty", "true") in call.query
    assert call.headers["accept-language"] == "pl-PL"


@pytest.mark.parametrize(
    "given",
    [
        {"publication_status": ["SOLD"]},
        {"limit": 0},
        {"limit": 1001},
        {"limit": True},
        {"selling_mode_price_amount_gte": 9.9},
        {"selling_mode_price_amount_gte": "-1"},
        {"accept_language": "de-DE"},
        {"unknown": 1},
        {"name": "a\x00b"},
    ],
)
def test_bad_values_are_refused_before_sending(given: dict[str, Any]) -> None:
    fake = FakeAllegro()
    result = run("get_offers_page", given, fake)
    assert result["error_code"] == "invalid_payload" and fake.calls == []


def test_required_parameters_and_body_fields_are_checked() -> None:
    fake = FakeAllegro()
    assert run("get_offer", {}, fake)["error_code"] == "invalid_payload"
    result = run("upload_offer_image", {"body": {"isAiCoCreated": True}}, fake)
    assert result["error_code"] == "invalid_payload" and "url" in result["error"]
    assert fake.calls == []


def test_a_beta_operation_is_sent_with_the_beta_media_types() -> None:
    entry = next(
        e for e in REGISTRY.values() if e["vendor_operation_id"] == "rejectCustomerReturnRefund"
    )
    oid = next(k for k, v in REGISTRY.items() if v is entry or v == entry)
    fake = FakeAllegro(ok({"id": "r"}))
    result = run(oid, minimal_input(entry), fake)
    assert result["ok"] is True
    assert fake.calls[0].headers["accept"] == BETA
    assert fake.calls[0].headers["content-type"] == BETA


def test_a_json_body_is_sent_with_the_documented_media_type_and_exact_numbers() -> None:
    fake = FakeAllegro(ok({"id": "1"}))
    run(
        "edit_offer",
        {
            "offer_id": "1",
            "body": {"sellingMode": {"price": {"amount": "49.90"}}, "n": Decimal("1.10")},
        },
        fake,
    )
    call = fake.calls[0]
    assert call.verb == "PATCH" and call.headers["content-type"] == PUBLIC
    assert call.raw_body == b'{"sellingMode":{"price":{"amount":"49.90"}},"n":1.10}'


def test_an_upload_sends_the_workflow_file_with_its_type() -> None:
    artifacts = Artifacts({"img": b"\x89PNG..."})
    fake = FakeAllegro(ok({"location": "https://a.allegroimg.com/x"}, 201))
    result = run(
        "upload_offer_image",
        {"file": "img", "content_type": "image/png"},
        fake,
        artifacts=artifacts,
    )
    assert result["ok"] is True
    call = fake.calls[0]
    assert call.host == "upload.allegro.pl" and call.raw_body == b"\x89PNG..."
    assert call.headers["content-type"] == "image/png"
    result = run(
        "upload_offer_image",
        {"file": "img", "content_type": "image/gif"},
        FakeAllegro(),
        artifacts=artifacts,
    )
    assert result["error_code"] == "invalid_payload"
    result = run(
        "upload_offer_image",
        {"file": "img", "body": {"url": "https://x"}},
        FakeAllegro(),
        artifacts=artifacts,
    )
    assert result["error_code"] == "invalid_payload"


def test_a_missing_upload_file_sends_nothing() -> None:
    fake = FakeAllegro()
    result = run("upload_offer_image", {"file": "nope"}, fake, artifacts=Artifacts())
    assert result["error_code"] == "artifact_input_unavailable" and fake.calls == []
    assert result["definitely_no_external_effect"] is True


def test_the_multipart_invoice_upload() -> None:
    artifacts = Artifacts({"pdf": b"%PDF-1.7 invoice"})
    fake = FakeAllegro(ok({"id": "inv"}, 201))
    result = run(
        "upload_order_invoice_with_details",
        {"order_id": "o1", "file": "pdf", "invoice_number": "FV/1/2026"},
        fake,
        artifacts=artifacts,
    )
    assert result["ok"] is True
    call = fake.calls[0]
    assert call.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="invoiceNumber"\r\n\r\nFV/1/2026' in call.raw_body
    assert (
        b'name="file"; filename="document.pdf"' in call.raw_body
        and b"%PDF-1.7 invoice" in call.raw_body
    )


def test_a_file_answer_is_saved_and_never_in_the_output() -> None:
    artifacts = Artifacts()
    fake = FakeAllegro(raw(200, b"%PDF-1.4 labels", "application/pdf"))
    result = run(
        "download_advance_ship_notice_labels",
        {"id": "a1", "accept": "application/pdf"},
        fake,
        artifacts=artifacts,
    )
    assert result["ok"] is True
    assert fake.calls[0].headers["accept"] == "application/pdf"
    assert result["result"]["file"] == "artifact-1" and result["result"]["size_bytes"] == 15
    assert artifacts.saved[0]["data"] == b"%PDF-1.4 labels"
    assert artifacts.saved[0]["binding_key"] == "allegro_file"
    assert "labels" not in text_of(result["result"])
    assert (
        run("download_advance_ship_notice_labels", {"id": "a1"}, FakeAllegro())["error_code"]
        == "invalid_payload"
    )


def test_a_read_answer_that_is_not_json_is_refused_and_a_write_keeps_it_as_text() -> None:
    result = run("get_my_account", {}, FakeAllegro(raw(200, b"<html>")))
    assert (
        result["error_code"] == "invalid_response"
        and result["definitely_no_external_effect"] is True
    )
    # Where the specification documents no body for the status, a text body is kept.
    result = run(
        "assign_tags_to_offer",
        {"offer_id": "1", "body": {"tags": []}},
        FakeAllegro(raw(200, b"done", content_type="text/plain")),
    )
    assert result["ok"] is True and result["result"]["data"] == "done"
    assert "not JSON" in result["result"]["warnings"][0]


@pytest.mark.parametrize(
    ("body", "content_type"),
    [(b"done", PUBLIC), (b'{"id": "1"}', "text/html"), (b"<html>login</html>", "text/html")],
)
def test_a_write_documented_as_json_with_another_body_is_unknown(
    body: bytes, content_type: str
) -> None:
    fake = FakeAllegro(raw(200, body, content_type=content_type))
    result = run("edit_offer", {"offer_id": "1", "body": {"a": 1}}, fake)
    assert result["ok"] is False
    assert result["error_code"] == "invalid_response"
    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False
    assert "not the JSON the specification documents" in result["error"]


def test_http_202_is_accepted_not_applied_and_names_its_check() -> None:
    location = "https://api.allegro.pl/sale/product-offers/1/operations/op"
    fake = FakeAllegro(ok({"id": "1"}, 202, {"Content-Type": PUBLIC, "Location": location}))
    result = run("edit_offer", {"offer_id": "1", "body": {"a": 1}}, fake)
    assert result["ok"] is True
    # Accepted: resending would create a duplicate, so the effect stays succeeded for Core.
    assert result["external_effect_status"] == "succeeded"
    out = result["result"]
    assert out["status"] == 202
    assert out["processing"] == "accepted" and out["applied"] is False
    assert out["check"] == {"operation_id": "check_offer_processing_status", "location": location}


@pytest.mark.parametrize(
    ("operation_id", "status"),
    [("edit_offer", 200), ("create_offer_from_product", 201), ("delete_draft_offer", 204)],
)
def test_a_synchronous_answer_is_completed(operation_id: str, status: int) -> None:
    answer = raw(204, b"") if status == 204 else ok({"id": "1"}, status)
    fake = FakeAllegro(answer)
    result = run(operation_id, minimal_input(REGISTRY[operation_id]), fake)
    assert result["ok"] is True, result
    out = result["result"]
    assert out["processing"] == "completed" and out["applied"] is True and out["check"] is None


def test_a_registered_command_is_accepted_whatever_its_status() -> None:
    entry = REGISTRY["change_price_of_many_offers"]
    location = "https://api.allegro.pl/sale/offer-price-change-commands/c"
    fake = FakeAllegro(ok({"id": "c"}, 201, {"Content-Type": PUBLIC, "Location": location}))
    result = run("change_price_of_many_offers", minimal_input(entry), fake)
    out = result["result"]
    assert out["processing"] == "accepted" and out["applied"] is False
    assert out["check"] == {
        "operation_id": "get_price_change_command_summary",
        "location": location,
    }
    # A command without a documented check step still says it was only accepted.
    entry = REGISTRY["change_buy_now_price_of_offer"]
    result = run("change_buy_now_price_of_offer", minimal_input(entry), FakeAllegro(ok({}, 200)))
    out = result["result"]
    assert out["processing"] == "accepted" and out["applied"] is False
    assert out["check"] == {"operation_id": None, "location": None}


def test_every_write_declares_and_returns_processing() -> None:
    manifest = yaml.safe_load((BUNDLE / "contracts/operation_manifest.yaml").read_text())
    for op in manifest["operations"]:
        names = [o["name"] for o in op["outputs"]]
        write = op["operation_kind"] == "action"
        assert ({"processing", "applied", "check"} <= set(names)) is write, op["operation_id"]
    for oid in WRITES:
        check = REGISTRY[oid]["processing"]["check"]
        assert check is None or REGISTRY[check]["kind"] == "read", oid


def test_the_etag_and_if_match_round_trip() -> None:
    fake = FakeAllegro(ok({"id": "a"}, 200, {"Content-Type": PUBLIC, "ETag": "7"}))
    result = run("get_advance_ship_notice", {"id": "a"}, fake)
    assert result["result"]["etag"] == "7"
    entry = REGISTRY["update_advance_ship_notice"]
    fake = FakeAllegro(ok({"id": "a"}))
    given = {**minimal_input(entry), "if_match": "7"}
    run("update_advance_ship_notice", given, fake)
    assert fake.calls[0].headers["if-match"] == "7"
    assert (
        run(
            "update_advance_ship_notice",
            {k: v for k, v in given.items() if k != "if_match"},
            FakeAllegro(),
        )["error_code"]
        == "invalid_payload"
    )


def test_a_read_too_large_for_the_output_is_refused() -> None:
    offers = [{"id": str(i), "name": "x" * 150} for i in range(30000)]
    result = run("get_offers_page", {}, FakeAllegro(ok({"offers": offers})))
    assert result["error_code"] == "response_too_large"
    assert result["definitely_no_external_effect"] is True


def test_a_write_answer_too_large_for_the_output_keeps_its_ids_and_facts() -> None:
    big = {"id": "offer-1", "items": [{"id": str(i), "text": "y" * 200} for i in range(20000)]}
    result = run("edit_offer", {"offer_id": "1", "body": {"a": 1}}, FakeAllegro(ok(big)))
    assert result["ok"] is True
    assert result["external_effect_status"] == "succeeded"
    assert budget.fits(result["result"])
    assert any("too large" in w for w in result["result"]["warnings"])


def test_every_generated_step_is_routable() -> None:
    assert set(REGISTRY) <= catalog.OPERATION_IDS
    for entry in REGISTRY.values():
        json.dumps(entry)


# -- reads Allegro documents with another method than GET -------------------------------


def test_a_test_run_downloads_shipment_labels_as_a_read() -> None:
    fake = FakeAllegro(raw(200, b"%PDF-1.4 labels", "application/octet-stream"))
    artifacts = Artifacts()
    result = run(
        "download_shipment_labels",
        {"body": {"shipmentIds": ["s1"]}},
        fake,
        artifacts=artifacts,
        test_mode=True,
    )
    assert result["ok"] is True, result
    assert len(fake.calls) == 1 and fake.calls[0].verb == "POST"
    assert result["result"]["file"] == "artifact-1"
    assert artifacts.saved[0]["data"] == b"%PDF-1.4 labels"
    assert "external_effect_status" not in result
    assert "processing" not in result["result"]


def test_a_network_failure_of_a_non_get_read_is_an_ordinary_read_failure() -> None:
    fake = FakeAllegro(ConnectionResetError())
    result = run("download_shipment_protocol", {"body": {"shipmentIds": ["s1"]}}, fake)
    assert result["error_code"] == "connection_failed"
    assert result["definitely_no_external_effect"] is True
    assert result["external_effect_status"] == "failed"


# -- a format other than JSON --------------------------------------------------------------


def test_fulfillment_stock_is_json_by_default() -> None:
    fake = FakeAllegro(ok({"stock": []}))
    result = run("get_fulfillment_stock", {}, fake)
    assert result["ok"] is True
    assert fake.calls[0].headers["accept"] == PUBLIC
    out = result["result"]
    assert out["data"] == {"stock": []} and out["file"] is None


def test_fulfillment_stock_as_csv_is_a_workflow_file() -> None:
    fake = FakeAllegro(raw(200, b"productId;available\nx;3\n", "text/csv"))
    artifacts = Artifacts()
    result = run("get_fulfillment_stock", {"format": "csv"}, fake, artifacts=artifacts)
    assert result["ok"] is True, result
    assert fake.calls[0].headers["accept"] == "text/csv"
    out = result["result"]
    assert out["data"] is None and out["file"] == "artifact-1"
    assert out["content_type"] == "text/csv" and out["size_bytes"] == 24
    assert artifacts.saved[0]["content_type"] == "text/csv"
    assert run("get_fulfillment_stock", {"format": "xml"}, FakeAllegro())["error_code"] == (
        "invalid_payload"
    )
