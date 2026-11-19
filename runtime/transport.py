"""The only way this extension reaches Allegro.

Each documented operation is a REST call to ``https://api.allegro.pl`` (or
``https://upload.allegro.pl`` where the specification says so; the
``allegro.pl.allegrosandbox.pl`` hosts for a Sandbox connection) with the method,
``Accept`` and ``Content-Type`` the specification gives for that operation, for
example ``application/vnd.allegro.public.v1+json`` or the beta media type. The
access token travels only in the ``Authorization: Bearer`` header. Requests go out
through the public Flow Steward extension SDK, which pins DNS, verifies the
connected peer, refuses redirects and enforces the timeout; they are sent one at a
time, never in parallel, and never retried.

Refusals carry an HTTP status and, by the specification, an ``ErrorsHolder``
(``{"errors": [{"code", "message", "userMessage", …}]}``) or an ``AuthError``
(``{"error", "error_description"}``). Only their texts reach the step's error,
bounded and with the token removed.

An answer with a 2xx status the operation does not document (``success`` in the
registry) is not taken as success: for a read it is an invalid answer, for a write
the change may or may not have been made (``timeout_unknown``).

Outcome of a write: a refusal shows that nothing changed only when its status is one
the operation documents (``refusals`` in the registry) and, when the documentation
gives that status a body, the body has the documented error form; and for HTTP 429,
which Allegro documents as rejecting requests over a limit. Every other failure
after the request was sent leaves the write ``timeout_unknown``, and nothing is
retried.
"""

from __future__ import annotations

import http.client
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request

from flowsteward_extension_sdk import PinnedPeerError, open_pinned_url, parse_retry_after

from . import errors, numbers
from .connection import Connection
from .errors import ExtensionError

DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_REQUEST_BODY_BYTES = 20 * 1024 * 1024
#: The most read from a JSON answer.
MAX_ANSWER_BYTES = 30 * 1024 * 1024
#: The most read from a file answer (labels, PDFs, attachments).
MAX_FILE_ANSWER_BYTES = 40 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
CHUNK_BYTES = 64 * 1024
#: Shortest window still worth starting a request in.
MIN_CALL_WINDOW_SECONDS = 15.0
_USER_AGENT = "FlowSteward-Allegro/0.1"
_PATH_RE = re.compile(r"^(/[A-Za-z0-9%._~-]+)+$")
CHECK = "; check in Allegro what changed before running the step again"
#: Response headers a step may return.
KEPT_HEADERS = ("Location", "ETag", "Last-Modified", "Retry-After")


class DeadlineReached(Exception):
    """The invocation's time budget leaves no room for another request."""


@dataclass
class Answer:
    status: int
    raw: bytes
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.raw.strip()


@dataclass(frozen=True)
class Call:
    """One request, as the registry describes it."""

    method: str
    path: str
    shown: str
    server: str = "api"
    query: Sequence[tuple[str, str]] = ()
    headers: Mapping[str, str] = field(default_factory=dict)
    accept: str = "application/vnd.allegro.public.v1+json"
    body: bytes | None = None
    content_type: str | None = None
    #: Documented 4xx statuses: {status: its body must have the error form}.
    refusals: Mapping[str, bool] = field(default_factory=dict)
    #: Documented success statuses (2xx and the 3xx an operation documents).
    success: Sequence[str] = ()
    limit: int = MAX_ANSWER_BYTES


class AllegroTransport:
    """Sends Allegro requests for one connection."""

    def __init__(
        self,
        connection: Connection,
        *,
        opener: Callable[..., Any] = open_pinned_url,
        clock: Callable[[], float] = time.monotonic,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._connection = connection
        self._opener = opener
        self.clock = clock
        self._timeout_seconds = timeout_seconds
        self._call_timeout = timeout_seconds
        #: True once a request that can change data may have left this process.
        self.mutated = False
        #: True once Allegro confirmed a change in this invocation.
        self.committed = False
        #: Monotonic time after which no request may start (set by the operation).
        self.deadline: float | None = None

    @property
    def connection(self) -> Connection:
        return self._connection

    def remaining(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - self.clock()

    # -- public calls --------------------------------------------------------

    def read(self, call: Call) -> Answer:
        return self._call(call, mutating=False)

    def write(self, call: Call) -> Answer:
        """A change; its answer once Allegro confirmed it."""
        answer = self._call(call, mutating=True)
        self.committed = True
        return answer

    # -- answers -------------------------------------------------------------

    def scrub(self, value: Any) -> Any:
        """The token removed from every text and key of a parsed answer."""
        if isinstance(value, str):
            for form in self._connection.secret_forms():
                if form in value:
                    value = value.replace(form, "[redacted]")
            return value
        if isinstance(value, Mapping):
            return {self.scrub(k): self.scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v) for v in value]
        return value

    def scrub_bytes(self, raw: bytes) -> bytes:
        for form in self._connection.secret_forms():
            encoded = form.encode("utf-8")
            if encoded in raw:
                raw = raw.replace(encoded, b"[redacted]")
        return raw

    def json(self, answer: Answer) -> Any:
        """The answer's JSON value (numbers with a point as Decimal), token removed.
        ValueError when it is not JSON."""
        return self.scrub(parse_json(answer.raw))

    def text(self, answer: Answer, limit: int = 100_000) -> str:
        return self.scrub(answer.raw.decode("utf-8", "replace"))[:limit]

    # -- one request ---------------------------------------------------------

    def _call(self, call: Call, *, mutating: bool) -> Answer:
        if not _PATH_RE.fullmatch(call.path) or call.server not in {"api", "upload"}:
            raise errors.invalid("Not an Allegro operation")
        headers = {
            "Accept": call.accept,
            "User-Agent": _USER_AGENT,
            "Authorization": "Bearer " + self._connection.access_token,
        }
        for name, value in call.headers.items():
            headers[name] = value
        if call.body is not None:
            if len(call.body) > MAX_REQUEST_BODY_BYTES:
                raise errors.invalid("The request is larger than the 20 MB this extension sends")
            headers["Content-Type"] = call.content_type or "application/vnd.allegro.public.v1+json"
        url = self._connection.base_url(call.server) + call.path
        if call.query:
            url += "?" + urlencode(list(call.query))
        request = Request(url, method=call.method, data=call.body, headers=headers)
        remaining = self.remaining()
        if remaining < MIN_CALL_WINDOW_SECONDS:
            raise DeadlineReached
        self._call_timeout = min(self._timeout_seconds, remaining)
        if mutating:
            self.mutated = True
        expires = None if self.deadline is None else self.deadline + MIN_CALL_WINDOW_SECONDS
        if expires is None:
            return self._exchange(request, call, mutating)
        outcome: dict[str, Any] = {}

        def work() -> None:
            try:
                outcome["value"] = self._exchange(request, call, mutating)
            except BaseException as exc:  # handed to the caller below
                outcome["error"] = exc

        worker = threading.Thread(target=work, name="allegro-call", daemon=True)
        worker.start()
        worker.join(max(0.0, expires - self.clock()))
        if worker.is_alive():
            _late(mutating)
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    def _exchange(self, request: Request, call: Call, mutating: bool) -> Answer:
        expires = None if self.deadline is None else self.deadline + MIN_CALL_WINDOW_SECONDS
        try:
            with self._opener(
                request, timeout_seconds=self._call_timeout, purpose="Allegro"
            ) as response:
                status = int(getattr(response, "status", 0) or getattr(response, "code", 0) or 0)
                headers = getattr(response, "headers", None) or {}
                raw = read_bounded(
                    response,
                    call.limit,
                    mutating=mutating,
                    expires=expires,
                    clock=self.clock,
                    per_read=self._call_timeout,
                )
        except HTTPError as exc:
            try:
                body = exc.read(MAX_ERROR_BODY_BYTES)
            except Exception:  # pragma: no cover - upstream body is optional
                body = b""
            status = int(exc.code)
            kept = _kept_headers(exc.headers)
            if 300 <= status < 400 and str(status) in call.success:
                return Answer(status, b"", "", kept)
            raise self.refusal(status, body or b"", call, mutating=mutating, headers=kept) from None
        except PinnedPeerError as exc:
            # Raised while resolving and pinning, before anything was sent.
            host = request.host
            if "cannot be resolved" in str(exc):
                raise ExtensionError(
                    errors.CONNECTION_FAILED,
                    f"{host} could not be resolved",
                    failure_class="transient",
                    retryable=True,
                    definitely_no_external_effect=True,
                    external_effect_status="failed",
                ) from None
            raise errors.no_effect(
                errors.BLOCKED_ADDRESS,
                f"{host} is not reachable under the host network policy",
            ) from None
        except TimeoutError:
            raise ExtensionError(
                errors.TIMEOUT_UNKNOWN if mutating else errors.TIMEOUT,
                "Allegro did not answer in time" + (CHECK if mutating else ""),
                **_transport_failure_facts(mutating=mutating),
            ) from None
        except (OSError, http.client.HTTPException):
            raise ExtensionError(
                errors.TIMEOUT_UNKNOWN if mutating else errors.CONNECTION_FAILED,
                "The connection to Allegro failed" + (CHECK if mutating else ""),
                **_transport_failure_facts(mutating=mutating),
            ) from None
        kept = _kept_headers(headers)
        if status < 200 or status >= 300:
            raise self.refusal(
                status, raw[:MAX_ERROR_BODY_BYTES], call, mutating=mutating, headers=kept
            )
        if str(status) not in call.success:
            raise undocumented(status, call, mutating=mutating)
        kind = str(headers.get("Content-Type") or "") if hasattr(headers, "get") else ""
        return Answer(status, raw, kind.split(";")[0].strip().lower(), kept)

    # -- refusals ------------------------------------------------------------

    @staticmethod
    def error_form(raw: bytes) -> bool:
        """True when ``raw`` is Allegro's documented error: an ``ErrorsHolder`` with a
        non-empty ``errors`` list of objects, or an ``AuthError`` with a text ``error``."""
        try:
            data = parse_json(raw)
        except ValueError:
            return False
        if not isinstance(data, Mapping):
            return False
        items = data.get("errors")
        if isinstance(items, list) and items and all(isinstance(i, Mapping) for i in items):
            return True
        return isinstance(data.get("error"), str) and bool(data["error"].strip())

    def detail(self, raw: bytes) -> str:
        """Allegro's own words about a refusal; never the token."""
        try:
            data = self.scrub(parse_json(raw))
        except ValueError:
            text = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "replace"))
            return errors.safe_text(self.scrub(text), 200)
        if not isinstance(data, Mapping):
            return errors.safe_text(data, 200)
        parts: list[str] = []
        items = data.get("errors")
        if isinstance(items, list):
            for item in items[:3]:
                if not isinstance(item, Mapping):
                    continue
                words = item.get("userMessage") or item.get("message") or ""
                code = item.get("code")
                path = item.get("path")
                text = errors.safe_text(words, 300)
                if isinstance(code, str) and code.strip():
                    text = f"{code.strip()}: {text}" if text else code.strip()
                if isinstance(path, str) and path.strip():
                    text += f" (at {errors.safe_text(path, 100)})"
                if text:
                    parts.append(text)
        for key in ("error_description", "error", "message"):
            value = data.get(key)
            if not parts and isinstance(value, str) and value.strip():
                parts.append(value.strip())
        return errors.safe_text("; ".join(parts), 900)

    def refusal(
        self,
        status: int,
        raw: bytes,
        call: Call,
        *,
        mutating: bool,
        headers: Mapping[str, str] | None = None,
    ) -> ExtensionError:
        shown = call.shown
        detail = self.detail(raw)
        suffix = f": {detail}" if detail else ""
        retry_after = parse_retry_after((headers or {}).get("Retry-After"))
        documented = str(status) in call.refusals
        certain = (
            not mutating
            or status == 429
            or (documented and (not call.refusals[str(status)] or self.error_form(raw)))
        )
        if status == 429:
            code = errors.RATE_LIMITED
            wait = f"; Allegro asks to wait {int(retry_after)} s" if retry_after else ""
            message = f"Allegro refused {shown}: a rate limit is reached (HTTP 429){wait}{suffix}"
        elif status in {401, 403}:
            code = errors.AUTHENTICATION_FAILED
            message = (
                f"Allegro refused {shown} (HTTP {status}){suffix}. The authorization expired or "
                "was withdrawn, or the Allegro application lacks the permission this step needs; "
                "sign in again on the Connection page or add the permission to the application"
            )
        elif 300 <= status < 400:
            code = errors.REDIRECT_REJECTED
            message = f"Allegro redirected {shown} (HTTP {status}); redirects are not followed"
        elif status == 404:
            code = errors.NOT_FOUND
            message = f"Allegro found nothing for {shown} (HTTP 404){suffix}"
        elif status == 409:
            code = errors.CONFLICT
            message = f"Allegro refused {shown} as a conflict (HTTP 409){suffix}"
        elif status in {400, 422}:
            code = errors.UPSTREAM_VALIDATION_FAILED
            message = f"Allegro refused {shown} (HTTP {status}){suffix}"
        elif status >= 500 or status == 408:
            code = errors.TIMEOUT if status in {408, 504} else errors.UPSTREAM_FAILURE
            message = f"Allegro could not complete {shown} (HTTP {status}){suffix}"
        else:
            code = errors.UPSTREAM_VALIDATION_FAILED
            message = f"Allegro rejected {shown} (HTTP {status}){suffix}"
        if not certain:
            return unknown(message + CHECK, code, status)
        if mutating:
            message += "; nothing was changed"
        return ExtensionError(
            code,
            message,
            failure_class="provider",
            retryable=not mutating and (status >= 500 or status in {408, 429}),
            definitely_no_external_effect=True,
            external_effect_status="failed",
            http_status=status,
            retry_after_seconds=retry_after if status == 429 and not mutating else None,
        )


def undocumented(status: int, call: Call, *, mutating: bool) -> ExtensionError:
    """A 2xx answer the operation does not document: not taken as success."""
    documented = ", ".join(call.success) or "none"
    message = (
        f"Allegro answered {call.shown} with HTTP {status}, which the specification does not "
        f"document for it (documented: {documented})"
    )
    if mutating:
        return unknown(message + CHECK, errors.INVALID_RESPONSE, status)
    return errors.no_effect(
        errors.INVALID_RESPONSE,
        message,
        failure_class="provider",
        retryable=False,
        http_status=status,
    )


def unknown(message: str, code: str, status: int | None) -> ExtensionError:
    return ExtensionError(
        code,
        message,
        failure_class="provider",
        retryable=False,
        definitely_no_external_effect=False,
        external_effect_status="timeout_unknown",
        http_status=status,
    )


def _kept_headers(headers: Any) -> dict[str, str]:
    kept: dict[str, str] = {}
    if headers is None or not hasattr(headers, "get"):
        return kept
    for name in KEPT_HEADERS:
        value = headers.get(name)
        if isinstance(value, str) and value.strip():
            kept[name] = errors.safe_text(value, 2000)
    return kept


def dumps_json(value: Any) -> str:
    """JSON with ``Decimal`` written as an exact number (never through float).

    A Decimal outside ``numbers``' bounds is written as text in its own exponential
    notation, never expanded."""
    marker = secrets.token_hex(8)

    def default(item: Any) -> str:
        if isinstance(item, Decimal) and numbers.bounded(item):
            return f"@{marker}@{format(item, 'f')}@{marker}@"
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError(f"{type(item).__name__} is not JSON")

    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=default)
    return re.sub(rf'"@{marker}@([-+0-9.]+)@{marker}@"', r"\1", text)


def _answer_number(integer: bool) -> Callable[[str], Any]:
    def read(text: str) -> Any:
        number = numbers.read(text, integer=integer)
        # Outside the bounds: kept as Allegro wrote it, as text, never expanded.
        return text if number is None else number

    return read


_ANSWER_FLOAT = _answer_number(False)
_ANSWER_INT = _answer_number(True)


def parse_json(raw: bytes) -> Any:
    """Any JSON value, numbers with a point read as Decimal; ValueError otherwise.

    A number outside ``numbers``' bounds (and NaN or Infinity) is kept as the text
    Allegro wrote, so reading it never expands it or fails on it."""
    try:
        text = raw.decode("utf-8-sig", "strict") if raw else ""
    except UnicodeDecodeError as exc:
        raise ValueError("not UTF-8") from exc
    if not text.strip():
        raise ValueError("empty")
    try:
        return json.loads(
            text, parse_float=_ANSWER_FLOAT, parse_int=_ANSWER_INT, parse_constant=str
        )
    except json.JSONDecodeError as exc:
        raise ValueError("bad JSON") from exc


def read_bounded(
    response: Any,
    limit: int,
    *,
    mutating: bool = False,
    expires: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    per_read: float | None = None,
) -> bytes:
    """Read at most ``limit`` bytes before ``expires``; a longer answer is an error."""
    buffer = bytearray()
    sock = _socket_of(response)
    while True:
        if expires is not None:
            left = expires - clock()
            if left <= 0:
                _late(mutating)
            if sock is not None:
                try:
                    sock.settimeout(max(0.5, min(left, per_read or left)))
                except OSError:  # pragma: no cover - a closed socket fails the read below
                    pass
        chunk = response.read(min(CHUNK_BYTES, limit + 1 - len(buffer)))
        if not chunk:
            break
        buffer.extend(chunk)
        if expires is not None and clock() > expires:
            _late(mutating)
        if len(buffer) > limit:
            if mutating:
                raise unknown(
                    "Allegro's answer was too large to read" + CHECK,
                    errors.RESPONSE_TOO_LARGE,
                    None,
                )
            raise errors.no_effect(
                errors.RESPONSE_TOO_LARGE,
                f"Allegro's answer is larger than the {limit // (1024 * 1024)} MB this extension "
                "reads; narrow the request",
            )
    return bytes(buffer)


def _late(mutating: bool) -> None:
    if not mutating:
        raise DeadlineReached
    raise ExtensionError(
        errors.TIMEOUT_UNKNOWN,
        "Allegro's answer did not arrive in time" + CHECK,
        failure_class="transient",
        retryable=False,
        definitely_no_external_effect=False,
        external_effect_status="timeout_unknown",
    )


def _socket_of(response: Any) -> Any:
    for path in (("fp", "raw", "_sock"), ("fp", "fp", "raw", "_sock"), ("_sock",)):
        node = response
        for name in path:
            node = getattr(node, name, None)
        if node is not None and hasattr(node, "settimeout"):
            return node
    return None


def _transport_failure_facts(*, mutating: bool) -> dict[str, Any]:
    return {
        "failure_class": "transient",
        "retryable": not mutating,
        "definitely_no_external_effect": not mutating,
        "external_effect_status": "timeout_unknown" if mutating else "failed",
    }


__all__ = [
    "CHECK",
    "MAX_FILE_ANSWER_BYTES",
    "MIN_CALL_WINDOW_SECONDS",
    "AllegroTransport",
    "Answer",
    "Call",
    "DeadlineReached",
    "dumps_json",
    "parse_json",
    "read_bounded",
    "undocumented",
    "unknown",
]
