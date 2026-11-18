"""The pinned facts are what the pinned specification says."""

from __future__ import annotations

import json
import sys

import pytest
from support import BUNDLE

sys.path.insert(0, str(BUNDLE / "tests" / "reference"))

import build_contracts  # noqa: E402
import extract_reference  # noqa: E402

SOURCE = extract_reference.SOURCE


@pytest.mark.skipif(not SOURCE.exists(), reason="the vendor document is not committed")
def test_the_facts_are_extracted_from_the_pinned_document() -> None:
    assert extract_reference.extract(SOURCE.read_bytes()) == json.loads(
        extract_reference.OUTPUT.read_text(encoding="utf-8")
    )


def test_a_changed_document_is_refused() -> None:
    with pytest.raises(SystemExit, match="the specification changed"):
        extract_reference.extract(b"openapi: 3.0.0\n")


def test_the_pin_is_the_downloaded_one() -> None:
    source = build_contracts.facts()["source"]
    assert source["sha256"] == extract_reference.SHA256
    assert source["bytes"] == extract_reference.BYTES == 1538715
    assert source["downloaded"] == "2026-11-18"
    assert source["openapi"] == "3.0.0" and source["api_version"] == "latest"
    assert source["url"] == "https://developer.allegro.pl/swagger.yaml"
    assert [s["variables"]["environment"]["enum"] for s in source["servers"]] == [
        ["allegro.pl", "allegro.pl.allegrosandbox.pl"]
    ]


def test_the_single_client_id_rule_is_in_the_pinned_facts() -> None:
    note = build_contracts.facts()["source"]["oauth"]["note"]
    assert "single key (Client_ID)" in note


def test_every_operation_has_a_server_tags_and_documented_answers() -> None:
    for op in build_contracts.facts()["operations"]:
        assert op["server"] in {"api", "upload"}
        assert op["tags"] and op["responses"], op["path"]
        assert any(code[:1] in "23" for code in op["responses"]), op["path"]
