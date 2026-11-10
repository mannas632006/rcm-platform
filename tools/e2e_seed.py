"""Seed the test database for the browser journeys (GATE M5, ADR-0023).

    python -m tools.e2e_seed

Wipes `rcm_test` and builds it up the way the system would: the smoke
corpus is loaded, claims are advanced on their evidence, denials are
classified and routed, and sent claims are reviewed against a published
ruleset. The queues the journeys work are then the ones an operator would
see, not hand-made rows.

Refuses any database but `rcm_test`: this wipes every table, and the
development database `rcm` holds the corpus the UI shows.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from rcm.decision.rules.store import publish_ruleset
from rcm.platform.config import get_settings
from rcm.platform.db import session_scope
from rcm.work import denials, progression, scrub_review
from rcm.work.models import WorkItem, WorkItemKind
from tools.load_corpus import DEFAULT_CLIENT, DEFAULT_CORPUS, load
from tools.m4_gate import seed_reference

TEST_DATABASE = "rcm_test"
PUBLISHER = "e2e:seed"


def _database_names() -> set[str | None]:
    settings = get_settings()
    return {
        make_url(settings.database_url.get_secret_value()).database,
        make_url(settings.migration_database_url.get_secret_value()).database,
    }


#: Every table but Alembic's, emptied as the database owner. The table list
#: is built inside Postgres with `format('%I')`, so no SQL is assembled here
#: (S-11).
_WIPE = text(
    """
    DO $$
    BEGIN
        EXECUTE (
            SELECT 'TRUNCATE ' || string_agg(format('%I', tablename), ', ')
                   || ' RESTART IDENTITY CASCADE'
            FROM pg_tables
            WHERE schemaname = 'public' AND tablename <> 'alembic_version'
        );
    END
    $$
    """
)


def wipe() -> None:
    engine = create_engine(get_settings().migration_database_url.get_secret_value())
    try:
        with engine.begin() as conn:
            conn.execute(_WIPE)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    if _database_names() != {TEST_DATABASE}:
        sys.stderr.write(
            f"refusing: both database URLs must name {TEST_DATABASE!r}; this wipes every table.\n"
        )
        return 2
    if not (DEFAULT_CORPUS / "x12").is_dir():
        sys.stderr.write("no corpus on disk. Run `make seed-smoke` first.\n")
        return 2

    client_id = DEFAULT_CLIENT
    now = datetime.now(UTC)
    wipe()

    # Matches RCM_RAW_ARCHIVE_ROOT in the API the journeys run against, so a
    # correction can read back the 837 it replaces (PD-9).
    archive_root = Path("data") / "archive" / "e2e"
    archive_root.mkdir(parents=True, exist_ok=True)
    counts = load(DEFAULT_CORPUS, client_id=client_id, archive_root=archive_root)
    sys.stdout.write(f"loaded {counts}\n")

    with session_scope() as session:
        seed_reference(session, today=now.date())
    with session_scope() as session:
        progression.advance_all(session, client_id=client_id, now=now)
    with session_scope() as session:
        denials.classify_and_route(session, client_id=client_id, now=now)
    with session_scope() as session:
        publish_ruleset(session, published_by=PUBLISHER)
    with session_scope() as session:
        scrub_review.review_client(session, client_id=client_id, now=now)

    with session_scope() as session:
        kinds = session.execute(
            select(WorkItem.kind, func.count()).group_by(WorkItem.kind).order_by(WorkItem.kind)
        ).all()
    for kind, count in kinds:
        sys.stdout.write(f"  {kind:<20} {count}\n")
    if not any(kind == WorkItemKind.DENIAL for kind, _ in kinds):
        sys.stderr.write("no denial items: the journeys need some.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
