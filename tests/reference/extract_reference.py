#!/usr/bin/env python3
"""Pin the facts this extension uses from the Allegro REST API specification.

Source: the OpenAPI 3.0 document Allegro publishes at
https://developer.allegro.pl/swagger.yaml (``swagger.json`` answers 404). It is not
committed: download it to tests/reference/sources/swagger.yaml, then run from the
bundle root:

    python3 tests/reference/extract_reference.py

The script refuses a document whose SHA-256 is not the pinned one, so a changed
specification is noticed rather than silently picked up. Output:
tests/reference/allegro_api.json — every operation (method, path, its vendor
operationId, parameters, request and response media types, documented statuses,
required OAuth scopes and server) with descriptions shortened. Nothing is guessed
here; build_contracts.py decides what the steps do.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "sources" / "swagger.yaml"
OUTPUT = HERE / "allegro_api.json"
URL = "https://developer.allegro.pl/swagger.yaml"
SHA256 = "f82541bb821c050d50f27792705090acfa4cb8d4f9796b60463cefc79fea14b9"
BYTES = 1538715
DOWNLOADED = "2026-11-18"
METHODS = ("get", "post", "put", "patch", "delete")
#: Schema keywords kept for a parameter (everything else is presentation).
PARAM_KEYS = (
    "type",
    "format",
    "enum",
    "minimum",
    "maximum",
    "maxLength",
    "minLength",
    "maxItems",
    "minItems",
    "default",
    "pattern",
)
_TAG_RE = re.compile(r"<[^>]+>")


def short(text: Any, limit: int = 600) -> str:
    """Plain one-line text: HTML tags and markdown links removed."""
    value = _TAG_RE.sub(" ", str(text or ""))
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


class Spec:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.components = document.get("components") or {}

    def ref(self, pointer: str) -> dict[str, Any]:
        if not pointer.startswith("#/components/"):
            raise SystemExit(f"unexpected $ref {pointer!r}")
        node: Any = self.document
        for part in pointer[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def deref(self, node: Any) -> Any:
        seen = 0
        while isinstance(node, dict) and "$ref" in node:
            node = self.ref(node["$ref"])
            seen += 1
            if seen > 20:
                raise SystemExit("$ref loop")
        return node

    def shape(self, schema: Any, depth: int = 0) -> dict[str, Any]:
        """The top level of a schema: its type, required and known properties."""
        node = self.deref(schema) or {}
        out: dict[str, Any] = {"type": node.get("type"), "required": [], "properties": []}
        if depth < 8:
            for part in node.get("allOf") or []:
                inner = self.shape(part, depth + 1)
                out["type"] = out["type"] or inner["type"]
                out["required"] += inner["required"]
                out["properties"] += inner["properties"]
        if node.get("properties"):
            out["type"] = out["type"] or "object"
            out["properties"] += list(node["properties"])
        out["required"] += list(node.get("required") or [])
        if node.get("oneOf") or node.get("anyOf") or node.get("discriminator"):
            out["variants"] = True
        if node.get("additionalProperties") is False:
            out["closed"] = True
        if node.get("format") == "binary":
            out["binary"] = True
        if node.get("type") == "array" and isinstance(node.get("items"), dict):
            out["items"] = (self.deref(node["items"]) or {}).get("type") or "object"
        out["required"] = sorted(dict.fromkeys(out["required"]))
        out["properties"] = sorted(dict.fromkeys(out["properties"]))
        return out

    def multipart(self, schema: Any) -> list[dict[str, Any]]:
        node = self.deref(schema) or {}
        required = set(node.get("required") or [])
        parts = []
        for name, prop in (node.get("properties") or {}).items():
            prop = self.deref(prop) or {}
            encoding = (
                "binary"
                if prop.get("format") == "binary"
                else (
                    "json"
                    if prop.get("type") in ("object", "array") or "properties" in prop
                    else "text"
                )
            )
            parts.append(
                {
                    "name": name,
                    "required": name in required,
                    "encoding": encoding,
                    "description": short(prop.get("description"), 300),
                }
            )
        return parts


def param_schema(spec: Spec, schema: Any) -> dict[str, Any]:
    node = spec.deref(schema) or {}
    out = {key: node[key] for key in PARAM_KEYS if key in node}
    if "type" not in out and "enum" in node:
        out["type"] = "string"
    if node.get("type") == "array":
        out["items"] = param_schema(spec, node.get("items") or {})
    if node.get("type") == "object" or "additionalProperties" in node:
        out["type"] = "object"
    return out


def servers(raw: list[dict[str, Any]] | None) -> str:
    if not raw:
        return "api"
    url = str(raw[0].get("url") or "")
    if url == "https://upload.{environment}":
        return "upload"
    if url == "https://api.{environment}":
        return "api"
    raise SystemExit(f"unexpected server {url!r}")


def scopes_of(security: Any) -> dict[str, Any]:
    user: list[str] = []
    app = False
    for requirement in security or []:
        for scheme, scopes in requirement.items():
            if scheme == "bearer-token-for-user":
                user += list(scopes or [])
            elif scheme == "bearer-token-for-application":
                app = True
            else:
                raise SystemExit(f"unexpected security scheme {scheme!r}")
    return {"user_scopes": sorted(dict.fromkeys(user)), "application_token": app}


def operation(spec: Spec, path: str, method: str, item: dict[str, Any]) -> dict[str, Any]:
    op = item[method]
    params = []
    for raw in list(item.get("parameters") or []) + list(op.get("parameters") or []):
        prm = spec.deref(raw)
        params.append(
            {
                "name": prm["name"],
                "in": prm["in"],
                "required": bool(prm.get("required")),
                "description": short(prm.get("description"), 400),
                "schema": param_schema(spec, prm.get("schema") or {}),
                **({"explode": prm["explode"]} if "explode" in prm else {}),
                **({"style": prm["style"]} if "style" in prm else {}),
            }
        )
    request: dict[str, Any] | None = None
    if op.get("requestBody"):
        body = spec.deref(op["requestBody"])
        content = body.get("content") or {}
        media_types: dict[str, Any] = {}
        for media, value in content.items():
            schema = value.get("schema") or {}
            if media == "multipart/form-data":
                media_types[media] = {"parts": spec.multipart(schema)}
            else:
                media_types[media] = spec.shape(schema)
        request = {"required": bool(body.get("required")), "content": media_types}
    responses = {}
    for code, raw in (op.get("responses") or {}).items():
        resp = spec.deref(raw)
        responses[str(code)] = {
            "content": list((resp.get("content") or {}).keys()),
            "headers": sorted((resp.get("headers") or {}).keys()),
            "description": short(resp.get("description"), 200),
        }
    security = op.get("security", spec.document.get("security"))
    return {
        "operation_id": op["operationId"],
        "method": method.upper(),
        "path": path,
        "summary": short(op.get("summary"), 200),
        "description": short(op.get("description"), 1200),
        "tags": list(op.get("tags") or []),
        "deprecated": bool(op.get("deprecated")),
        "server": servers(op.get("servers") or item.get("servers")),
        **scopes_of(security),
        "parameters": params,
        "request": request,
        "responses": responses,
    }


def extract(raw: bytes) -> dict[str, Any]:
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"the specification changed: sha256 {digest}, pinned {SHA256}")
    document = yaml.load(raw.decode("utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    spec = Spec(document)
    operations = []
    for path, item in document["paths"].items():
        for method in METHODS:
            if method in item:
                operations.append(operation(spec, path, method, item))
    schemes = spec.components.get("securitySchemes") or {}
    user_flows = schemes["bearer-token-for-user"]["flows"]
    return {
        "source": {
            "url": URL,
            "sha256": SHA256,
            "bytes": len(raw),
            "downloaded": DOWNLOADED,
            "openapi": document["openapi"],
            "title": document["info"]["title"],
            "api_version": str(document["info"]["version"]),
            "servers": document.get("servers"),
            "oauth": {
                "authorization_url": user_flows["authorizationCode"]["authorizationUrl"],
                "token_url": user_flows["authorizationCode"]["tokenUrl"],
                "device_url": user_flows.get("x-deviceCode", {}).get("authorizationUrl"),
                "note": short(schemes["bearer-token-for-user"].get("description"), 600),
            },
        },
        "operations": operations,
    }


def main() -> int:
    if not SOURCE.exists():
        print(f"download {URL} to {SOURCE} first", file=sys.stderr)
        return 1
    facts = extract(SOURCE.read_bytes())
    OUTPUT.write_text(
        json.dumps(facts, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{len(facts['operations'])} operations written to {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
