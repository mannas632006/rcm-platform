"""The models and the migrated database describe the same schema.

PD-10: every layer rebuildable from RAW + code. A model that disagrees with
the database it was migrated to is hidden state -- a rebuild from the
migrations gives a schema the code does not describe. Drift went unnoticed
from M1.7 to M4.8 because nothing ran `alembic check`; this does.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def test_models_match_the_migrated_schema() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
