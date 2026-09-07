#!/usr/bin/env python
"""Run a local PostgreSQL that stands in for RDS until a real one exists.

    python scripts/demo_source.py            # start and print connection details
    python scripts/demo_source.py --stop     # shut it down

This is NOT a mock. It is a real PostgreSQL server, seeded with the same fixture the
integration tests use, so the source-to-landing comparison exercises actual cross-engine
behaviour: real type mapping, real identifier case folding, real NULL semantics. Those
are precisely the things that break when you swap a fake for the real thing, so faking
them would test nothing.

Paste the printed details into the wizard's "I also have a source database" section.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

STATE = Path.home() / ".dphm" / "demo_source.json"
FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "postgres_seed.sql"


def _data_dir() -> Path:
    return Path.home() / ".dphm" / "demo_pgdata"


def start() -> int:
    try:
        import pgserver
    except ImportError:
        sys.stderr.write(
            "pgserver is not installed. It is in the dev extras:\n  uv pip install -e '.[dev]'\n"
        )
        return 2

    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"starting PostgreSQL in {data_dir} …")
    # cleanup_mode=None: the server must OUTLIVE this script. The default stops it when
    # the last handle closes, which meant it was gone by the time the wizard tried to
    # connect — the connection test failed with "no such file or directory" on the socket.
    server = pgserver.get_server(data_dir, cleanup_mode=None)

    # Already exists on a restart, which is the normal case.
    with contextlib.suppress(Exception):
        server.psql("create database orders_prod")

    import psycopg

    uri = server.get_uri(database="orders_prod")
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(FIXTURE.read_text())
        row = conn.execute("select count(*) from public.customers").fetchone()
        customers = row[0] if row else 0
        row = conn.execute("select count(*) from public.orders").fetchone()
        orders = row[0] if row else 0

    # pgserver listens on a unix socket, so `host` is a directory path. psycopg accepts
    # that, and so does the wizard.
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(str(uri))
    # The socket lives wherever pgserver put it, which is NOT the data directory.
    socket_dir = parse_qs(parsed.query).get("host", [""])[0] or parsed.hostname or "localhost"
    if not socket_dir or not Path(socket_dir).exists():
        sys.stderr.write(f"could not determine the socket directory from {uri!r}\n")
        return 1

    details = {
        "host": socket_dir,
        "port": parsed.port or 5432,
        "database": "orders_prod",
        "user": parsed.username or "postgres",
        "password": parsed.password or "",
    }
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(details, indent=2))

    print(f"\nseeded: {customers} customers, {orders} orders")
    print("\nPaste these into the wizard's source-database section:\n")
    for key, value in details.items():
        print(f"  {key:<9} {value if value != '' else '(leave blank)'}")
    print(
        "\nThe fixture carries deliberate defects — duplicate customers, a tie with no\n"
        "tiebreaker, a null business key, a test account, and one customer whose segment\n"
        "changes. Without something to find, a passing check proves nothing."
    )
    print(f"\nDetails saved to {STATE}")
    return 0


def stop() -> int:
    try:
        import pgserver
    except ImportError:
        return 2
    data_dir = _data_dir()
    if not data_dir.exists():
        print("not running")
        return 0
    pgserver.get_server(data_dir, cleanup_mode=None).cleanup()
    if STATE.exists():
        STATE.unlink()
    print("stopped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true", help="shut the server down")
    args = parser.parse_args()
    os.environ.setdefault("PGTZ", "UTC")
    return stop() if args.stop else start()


if __name__ == "__main__":
    raise SystemExit(main())
