"""Fixtures for the leakage suite: one loaded corpus, one warehouse build."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from rcm.platform.db import reset_engines
from tests.leakage.warehouse import CORPUS, TABLES, load_corpus, run_dbt


@pytest.fixture(scope="package")
def raw_archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Where the suite's corpus is archived (PD-9); provenance reads it back."""
    return tmp_path_factory.mktemp("raw-leakage")


@pytest.fixture(scope="package")
def warehouse(owner_engine: Engine, raw_archive: Path) -> Iterator[Engine]:
    """Load the corpus and build the warehouse once for the module.

    The suite loads its own data rather than relying on whatever happens to
    be in CORE. The integration suite truncates on teardown, so a leakage
    run that assumed a loaded database would quietly measure an empty mart
    and pass -- which is the exact failure mode this suite exists to prevent
    in the data it checks.
    """
    if not CORPUS.exists():
        pytest.skip("no synthetic corpus on disk; run `make seed`")

    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    load_corpus(raw_archive)
    run_dbt(["build", "--select", "staging", "core", "marts"])
    yield owner_engine
    wipe()
