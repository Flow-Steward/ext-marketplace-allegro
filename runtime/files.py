"""Workflow files: what an upload step sends and what a download step saves."""

from __future__ import annotations

from collections.abc import Mapping

from . import errors
from .context import Context
from .errors import ExtensionError

ARTIFACT_TIMEOUT_SECONDS = 120.0
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024

#: Must match contracts/artifact_policies.yaml (tests/reference/build_contracts.py).
UPLOAD_BINDING = "allegro_upload"
DOWNLOAD_BINDING = "allegro_file"


def read(context: Context, handle: object, what: str = "The file") -> bytes:
    """The bytes of a workflow file given as an input; nothing is sent when it fails."""
    if not isinstance(handle, str) or not handle.strip():
        raise errors.invalid(f"{what} must be a workflow file from an earlier step")
    remaining = context.transport.remaining()
    if remaining < 5:
        raise errors.no_effect(errors.TIMEOUT, "The step ran out of time before the file was read")
    try:
        body = context.artifact_reader(
            dict(context.payload),
            artifact_id=handle.strip(),
            timeout_seconds=min(ARTIFACT_TIMEOUT_SECONDS, remaining),
        )
    except ExtensionError:
        raise
    except Exception:
        raise errors.no_effect(
            errors.ARTIFACT_INPUT_UNAVAILABLE, f"{what} could not be read from Flow Steward storage"
        ) from None
    if not isinstance(body, (bytes, bytearray)) or not body:
        raise errors.no_effect(errors.ARTIFACT_INPUT_UNAVAILABLE, f"{what} is empty")
    if len(body) > MAX_UPLOAD_BYTES:
        raise errors.invalid(f"{what} is larger than the 16 MB this extension uploads")
    return bytes(body)


def save(context: Context, data: bytes, *, content_type: str, what: str) -> str:
    """Save ``data`` as a workflow file; its handle."""
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise errors.no_effect(errors.RESPONSE_TOO_LARGE, f"{what} is larger than 40 MB")
    try:
        written = context.artifact_writer(
            dict(context.payload),
            [data],
            binding_key=DOWNLOAD_BINDING,
            content_type=content_type,
            timeout_seconds=ARTIFACT_TIMEOUT_SECONDS,
        )
    except ExtensionError:
        raise
    except Exception:
        raise errors.no_effect(
            errors.ARTIFACT_OUTPUT_UNAVAILABLE, f"{what} could not be saved"
        ) from None
    handle = written.get("artifact_handle") if isinstance(written, Mapping) else None
    if not isinstance(handle, str) or not handle.strip():
        raise errors.no_effect(errors.ARTIFACT_OUTPUT_UNAVAILABLE, f"{what} could not be saved")
    return handle.strip()


__all__ = [
    "ARTIFACT_TIMEOUT_SECONDS",
    "DOWNLOAD_BINDING",
    "MAX_DOWNLOAD_BYTES",
    "MAX_UPLOAD_BYTES",
    "UPLOAD_BINDING",
    "read",
    "save",
]
