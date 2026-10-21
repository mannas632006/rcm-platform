"""Export the OpenAPI schema so CI can detect contract drift.

M5.1 generates the frontend API client from this schema and fails the build if
anyone hand-edits the generated types. That only works if the schema itself is
committed and verified, otherwise the client is generated from whatever the
server happened to look like on the day.

Usage: python tools/export_openapi.py [output.json]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# The app validates its configuration at import. CI supplies the rest; this
# default keeps the export runnable on a developer machine with no .env.
os.environ.setdefault("RCM_ENVIRONMENT", "ci")


def main(argv: list[str]) -> int:
    from rcm.api.main import create_app

    destination = Path(argv[1]) if len(argv) > 1 else Path("openapi.json")
    schema = create_app().openapi()
    destination.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths = len(schema.get("paths", {}))
    print(f"wrote {destination} ({paths} paths)")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
