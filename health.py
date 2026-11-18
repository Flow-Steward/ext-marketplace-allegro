"""Health command for the Allegro extension (no network)."""

from __future__ import annotations

import json


def check_health() -> dict[str, object]:
    from runtime.catalog import OPERATION_IDS, WRITE_OPERATION_IDS
    from runtime.generic import REGISTRY_FILE

    source = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))["source"]
    return {
        "extension_id": "flowsteward.allegro",
        "ok": True,
        "status": "healthy",
        "specification_sha256": source["sha256"],
        "specification_version": source["api_version"],
        "declared_operations": len(OPERATION_IDS),
        "effect_operations": len(WRITE_OPERATION_IDS),
    }


if __name__ == "__main__":
    print(json.dumps(check_health(), separators=(",", ":"), sort_keys=True))
