"""Review a client's sent claims against the latest ruleset (M5.8, ADR-0019).

Runs `rcm.work.scrub_review.review_client` twice. The first run raises one
named item per blocking finding; the second must review nothing
(idempotency). Needs a published ruleset: none is an error, not a pass.

    python -m tools.advance_claims && python -m tools.review_scrubs
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime

from rcm.platform.db import session_scope
from rcm.work import scrub_review
from tools.load_corpus import DEFAULT_CLIENT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--client", type=uuid.UUID, default=DEFAULT_CLIENT)
    args = parser.parse_args(argv)
    now = datetime.now(UTC)

    with session_scope() as session:
        first = scrub_review.review_client(session, client_id=args.client, now=now)
    with session_scope() as session:
        second = scrub_review.review_client(session, client_id=args.client, now=now)

    out = sys.stdout.write
    out(f"claims reviewed      {first.claims_reviewed}\n")
    out(f"claims blocked       {first.claims_blocked}\n")
    out(f"exceptions raised    {first.exceptions_raised}\n")
    out(f"reviewed, second run {second.claims_reviewed}\n")
    return 0 if second.claims_reviewed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
