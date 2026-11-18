"""Project-scoped Allegro connection: one authorized Allegro or Allegro Sandbox account.

Flow Steward Core runs the OAuth 2.0 authorization code flow (ADR 0023), keeps the
client credentials and the refresh token, refreshes the access token under a
per-connection lock and stores the new refresh token Allegro issues on every
refresh. A step receives only the current access token, in
``target.connection.secrets.access_token``; this extension never sees the refresh
token or the client secret and never mints, stores or refreshes a token itself.

The environment is the connection type: ``allegro`` is production
(``api.allegro.pl``), ``allegro_sandbox`` is Allegro Sandbox
(``api.allegro.pl.allegrosandbox.pl``). It is a property of the connection, not a
test mode: a Sandbox connection really sends its changes, to Sandbox.

The token lives in a private field of a frozen dataclass whose ``repr`` never shows
it; it travels only in the ``Authorization`` header and is never placed in a
result, a log line or an exception message.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, quote_plus

from . import errors
from .errors import ExtensionError

#: Connection type -> (environment, API host, upload host).
ENVIRONMENTS: dict[str, tuple[str, str, str]] = {
    "allegro": ("production", "api.allegro.pl", "upload.allegro.pl"),
    "allegro_sandbox": (
        "sandbox",
        "api.allegro.pl.allegrosandbox.pl",
        "upload.allegro.pl.allegrosandbox.pl",
    ),
}
SECRET_FIELDS: frozenset[str] = frozenset({"access_token"})
#: Shorter tokens could not be removed from answers without mangling them.
MIN_TOKEN_LENGTH = 16
MAX_TOKEN_LENGTH = 16384
#: Printable ASCII without spaces: a header value that cannot be split.
_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")


@dataclass(frozen=True)
class Connection:
    """One validated Allegro connection."""

    connection_id: str
    connection_type: str
    _access_token: str = field(repr=False)

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def environment(self) -> str:
        return ENVIRONMENTS[self.connection_type][0]

    def base_url(self, server: str = "api") -> str:
        _, api, upload = ENVIRONMENTS[self.connection_type]
        return "https://" + (upload if server == "upload" else api)

    @property
    def hosts(self) -> tuple[str, str]:
        _, api, upload = ENVIRONMENTS[self.connection_type]
        return api, upload

    def secret_forms(self) -> tuple[str, ...]:
        """Every way the token can appear in a text: as is and encoded."""
        token = self._access_token
        forms = {
            token,
            quote_plus(token, safe=""),
            quote(token, safe=""),
            json.dumps(token, ensure_ascii=False)[1:-1],
            html.escape(token, quote=True),
            html.escape(token, quote=False),
        }
        # Longest first, so a form that contains another is removed whole.
        return tuple(sorted((f for f in forms if f), key=len, reverse=True))

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"Connection(connection_id={self.connection_id!r})"

    __str__ = __repr__


def _invalid(message: str) -> ExtensionError:
    return errors.no_effect(errors.INVALID_CONNECTION, message)


def _token(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise _invalid(
            "The connection has no Allegro authorization; sign in to Allegro again on the "
            "Connection page"
        )
    if (
        len(text) < MIN_TOKEN_LENGTH
        or len(text) > MAX_TOKEN_LENGTH
        or not _TOKEN_RE.fullmatch(text)
    ):
        raise _invalid("The connection's Allegro authorization is not usable; sign in again")
    return text


def connection_from_payload(payload: Mapping[str, Any], *, connection_ref: str) -> Connection:
    """Validate the selected project connection before any outbound request."""
    action = payload.get("action")
    target = action.get("target") if isinstance(action, Mapping) else None
    connection = target.get("connection") if isinstance(target, Mapping) else None
    if not isinstance(connection, Mapping):
        raise _invalid("The selected connection is unavailable")
    connection_id = str(connection.get("connection_id") or "")
    if not connection_id or (connection_ref and connection_id != connection_ref):
        raise _invalid("The selected connection does not match connection_ref")
    connection_type = str(
        connection.get("connection_type_id") or connection.get("connection_type") or ""
    )
    if connection_type not in ENVIRONMENTS:
        raise _invalid("The selected connection is not an Allegro or Allegro Sandbox connection")
    secrets = connection.get("secrets")
    secrets = secrets if isinstance(secrets, Mapping) else {}
    if set(secrets) - SECRET_FIELDS:
        raise _invalid("The connection carries unsupported secrets")
    return Connection(
        connection_id=connection_id,
        connection_type=connection_type,
        _access_token=_token(secrets.get("access_token")),
    )


__all__ = [
    "ENVIRONMENTS",
    "SECRET_FIELDS",
    "Connection",
    "connection_from_payload",
]
