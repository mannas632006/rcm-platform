"""PD-10: every layer rebuilds from RAW and code, with no hidden state (M2.7).

    PD-10: All layers rebuildable from RAW + code. No hidden state.

The claim being tested is that nothing the platform knows exists only in a
table. Delete everything derived, replay the archived files, and the same
facts come back. If they do not, something was computed once from a state
that no longer exists -- a sequence, a clock, a row edited by hand -- and
that thing is unreproducible and unauditable.

**What "byte-identical" can and cannot mean here.** Surrogate primary keys
are `uuid4`, so a reload mints new ones and a literal byte comparison fails
for a reason that has nothing to do with correctness. UUIDs are arbitrary
labels, not facts about the claim: the facts are the business keys and the
values, and those are what this test compares. The alternative -- deriving
ids deterministically from content -- would make the literal comparison
possible and is recorded as an open design question rather than assumed
here.

Two reconstructions, because they fail for different reasons:

    marts from CORE    a model that is not a pure function of its inputs
    CORE from RAW      a loader that depends on what was already loaded
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from rcm.ingestion.acknowledgement_loader import load_acknowledgement_file
from rcm.ingestion.claim_loader import load_claim_file
from rcm.ingestion.remittance_loader import load_remittance_file
from rcm.platform.db import reset_engines, session_scope

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "data" / "synthetic"
CLIENT = uuid.UUID("99999999-0000-4000-8000-000000000099")

TABLES = (
    "claim_acknowledgement, claim_line, claim, remittance_adjustment, "
    "remittance_service_line, claim_payment, provider_adjustment, remittance, "
    "raw_file, payer_alias, payer, payer_match_candidate"
)

#: Columns that are arbitrary labels rather than facts. Excluded from the
#: comparison because `uuid4` mints a new one per load and their difference
#: carries no information.
SURROGATE_COLUMNS = frozenset(
    {
        "service_line_id",
        "claim_line_id",
        "claim_id",
        "adjustment_id",
        "root_claim_id",
        "client_id",
    }
)

#: The business key: what identifies a mart row independently of how it was
#: loaded. A submitted line is the claim the submitter named plus the
#: procedure and its occurrence; an adjustment is that plus its reason.
BUSINESS_KEY = (
    "submitter_claim_id, cpt_hcpcs, coalesce(cas_group_code, ''), "
    "coalesce(carc_code, ''), coalesce(adjustment_sequence, 0), coalesce(line_charge, 0)"
)


def run_dbt(args: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "run_dbt.py"), *args],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"dbt {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")


def mart_digest(engine: Engine) -> str:
    """A stable fingerprint of the mart's content.

    Every column except the surrogate keys, every row, in business-key
    order. Hashing rather than comparing row by row keeps the failure
    message short; when it fires, the next step is a diff, and the test says
    so.
    """
    with engine.begin() as conn:
        columns = [
            name
            for (name,) in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='analytics_marts' "
                    "AND table_name='mart_claim_line_outcome' ORDER BY ordinal_position"
                )
            ).all()
            if name not in SURROGATE_COLUMNS
        ]
        selected = ", ".join(f"coalesce({c}::text, '~')" for c in columns)
        rows = conn.execute(
            text(
                f"SELECT {selected} FROM analytics_marts.mart_claim_line_outcome "
                f"ORDER BY {BUSINESS_KEY}"
            )
        ).all()

    digest = hashlib.sha256()
    for row in rows:
        digest.update("\x1f".join(row).encode())
        digest.update(b"\x1e")
    return f"{len(rows)}:{digest.hexdigest()}"


def load_everything(archive_root: Path) -> None:
    """Replay the archived corpus in the order the files arrive."""
    for folder, loader in (
        ("837", load_claim_file),
        ("999", load_acknowledgement_file),
        ("277CA", load_acknowledgement_file),
        ("835", load_remittance_file),
    ):
        for path in sorted((CORPUS / "x12" / folder).glob("*.txt")):
            with session_scope() as session:
                loader(
                    session,
                    path.read_bytes(),
                    client_id=CLIENT,
                    original_filename=path.name,
                    archive_root=archive_root,
                )


@pytest.fixture(scope="module")
def rebuilt(owner_engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    if not CORPUS.exists():
        pytest.skip("no synthetic corpus on disk; run `make seed`")

    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    load_everything(tmp_path_factory.mktemp("raw-first"))
    run_dbt(["build", "--select", "staging", "core", "marts"])
    yield owner_engine
    wipe()


def test_the_digest_covers_a_real_dataset(rebuilt: Engine) -> None:
    """The control for both reconstruction tests.

    The digest carries its own row count, so an empty mart hashes to a
    stable value and both comparisons below would pass by comparing nothing
    to nothing. This is the assertion that stops that being silent.
    """
    rows, _, _ = mart_digest(rebuilt).partition(":")
    assert int(rows) > 100, f"the mart holds only {rows} rows; the reconstruction proves little"


@pytest.mark.invariant
def test_marts_rebuild_identically_from_core(rebuilt: Engine) -> None:
    """A model must be a pure function of its inputs.

    `--full-refresh` drops and recreates every table rather than updating in
    place, so anything that survived is state the models carried rather than
    computed -- which is what PD-10 forbids.
    """
    before = mart_digest(rebuilt)
    run_dbt(["build", "--select", "staging", "core", "marts", "--full-refresh"])
    after = mart_digest(rebuilt)

    assert after == before, (
        "the mart changed when rebuilt from unchanged CORE, so a model is not a "
        "pure function of its inputs"
    )


@pytest.mark.invariant
def test_reconstruction_from_raw(
    rebuilt: Engine, owner_engine: Engine, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """PD-10 end to end: drop everything derived, replay RAW, rebuild.

    This is the test that would catch a loader whose output depends on what
    was already in the database -- a sequence read, a `max()` taken, an
    id assigned from a counter. Those produce a platform that cannot be
    rebuilt, and the failure only shows up when somebody tries.
    """
    before = mart_digest(rebuilt)

    with owner_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    with owner_engine.begin() as conn:
        remaining = conn.execute(text("SELECT count(*) FROM claim")).scalar_one()
    assert remaining == 0, "the truncate did not empty CORE, so the rebuild proves nothing"

    load_everything(tmp_path_factory.mktemp("raw-second"))
    run_dbt(["build", "--select", "staging", "core", "marts", "--full-refresh"])
    after = mart_digest(rebuilt)

    assert after == before, (
        "replaying the archived files produced a different mart, so something the "
        "platform knows is not derivable from RAW + code (PD-10)"
    )
