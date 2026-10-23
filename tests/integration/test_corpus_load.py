"""The M1 gate: ingest the full synthetic corpus (M1.3, M1.4, M1.8).

    "Ingest the full synthetic corpus. Every file balances or quarantines.
     Reprocessing the entire corpus changes nothing (idempotency). Report:
     files processed, rows loaded, quarantine count, and the reprocessing
     diff (must be empty)."

Two hundred files of real X12 through the real loader into a real database,
twice. The unit and integration fixtures elsewhere each contain what their
author thought of; this contains what the generator produced, including the
reversals, recoupments, multi-CAS lines and the one deliberately broken file.
"""

from __future__ import annotations

import collections
import json
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text

from rcm.domain.remittance import (
    ClaimPayment,
    ProviderAdjustment,
    RawFile,
    RawFileStatus,
    Remittance,
    RemittanceAdjustment,
    RemittanceServiceLine,
)
from rcm.ingestion.remittance_loader import (
    ASSERTION_BALANCE,
    LoadOutcome,
    load_remittance_file,
)
from rcm.ingestion.x12 import read_remittances
from rcm.platform.db import reset_engines, session_scope

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_CONFIG = REPO_ROOT / "tools" / "synthetic" / "config" / "smoke.yaml"
CLIENT = uuid.UUID("dddddddd-0000-4000-8000-00000000000d")


@pytest.fixture(scope="module")
def corpus() -> Path:
    from tools.synthetic.generate import OUT

    if OUT.exists():
        shutil.rmtree(OUT)
    result = subprocess.run(
        [sys.executable, "-m", "tools.synthetic", "--config", str(SMOKE_CONFIG)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"generator failed:\n{result.stdout}\n{result.stderr}"
    return OUT


@pytest.fixture(scope="module")
def loaded(
    corpus: Path,
    owner_engine: Engine,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, object]]:
    """Load the corpus once, then load it again, recording both passes.

    Truncation runs as the **owner**. TRUNCATE is an ownership privilege, and
    the application role deliberately does not have it -- which is the schema
    telling the truth about what the platform may do to its own CORE tables.
    """
    reset_engines()
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE remittance_adjustment, remittance_service_line, claim_payment, "
                "provider_adjustment, remittance, raw_file, payer_alias, payer "
                "RESTART IDENTITY CASCADE"
            )
        )

    archive_root = tmp_path_factory.mktemp("raw")
    files = sorted((corpus / "x12" / "835").glob("*.txt"))
    assert files, "no 835 files in the corpus"

    def pass_over() -> collections.Counter[str]:
        tally: collections.Counter[str] = collections.Counter()
        for path in files:
            with session_scope() as session:
                result = load_remittance_file(
                    session,
                    path.read_bytes(),
                    client_id=CLIENT,
                    original_filename=path.name,
                    archive_root=archive_root,
                )
            tally[result.outcome.value] += 1
            tally["claims"] += result.claims_loaded
            tally["service_lines"] += result.service_lines_loaded
            tally["adjustments"] += result.adjustments_loaded
            if result.quarantined:
                tally[f"assertion:{result.quarantine_assertion}"] += 1
        return tally

    first = pass_over()
    with session_scope() as session:
        snapshot = _row_counts(session)
    second = pass_over()
    with session_scope() as session:
        after = _row_counts(session)

    # What the reversal files re-stated, read back out of the corpus rather
    # than hard-coded: these rows are legitimately loaded but describe claims
    # the generator's CSVs already counted once.
    reversal_totals: collections.Counter[str] = collections.Counter()
    for path in files:
        if "REVERSAL" not in path.name:
            continue
        for remittance in read_remittances(path.read_bytes()):
            reversal_totals["claims"] += len(remittance.claims)
            reversal_totals["lines"] += sum(len(c.service_lines) for c in remittance.claims)
            reversal_totals["adjustments"] += len(remittance.adjustments)

    yield {
        "files": len(files),
        "first": first,
        "second": second,
        "snapshot": snapshot,
        "after": after,
        "archive_root": archive_root,
        "reversal_totals": dict(reversal_totals),
        "manifest": json.loads((corpus / "manifest.json").read_text(encoding="utf-8")),
    }
    reset_engines()


def _row_counts(session: object) -> dict[str, int]:
    scalar = session.scalar  # type: ignore[attr-defined]
    return {
        "raw_file": scalar(select(func.count()).select_from(RawFile)) or 0,
        "remittance": scalar(select(func.count()).select_from(Remittance)) or 0,
        "claim_payment": scalar(select(func.count()).select_from(ClaimPayment)) or 0,
        "service_line": scalar(select(func.count()).select_from(RemittanceServiceLine)) or 0,
        "adjustment": scalar(select(func.count()).select_from(RemittanceAdjustment)) or 0,
        "provider_adjustment": scalar(select(func.count()).select_from(ProviderAdjustment)) or 0,
    }


def test_every_file_is_either_loaded_or_quarantined(loaded: dict[str, object]) -> None:
    """No third outcome. There is no partial load and no silent skip."""
    first: collections.Counter[str] = loaded["first"]  # type: ignore[assignment]
    accounted = first[LoadOutcome.LOADED.value] + first[LoadOutcome.QUARANTINED.value]

    assert accounted == loaded["files"]
    assert first[LoadOutcome.QUARANTINED.value] == 1
    assert first[f"assertion:{ASSERTION_BALANCE}"] == 1


def test_the_loaded_rows_reconcile_with_the_generator(loaded: dict[str, object]) -> None:
    """Exact equality against what the generator says it produced.

    Two adjustments to the raw totals, both of them real rather than fudge.

    The quarantined file's rows are correctly absent, which is why the
    manifest totals match rather than fall short.

    Reversal files *are* loaded -- they are valid remittances and must be --
    but their rows re-state claims the CSV already counted, so the CSV totals
    are expected to be short by exactly what the reversals re-stated. The
    expected surplus is computed from the corpus rather than hard-coded, so a
    change in the generator's reversal rate does not silently become a change
    in what this test tolerates.
    """
    manifest: dict[str, object] = loaded["manifest"]  # type: ignore[assignment]
    counts: dict[str, int] = manifest["counts"]  # type: ignore[assignment]
    snapshot: dict[str, int] = loaded["snapshot"]  # type: ignore[assignment]
    reversal: dict[str, int] = loaded["reversal_totals"]  # type: ignore[assignment]

    assert snapshot["service_line"] == counts["service_lines"] + reversal["lines"]
    assert snapshot["adjustment"] == counts["adjustment_rows"] + reversal["adjustments"]
    assert snapshot["claim_payment"] == counts["claims"] + reversal["claims"]
    # One remittance per file except the quarantined one, which loaded none.
    assert snapshot["remittance"] == loaded["files"] - 1


@pytest.mark.invariant
def test_reprocessing_the_corpus_changes_nothing(loaded: dict[str, object]) -> None:
    """The reprocessing diff the gate asks for, and it must be empty."""
    second: collections.Counter[str] = loaded["second"]  # type: ignore[assignment]

    assert second["claims"] == 0
    assert second["service_lines"] == 0
    assert second["adjustments"] == 0
    assert second[LoadOutcome.LOADED.value] == 0
    assert loaded["snapshot"] == loaded["after"], "a second pass changed the database"


def test_the_quarantined_file_contributed_no_rows(loaded: dict[str, object]) -> None:
    """DR-10: quarantined means unparsed into CORE, not partially loaded."""
    with session_scope() as session:
        quarantined = list(
            session.scalars(select(RawFile).where(RawFile.status == str(RawFileStatus.QUARANTINED)))
        )
        assert len(quarantined) == 1
        assert quarantined[0].quarantine_assertion == ASSERTION_BALANCE
        assert quarantined[0].quarantine_context is not None

        orphaned = session.scalar(
            select(func.count())
            .select_from(Remittance)
            .where(Remittance.raw_file_id == quarantined[0].raw_file_id)
        )
        assert orphaned == 0


@pytest.mark.invariant
def test_multi_cas_lines_survive_into_the_database(loaded: dict[str, object]) -> None:
    """PD-6 at corpus volume, measured in CORE rather than in the parser."""
    del loaded
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT reasons, count(*) AS lines FROM (
                    SELECT service_line_id, count(*) AS reasons
                    FROM remittance_adjustment
                    WHERE service_line_id IS NOT NULL
                    GROUP BY service_line_id
                ) z GROUP BY reasons ORDER BY reasons
                """
            )
        ).all()

    distribution = {int(reasons): int(lines) for reasons, lines in rows}
    assert distribution.get(2, 0) >= 100, (
        f"only {distribution.get(2, 0)} lines carry two reasons; "
        "reasons are being collapsed somewhere between the file and CORE"
    )


def test_every_stored_adjustment_has_a_valid_group(loaded: dict[str, object]) -> None:
    """PD-3 across the corpus, read back out of the database."""
    del loaded
    with session_scope() as session:
        groups = session.execute(
            text("SELECT cas_group_code, count(*) FROM remittance_adjustment GROUP BY 1 ORDER BY 1")
        ).all()

    found = {str(code): int(count) for code, count in groups}
    assert set(found) <= {"CO", "PR", "OA", "PI", "CR"}
    assert found.get("CO", 0) > 0
    assert found.get("PR", 0) > 0


def test_every_archived_file_is_readable_back(loaded: dict[str, object]) -> None:
    """PD-10: CORE is rebuildable from RAW, so RAW must actually hold it."""
    from rcm.ingestion.archive import content_hash, read_archived

    archive_root: Path = loaded["archive_root"]  # type: ignore[assignment]
    with session_scope() as session:
        files = list(session.scalars(select(RawFile)))

    assert files
    for record in files:
        stored = read_archived(record.storage_path, archive_root=archive_root)
        assert len(stored) == record.byte_size
        assert content_hash(stored) == record.content_hash
