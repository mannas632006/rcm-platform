"""Put the payer's status answers in front of a person (ADR-0033).

Runs `rcm.work.status_follow_up.follow_up_client` twice. The first run
raises one named item per claim whose latest 277 status answer needs a
person; the second must raise nothing new (idempotency).

    python -m tools.advance_claims && python -m tools.follow_up_status
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime

from rcm.platform.db import session_scope
from rcm.work import status_follow_up
from tools.load_corpus import DEFAULT_CLIENT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--client", type=uuid.UUID, default=DEFAULT_CLIENT)
    args = parser.parse_args(argv)
    now = datetime.now(UTC)

    with session_scope() as session:
        first = status_follow_up.follow_up_client(session, client_id=args.client, now=now)
    with session_scope() as session:
        second = status_follow_up.follow_up_client(session, client_id=args.client, now=now)

    out = sys.stdout.write
    out(f"claims read          {first.claims_read}\n")
    out(f"waiting              {first.waiting}\n")
    out(f"raised               {first.raised}\n")
    for key, count in sorted(first.by_key.items()):
        out(f"  {key:<26} {count}\n")
    out(f"raised, second run   {second.raised}\n")
    return 0 if second.raised == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
