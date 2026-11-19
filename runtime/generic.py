"""Runners of the generated steps: one documented Allegro operation each.

What each step accepts and how its answer is read comes from runtime/registry.json,
built from the pinned specification by tests/reference/build_contracts.py:

- path parameters are required text, percent-encoded as one path segment;
- query parameters are typed by their schema; a list sends one parameter per item
  (OpenAPI's default form/explode), an object one parameter per key;
- ``Accept-Language``, ``If-Match`` and ``If-Modified-Since`` are inputs; ``Accept``
  and ``Content-Type`` are the media types the specification gives the operation
  (vendor JSON, public or beta), never an input, except where Allegro lets the
  caller choose the file format of labels;
- a JSON request body is the ``body`` input, sent as given with exact decimals; its
  top-level required fields are checked here and the rest by Allegro;
- a file upload takes a workflow file (``file``) and the documented media type;
- a JSON answer is ``data``; one nested deeper than 48 levels is not read (a read
  fails with ``invalid_response``, a confirmed write returns its facts with ``data``
  empty and a warning); a binary answer (PDF, labels, attachments) is saved as a
  workflow file and never put into the step output; where Allegro documents the
  answer both as JSON and in another type (CSV), the ``format`` input chooses, JSON by
  default;
- a write returns ``processing``: ``completed`` when Allegro answered that the change
  is made, ``accepted`` when it answered HTTP 202 or the operation only registers a
  command Allegro applies afterwards; then ``applied`` is false and ``check`` names the
  step that reads the outcome and the ``Location`` Allegro returned.
"""

from __future__ import annotations

import json
import math
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flowsteward_extension_sdk import parse_retry_after

from . import budget, errors, files, nesting, outcome
from .context import Context
from .errors import invalid
from .params import check, text, wire
from .transport import (
    MAX_ANSWER_BYTES,
    MAX_FILE_ANSWER_BYTES,
    Answer,
    Call,
    TooDeep,
    dumps_json,
)

REGISTRY_FILE = Path(__file__).resolve().parent / "registry.json"
JSON_MEDIA = (
    "application/vnd.allegro.public.v1+json",
    "application/vnd.allegro.beta.v1+json",
    "application/json",
)


@cache
def _data() -> dict[str, Any]:
    return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))


def registry() -> dict[str, dict[str, Any]]:
    return dict(_data()["operations"])


def spec(operation_id: str) -> dict[str, Any]:
    return _data()["operations"][operation_id]


def snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and (name[i - 1].islower() or name[i - 1].isdigit()):
            out.append("_")
        out.append(ch.lower() if ch.isalnum() else "_")
    return "".join(out).strip("_")


def inputs_of(entry: Mapping[str, Any]) -> set[str]:
    names = {p["input"] for p in entry["path_params"] + entry["query"] + entry["headers"]}
    if entry["accept_choices"]:
        names.add("accept")
    if entry.get("formats"):
        names.add("format")
    body = entry["body"]
    if body:
        if body["json"]:
            names.add("body")
        if body["binary"]:
            names |= {"file", "content_type"}
        if body["multipart"]:
            names.add("file")
            names |= {snake(p["name"]) for p in body["multipart"] if p["encoding"] != "binary"}
    return names


@dataclass
class Plan:
    """A checked request; the file it uploads is read only when it is sent."""

    method: str
    path: str
    server: str
    shown: str
    query: list[tuple[str, str]] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    accept: str = JSON_MEDIA[0]
    body: bytes | None = None
    content_type: str | None = None
    file_handle: str | None = None
    multipart: list[tuple[str, str]] = field(default_factory=list)
    #: True when the answer is saved as a workflow file.
    as_file: bool = False


def prepare(operation_id: str, operation_input: Mapping[str, Any]) -> Plan:
    """The checked request of a generated step (also its test-run check)."""
    entry = spec(operation_id)
    given = {k: v for k, v in operation_input.items() if k != "connection_ref"}
    unknown = sorted(set(given) - inputs_of(entry))
    if unknown:
        raise invalid(f"The step input has unknown fields: {', '.join(unknown[:10])}")
    path = entry["path"]
    for prm in entry["path_params"]:
        value = given.get(prm["input"])
        if value is None or value == "":
            raise invalid(f"{prm['input']} is required")
        segment = check(prm["node"], value, prm["input"])
        path = path.replace("{" + prm["name"] + "}", quote(str(segment), safe=""))
    plan = Plan(
        method=entry["method"],
        path=path,
        server=entry["server"],
        shown=f"{entry['method']} {entry['path']}",
        accept=entry["accept"] or "",
    )
    for prm in entry["query"]:
        value = given.get(prm["input"])
        if value is None or value == "" or value == [] or value == {}:
            if prm["node"]["required"]:
                raise invalid(f"{prm['input']} is required")
            continue
        checked = check(prm["node"], value, prm["input"])
        if prm["node"]["kind"] == "pairs":
            plan.query += checked
        elif isinstance(checked, list):
            plan.query += [(prm["name"], wire(item)) for item in checked]
        else:
            plan.query.append((prm["name"], wire(checked)))
    for prm in entry["headers"]:
        value = given.get(prm["input"])
        if value is None or value == "":
            if prm["node"]["required"]:
                raise invalid(f"{prm['input']} is required")
            continue
        plan.headers[prm["name"]] = wire(check(prm["node"], value, prm["input"]))
    if entry["accept_choices"]:
        choice = given.get("accept")
        if choice not in entry["accept_choices"]:
            raise invalid("accept must be one of: " + ", ".join(entry["accept_choices"]))
        plan.accept = str(choice)
    formats = entry.get("formats")
    if formats:
        choice = given.get("format") or "json"
        if choice not in formats:
            raise invalid("format must be one of: " + ", ".join(formats))
        plan.accept = formats[choice]
        plan.as_file = choice != "json"
    plan.as_file = plan.as_file or entry["answer"] == "file"
    _body(entry, given, plan)
    return plan


def _body(entry: Mapping[str, Any], given: Mapping[str, Any], plan: Plan) -> None:
    body = entry["body"]
    if not body:
        return
    shape = body["json"]
    has_json = given.get("body") not in (None, "", {}, [])
    has_file = given.get("file") not in (None, "")
    if has_json and has_file:
        raise invalid("Give body or file, not both")
    if has_json and shape:
        value = given["body"]
        if shape["type"] == "array":
            if not isinstance(value, list):
                raise invalid("body must be a JSON list")
        else:
            if not isinstance(value, Mapping):
                raise invalid("body must be a JSON object")
            missing = [name for name in shape["required"] if name not in value]
            if missing:
                raise invalid("body needs: " + ", ".join(missing))
            if shape["closed"]:
                extra = sorted(str(k) for k in value if k not in shape["properties"])
                if extra:
                    raise invalid("body has unknown fields: " + ", ".join(extra[:10]))
        plan.body = dumps_json(value).encode("utf-8")
        plan.content_type = shape["content_type"]
        return
    if has_file and (body["binary"] or body["multipart"]):
        handle = given["file"]
        if not isinstance(handle, str) or not handle.strip():
            raise invalid("file must be a workflow file from an earlier step")
        plan.file_handle = handle.strip()
        if body["binary"]:
            chosen = given.get("content_type") or body["binary"][0]
            if chosen not in body["binary"]:
                raise invalid("content_type must be one of: " + ", ".join(body["binary"]))
            plan.content_type = str(chosen)
        else:
            for part in body["multipart"]:
                if part["encoding"] == "binary":
                    continue
                value = given.get(snake(part["name"]))
                if value in (None, ""):
                    if part["required"]:
                        raise invalid(f"{snake(part['name'])} is required")
                    continue
                plan.multipart.append(
                    (part["name"], text(value, snake(part["name"]), max_length=200))
                )
        return
    if body["required"]:
        options = (["body"] if shape else []) + (
            ["file"] if body["binary"] or body["multipart"] else []
        )
        raise invalid(" or ".join(options) + " is required")


def _multipart(fields: list[tuple[str, str]], file_part: str, content: bytes) -> tuple[bytes, str]:
    boundary = "fs-" + secrets.token_hex(16)
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_part}"; '
        f'filename="document.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
    )
    chunks.append(content)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def to_call(context: Context, entry: Mapping[str, Any], plan: Plan) -> Call:
    body, content_type = plan.body, plan.content_type
    if plan.file_handle is not None:
        content = files.read(context, plan.file_handle)
        if entry["body"]["multipart"]:
            file_part = next(
                p["name"] for p in entry["body"]["multipart"] if p["encoding"] == "binary"
            )
            body, content_type = _multipart(plan.multipart, file_part, content)
        else:
            body = content
    return Call(
        method=plan.method,
        path=plan.path,
        shown=plan.shown,
        server=plan.server,
        query=tuple(plan.query),
        headers=dict(plan.headers),
        accept=plan.accept,
        body=body,
        content_type=content_type,
        refusals=entry["refusals"],
        success=tuple(entry["success"]),
        limit=MAX_FILE_ANSWER_BYTES if plan.as_file else MAX_ANSWER_BYTES,
    )


def answer_result(
    context: Context, entry: Mapping[str, Any], answer: Answer, plan: Plan
) -> dict[str, Any]:
    """The outputs of an answer Allegro gave with a documented success status."""
    shown = plan.shown
    write = entry["kind"] == "write"
    warnings: list[str] = []
    result: dict[str, Any] = {"status": answer.status}
    if entry.get("formats"):
        result.update(data=None, file=None, content_type=None, size_bytes=None)
    if plan.as_file:
        handle = None
        if not answer.empty:
            kind = entry["file"]["content_type"]
            # A text file (CSV, JSON) can carry the token like any answer; a binary one
            # (PDF, labels, attachments) is saved as Allegro sent it.
            content = context.transport.scrub_bytes(answer.raw) if textual(kind) else answer.raw
            handle = files.save(context, content, content_type=kind, what="Allegro's file")
        elif answer.status not in {204, 304}:
            warnings.append(f"Allegro answered {shown} (HTTP {answer.status}) without a file")
        result.update(
            file=handle,
            content_type=answer.content_type or None,
            size_bytes=len(answer.raw) if handle else None,
        )
    elif write:
        result["data"] = outcome.write_outcome(context, entry, answer, shown, warnings)
    else:
        data: Any = None
        if not answer.empty:
            try:
                data = context.transport.json(answer)
            except ValueError as exc:
                what = (
                    f"JSON nested deeper than {nesting.MAX_ANSWER_DEPTH} levels; it is not read"
                    if isinstance(exc, TooDeep)
                    else "something that is not JSON"
                )
                raise errors.no_effect(
                    errors.INVALID_RESPONSE,
                    f"Allegro answered {shown} with {what}",
                    failure_class="provider",
                    retryable=False,
                ) from None
        result["data"] = data
    result["location"] = answer.headers.get("Location")
    result["etag"] = answer.headers.get("ETag")
    if write:
        result.update(processing_of(entry, answer.status, result["location"]))
        result["retry_after_seconds"] = retry_after(answer)
    result["warnings"] = warnings
    return result


def textual(content_type: str) -> bool:
    kind = content_type.lower()
    return kind.startswith("text/") or "json" in kind or "xml" in kind


def retry_after(answer: Answer) -> int | None:
    """Whole seconds (rounded up) Allegro asks to wait before checking an accepted
    (HTTP 202) request."""
    if answer.status != 202:
        return None
    seconds = parse_retry_after(answer.headers.get("Retry-After"))
    return None if seconds is None else max(0, math.ceil(seconds))


def processing_of(entry: Mapping[str, Any], status: int, location: str | None) -> dict[str, Any]:
    """What Allegro's answer to a write confirms: the change made (completed), or the
    request accepted, to be applied by Allegro afterwards (accepted)."""
    facts = entry["processing"]
    if status == 202 or facts["accepted_always"]:
        return {
            "processing": "accepted",
            "applied": False,
            "check": {"operation_id": facts["check"], "location": location},
        }
    return {"processing": "completed", "applied": True, "check": None}


def run(context: Context, operation_id: str) -> dict[str, Any]:
    entry = spec(operation_id)
    plan = prepare(operation_id, context.operation_input)
    call = to_call(context, entry, plan)
    if entry["kind"] == "read":
        answer = context.transport.read(call)
        result = answer_result(context, entry, answer, plan)
        budget.ensure_fits(result)
        return result
    answer = context.transport.write(call)
    return answer_result(context, entry, answer, plan)


__all__ = ["Plan", "inputs_of", "prepare", "processing_of", "registry", "run", "spec"]
