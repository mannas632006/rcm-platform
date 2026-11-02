"""Advance a client's claims on the evidence on file, and measure GATE M3.

    GATE M3: "Full synthetic claim lifecycle runs end to end: created ->
    scrubbed -> submitted -> acknowledged -> remitted -> posted or denied. No
    claim ever rests in a non-terminal state without a next action date."

Runs `rcm.work.progression.advance_all` twice. The first run moves claims;
the second must take no step (idempotency). Prints the state of every claim
afterwards and the count of claims with no next action date, which must be
zero.

    python -m tools.load_corpus && python -m tools.advance_claims
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from rcm.domain.claim import Claim
from rcm.domain.claim_lifecycle import claims_needing_action
from rcm.domain.claim_state_log import ClaimStateTransition
from rcm.platform.db import session_scope
from rcm.work import progression
from rcm.work.models import WorkItem, WorkItemKind
from tools.load_corpus import DEFAULT_CLIENT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--client", type=uuid.UUID, default=DEFAULT_CLIENT)
    args = parser.parse_args(argv)
    client_id: uuid.UUID = args.client
    now = datetime.now(UTC)

    with session_scope() as session:
        first = progression.advance_all(session, client_id=client_id, now=now)
    with session_scope() as session:
        second = progression.advance_all(session, client_id=client_id, now=now)

    with session_scope() as session:
        states = session.execute(
            select(Claim.state, func.count())
            .where(Claim.client_id == client_id)
            .group_by(Claim.state)
            .order_by(func.count().desc())
        ).all()
        unscheduled = [
            c
            for c in claims_needing_action(session, client_id=client_id, as_of=now.date())
            if c["unscheduled"]
        ]
        steps = session.scalar(
            select(func.count())
            .select_from(ClaimStateTransition)
            .where(ClaimStateTransition.client_id == client_id)
        )
        items = session.execute(
            select(WorkItem.kind, func.count())
            .where(WorkItem.client_id == client_id)
            .group_by(WorkItem.kind)
            .order_by(WorkItem.kind)
        ).all()
        # source_key is "<problem>:<claim id>"; the problem is what a person
        # is being asked to look at.
        problem = func.split_part(WorkItem.source_key, ":", 1)
        problems = session.execute(
            select(problem, func.count())
            .where(
                WorkItem.client_id == client_id,
                WorkItem.kind == str(WorkItemKind.DATA_QUALITY),
            )
            .group_by(problem)
            .order_by(problem)
        ).all()

    out = sys.stdout.write
    out(f"claims seen        {first.claims_seen}\n")
    out(f"steps, first run   {first.transitions}\n")
    out(f"steps, second run  {second.transitions}\n")
    out(f"steps on record    {steps}\n")
    out(f"held for a person  {len(first.held)}\n")
    out(f"problems raised    {first.problems_raised}\n")
    out("claims by state\n")
    for state, count in states:
        out(f"  {state:<16} {count}\n")
    out("work items by kind\n")
    for kind, count in items:
        out(f"  {kind:<26} {count}\n")
    out("data quality problems\n")
    for name, count in problems:
        out(f"  {name:<30} {count}\n")
    out(f"claims with no next action date  {len(unscheduled)}\n")
    return 0 if not unscheduled and second.transitions == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
