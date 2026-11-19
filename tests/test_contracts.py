"""The generated contracts match their sources; forms, inputs and the runtime agree;
the extension's standing rules (ids, names, outputs, UI, icon, version) hold."""

from __future__ import annotations

import collections
import hashlib
import json
import re
import struct
import sys
from typing import Any

import pytest
import yaml
from support import BUNDLE

sys.path.insert(0, str(BUNDLE / "tests" / "reference"))

import build_contracts  # noqa: E402
import composite_specs  # noqa: E402
import names  # noqa: E402

from runtime import catalog, composites, generic, limits  # noqa: E402

OUTPUTS = build_contracts.outputs()
SHA = "f82541bb821c050d50f27792705090acfa4cb8d4f9796b60463cefc79fea14b9"
FACTS = build_contracts.facts()
EVERYDAY = [c["operation_id"] for c in composite_specs.COMPOSITES]


def load(path: str) -> Any:
    return yaml.safe_load((BUNDLE / path).read_text(encoding="utf-8"))


def operations() -> list[dict[str, Any]]:
    return load("contracts/operation_manifest.yaml")["operations"]


@pytest.mark.parametrize("path", sorted(OUTPUTS))
def test_generated_files_are_current(path: str) -> None:
    assert (BUNDLE / path).read_text(encoding="utf-8") == build_contracts.render(
        path, OUTPUTS[path]
    ), f"{path} drifted: run python3 tests/reference/build_contracts.py"


def test_every_documented_operation_is_a_step() -> None:
    assert FACTS["source"]["sha256"] == SHA
    documented = {(o["method"], o["path"]) for o in FACTS["operations"]}
    assert len(documented) == 270
    assert len({o["path"] for o in FACTS["operations"]}) == 202
    assert collections.Counter(m for m, _ in documented) == {
        "GET": 156,
        "POST": 55,
        "PUT": 39,
        "PATCH": 5,
        "DELETE": 15,
    }
    registry = generic.registry()
    assert len(registry) == 270
    assert {(e["method"], e["path"]) for e in registry.values()} == documented
    assert set(names.TITLES) == documented
    assert {e["vendor_operation_id"] for e in registry.values()} == {
        o["operation_id"] for o in FACTS["operations"]
    }
    manifest = [o["operation_id"] for o in operations()]
    assert manifest[: len(EVERYDAY)] == EVERYDAY
    assert sorted(manifest[len(EVERYDAY) :]) == sorted(registry) and len(manifest) == 276
    assert set(manifest) | {"verify_identity"} == set(catalog.OPERATION_IDS)


def test_step_ids_are_frozen_unique_and_never_vendor_ids() -> None:
    ids = json.loads((BUNDLE / "tests/reference/step_ids.json").read_text())
    assert set(ids) == {f"{o['method']} {o['path']}" for o in FACTS["operations"]}
    values = list(ids.values()) + EVERYDAY + ["verify_identity"]
    assert len(values) == len(set(values))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{2,80}", v) for v in values)
    assert set(generic.registry()) == set(ids.values())
    vendor = {o["operation_id"] for o in FACTS["operations"]}
    assert not vendor & set(values)


def test_titles_are_verb_first_unique_and_not_noun_lists() -> None:
    titles = [o["display_name"] for o in operations()]
    assert len(titles) == len(set(titles))
    for title in titles:
        assert title.split()[0] in names.VERBS | {"Test"}, title
        assert "·" not in title and not title.startswith(("Advanced", "List ·"))


def test_everyday_steps_come_first_and_reads_precede_deletes_within_a_tag() -> None:
    ids = [o["operation_id"] for o in operations()]
    assert ids[: len(EVERYDAY)] == EVERYDAY
    registry = generic.registry()
    by_tag: dict[str, list[str]] = collections.defaultdict(list)
    for oid in ids[len(EVERYDAY) :]:
        by_tag[registry[oid]["tag"]].append(registry[oid]["method"])
    for tag, methods in by_tag.items():
        assert methods.index("GET") == 0 if "GET" in methods else True, tag
        if "DELETE" in methods:
            assert methods[-1] == "DELETE", tag


def test_every_step_has_a_warnings_output_and_writes_their_effect_facts() -> None:
    effects = load("extension.yaml")["external_effects"]
    effect_ids = {e["operation_id"] for e in effects}
    for op in operations():
        names_out = [o["name"] for o in op["outputs"]]
        assert "warnings" in names_out, op["operation_id"]
        write = op["operation_kind"] == "action"
        assert write == (op["operation_id"] in effect_ids)
        assert write == (op["operation_id"] in catalog.EFFECT_OPERATION_IDS)
        if write:
            assert {"external_effect_status", "definitely_no_external_effect"} <= set(names_out)
            assert "timeout_unknown" in op["error_codes"]
    # 114 documented non-GET operations, less the 6 that change nothing, + Update stock and prices
    assert len(effect_ids) == 109
    assert all(
        e["idempotency"] == "required" and e["test_mode_behavior"] == "suppress" for e in effects
    )
    generated_writes = {k for k, v in generic.registry().items() if v["kind"] == "write"}
    assert generated_writes | {"update_stock_and_prices"} == effect_ids


def test_the_non_get_reads_are_the_documented_list_and_match_the_manifest() -> None:
    registry = generic.registry()
    listed = set(build_contracts.READ_ONLY_NON_GET)
    documented = {(o["method"], o["path"]) for o in FACTS["operations"]}
    assert listed <= documented and all(method != "GET" for method, _ in listed)
    non_get_reads = {(e["method"], e["path"]) for e in registry.values() if e["kind"] == "read"} - {
        (m, p) for m, p in documented if m == "GET"
    }
    assert non_get_reads == listed
    ids = {(e["method"], e["path"]): oid for oid, e in registry.items()}
    reads = {ids[k] for k in listed}
    assert {"download_shipment_labels", "download_shipment_protocol"} <= reads
    # Already a read in 0.1.0: Allegro documents it as GET.
    assert registry["download_advance_ship_notice_labels"]["method"] == "GET"
    assert registry["download_advance_ship_notice_labels"]["kind"] == "read"
    effects = {e["operation_id"] for e in load("extension.yaml")["external_effects"]}
    kinds = {o["operation_id"]: o["operation_kind"] for o in operations()}
    actions = {a["action_id"]: a for a in load("ui/actions/actions.yaml")["actions"]}
    for oid in reads:
        assert oid not in effects and oid not in catalog.EFFECT_OPERATION_IDS
        assert kinds[oid] == "source" and actions[oid]["mutates_platform"] is False
    # Each one carries the specification's own words for why it changes nothing.
    facts = {(o["method"], o["path"]): o for o in FACTS["operations"]}
    for where, quote in build_contracts.READ_ONLY_NON_GET.items():
        summary, _, words = quote.partition(": ")
        assert summary == facts[where]["summary"], where
        first = words.split(" (")[0].rstrip(".")
        assert first in facts[where]["description"], where


def test_operations_with_a_json_and_another_answer_type_take_a_format() -> None:
    registry = generic.registry()
    with_formats = {oid: e["formats"] for oid, e in registry.items() if e.get("formats")}
    assert with_formats == {
        "get_fulfillment_stock": {
            "json": "application/vnd.allegro.public.v1+json",
            "csv": "text/csv",
        }
    }
    for op in FACTS["operations"]:
        has_accept = any(
            p["in"] == "header" and p["name"].lower() == "accept" for p in op["parameters"]
        )
        types = build_contracts.success_types(op)
        both = bool(build_contracts.choose_json(types)) and any(
            t not in build_contracts.JSON_TYPES for t in types
        )
        oid = next(
            k for k, e in registry.items() if (e["method"], e["path"]) == (op["method"], op["path"])
        )
        assert (both and not has_accept) == (oid in with_formats), oid
    form = next(
        f
        for f in load("contracts/step_ui_manifest.yaml")["forms"]
        if f["operation_id"] == "get_fulfillment_stock"
    )
    field = next(f for f in form["fields"] if f["name"] == "format")
    assert field["required"] is False and [o["value"] for o in field["options"]] == ["json", "csv"]
    assert "limit" in field["help_text"] and "offset" in field["help_text"]
    policy = next(
        p
        for p in load("contracts/artifact_policies.yaml")["artifact_policies"]
        if p["operation_id"] == "get_fulfillment_stock"
    )
    assert policy["outputs"][0]["content_type"] == "text/csv"
    assert "get_fulfillment_stock" in catalog.FILE_OUTPUT_IDS


def test_forms_inputs_and_runtime_agree() -> None:
    forms = {f["operation_id"]: f for f in load("contracts/step_ui_manifest.yaml")["forms"]}
    actions = {a["action_id"]: a for a in load("ui/actions/actions.yaml")["actions"]}
    registry = generic.registry()
    for op in operations():
        oid = op["operation_id"]
        inputs = [i["name"] for i in op["inputs"] if i["name"] != "connection_ref"]
        assert [f["name"] for f in forms[oid]["fields"]] == inputs, oid
        assert [p["name"] for p in actions[oid]["parameters"]] == ["connection_ref", *inputs]
        if oid in registry:
            assert set(inputs) == generic.inputs_of(registry[oid]), oid
        for item in op["inputs"]:
            if item["value_type"] == "artifact_handle":
                assert (
                    next(f for f in forms[oid]["fields"] if f["name"] == item["name"])[
                        "widget_type"
                    ]
                    == "artifact_picker"
                )


def test_the_post_connect_action_has_the_closed_automatic_profile() -> None:
    action = next(
        a for a in load("ui/actions/actions.yaml")["actions"] if a["action_id"] == "verify_identity"
    )
    assert action["workflow_visible"] is False and action["parameters"] == []
    assert action["mutates_platform"] is False and action["handler"] == {"mode": "extension"}
    assert "verify_identity" not in {o["operation_id"] for o in operations()}


def test_media_types_come_from_the_specification_per_operation() -> None:
    registry = generic.registry()
    facts = {(o["method"], o["path"]): o for o in FACTS["operations"]}
    for entry in registry.values():
        op = facts[(entry["method"], entry["path"])]
        documented = {
            m.replace(" ", "")
            for c, r in op["responses"].items()
            if c[:1] in "23"
            for m in r["content"]
        }
        if entry["accept"] and entry["answer"] == "json" and documented:
            assert entry["accept"] in documented, entry["path"]
        if entry["body"] and entry["body"]["json"]:
            assert entry["body"]["json"]["content_type"] in op["request"]["content"]
    beta_only = [
        e for e in registry.values() if e["accept"] == "application/vnd.allegro.beta.v1+json"
    ]
    assert len(beta_only) == 12
    # The one media type the spec writes with a stray space is sent without it.
    policy = next(
        e
        for e in registry.values()
        if e["vendor_operation_id"] == "deleteAfterSalesServiceReturnPolicyUsingDELETE"
    )
    assert policy["accept"] == "application/vnd.allegro.public.v1+json"
    # Where Allegro lists both, the public (stable) version is asked for.
    threads = next(e for e in registry.values() if e["vendor_operation_id"] == "listThreadsGET")
    assert threads["accept"] == "application/vnd.allegro.public.v1+json"


def test_upload_operations_use_the_upload_host() -> None:
    upload = sorted(e["path"] for e in generic.registry().values() if e["server"] == "upload")
    assert upload == [
        "/after-sales-service-conditions/attachments/{attachmentId}",
        "/sale/images",
        "/sale/offer-attachments/{attachmentId}",
    ]


def test_connection_types_and_oauth_providers_match_the_specification() -> None:
    types = load("contracts/connection_types.yaml")["connection_types"]
    assert [t["connection_type_id"] for t in types] == ["allegro", "allegro_sandbox"]
    providers = {
        p["provider_id"]: p
        for p in load("extension.yaml")["runtime"]["extension_contract_v2"]["oauth_providers"]
    }
    oauth = FACTS["source"]["oauth"]
    assert providers["allegro"]["authorization_endpoint"]["url"] == oauth["authorization_url"]
    assert providers["allegro"]["token_endpoint"]["url"] == oauth["token_url"]
    sandbox = providers["allegro_sandbox"]
    assert (
        sandbox["token_endpoint"]["url"] == "https://allegro.pl.allegrosandbox.pl/auth/oauth/token"
    )
    for provider in providers.values():
        assert provider["flow"] == {"type": "authorization_code", "pkce": "s256"}
        assert provider["token_endpoint"]["client_auth_method"] == "client_secret_basic"
        assert provider["post_connect_action_id"] == "verify_identity"
    all_scopes = sorted({s for o in FACTS["operations"] for s in o["user_scopes"]})
    for row in types:
        assert (
            row["auth"]["type"] == "oauth2"
            and row["auth"]["provider_id"] == row["connection_type_id"]
        )
        scopes = row["auth"]["scopes"]
        assert scopes["required"] == ["allegro:api:profile:read"]
        assert sorted(scopes["required"] + scopes["optional"]) == all_scopes


def test_every_description_names_the_permission_it_needs() -> None:
    registry = generic.registry()
    for oid, entry in registry.items():
        for scope in entry["scopes"]:
            assert scope in entry["description"], oid
    for spec in composite_specs.COMPOSITES:
        for scope in spec["scopes"]:
            assert scope in spec["description"], spec["operation_id"]


def _pages() -> list[tuple[str, str]]:
    manifest = load("ui/ui_manifest.yaml")
    out = []
    for ref in manifest["pages"]:
        page = load("ui/" + ref["ref"])
        for comp in page.get("components", []):
            out.append((page["page_id"], str(comp.get("body") or comp.get("content") or "")))
    return out


def test_ui_pages_have_no_tables_and_no_forbidden_words() -> None:
    for page_id, body in _pages():
        for line in body.splitlines():
            assert not line.strip().startswith("|"), (page_id, line)
        lowered = body.lower()
        for word in ("not verified", "unverified", "не проверено", "limits"):
            assert word not in lowered, (page_id, word)


def test_the_icon_is_a_square_png_and_branding_records_its_hash() -> None:
    data = (BUNDLE / "icon.png").read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    assert width == height and 32 <= width <= 512
    branding = (BUNDLE / "BRANDING.md").read_text(encoding="utf-8")
    assert hashlib.sha256(data).hexdigest() in branding
    assert "developer.allegro.pl/favicon.ico" in branding


def test_version_step_limit_and_changelog() -> None:
    manifest = load("extension.yaml")
    assert manifest["version"] == "0.1.1"
    assert manifest["entrypoint"]["timeout_seconds"] == 600
    assert limits.STEP_TIMEOUT_SECONDS == 600
    changelog = (BUNDLE / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## 0.1.1" in changelog and "## 0.1.0" in changelog
    assert changelog.index("## 0.1.1") < changelog.index("## 0.1.0")
    page = json.dumps(load("ui/pages/changelog.yaml"))
    assert "0.1.1" in page and "0.1.0" in page


def test_the_runtime_names_no_server_beyond_allegro() -> None:
    hosts = set()
    for path in (BUNDLE / "runtime").glob("*.py"):
        hosts |= set(re.findall(r"https://([a-z0-9.-]+)", path.read_text(encoding="utf-8")))
    assert hosts <= {"api.allegro.pl", "upload.allegro.pl"} | set()
    egress = set(load("extension.yaml")["egress"]["hosts"])
    assert egress == {
        "api.allegro.pl",
        "upload.allegro.pl",
        "api.allegro.pl.allegrosandbox.pl",
        "upload.allegro.pl.allegrosandbox.pl",
    }


def test_composite_writes_and_validators_are_declared() -> None:
    assert composites.COMPOSITE_WRITES == {"update_stock_and_prices"}
    assert set(composites.VALIDATORS) == {"update_stock_and_prices"}
