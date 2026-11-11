"""`tools.ml_reset`: what it refuses to empty, and what it will not leave behind."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from tools import ml_reset

from rcm.platform.config import get_settings


def _corpus(root: Path) -> Path:
    (root / "x12").mkdir(parents=True)
    (root / "manifest.json").write_text("{}", encoding="utf-8")
    return root


def test_reset_refuses_any_database_but_the_training_one() -> None:
    """Unset, the URL is `rcm`: the corpus the operator UI shows."""
    database = get_settings().migration_database_url.get_secret_value().rsplit("/", 1)[-1]
    assert database != ml_reset.ML_DB
    with pytest.raises(SystemExit, match="refusing to empty"):
        ml_reset.reset_db()


def test_a_write_protected_destination_is_replaced_not_nested(tmp_path: Path) -> None:
    """The failure this replaced: archived RAW files (PD-9) survived the
    delete, and the new corpus was moved inside the old directory."""
    source = _corpus(tmp_path / "synthetic")
    archived = tmp_path / "full" / "raw" / "835" / "old.edi"
    archived.parent.mkdir(parents=True)
    archived.write_text("ISA", encoding="utf-8")
    archived.chmod(stat.S_IREAD)

    ml_reset.replace_corpus(source, tmp_path / "full")

    assert (tmp_path / "full" / "x12").is_dir()
    assert not (tmp_path / "full" / "raw").exists()
    assert not (tmp_path / "full" / "synthetic").exists()
    assert not source.exists()


def test_a_missing_corpus_does_not_delete_the_one_in_place(tmp_path: Path) -> None:
    kept = _corpus(tmp_path / "full")
    with pytest.raises(SystemExit, match="no corpus"):
        ml_reset.replace_corpus(tmp_path / "absent", kept)
    assert (kept / "x12").is_dir()
