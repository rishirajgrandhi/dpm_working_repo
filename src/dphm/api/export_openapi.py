"""Dump the OpenAPI schema to stdout.

The frontend's TypeScript types are generated from this, and CI fails if the committed
types differ from a fresh generation — so the API and the UI cannot drift silently
(04 rule 5, 14 §6b).

Runs without a warehouse: schema generation needs the route signatures, not a connection.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    # Schema generation must not attempt migrations or require credentials.
    os.environ.setdefault("DPHM_SKIP_MIGRATIONS", "1")
    os.environ.setdefault("DPHM_DEV_AUTH", "1")
    os.environ.pop("DPHM_ENV", None)

    from dphm.api.main import create_app

    schema = create_app().openapi()
    json.dump(schema, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
